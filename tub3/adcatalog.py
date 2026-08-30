"""Commercial selection with a cooldown — the one thing FieldStation42 cannot do.

Upstream picks the least-played spot (`_lowest_count`) with no recency window and no
content-level comparison. Two consequences, both visible immediately in a generated
schedule:

1. The same file airs twice inside one ten-minute block.
2. Two rips of the same spot are unrelated entries and can share a break.

Both are fixed here without editing a single upstream file. `find_commercial` is the sole
place a commercial is chosen — both callers reach it through `self.` — so overriding it
intercepts all ad selection. And `find_candidate` already implements a tested
`exclusion_index` / `proposed_start` filter that upstream simply never wires to commercials.

That matters beyond tidiness: MPL-2.0 is file-level copyleft, so a change made by subclassing
costs nothing while the same change made by editing upstream creates a file we must carry,
re-merge and republish forever.

Three details make this correct rather than merely plausible:

- **`when` has no intra-block resolution.** Every commercial in a block is offered the block's
  own start time, so a cooldown keyed on `when` alone still lets two copies land in one pod.
  A synthetic cursor advances by each pick's duration to give them distinct instants.
- **The retry is mandatory.** `NoFillerContentFound` is a *sibling* of `MatchingContentNotFound`,
  not a subclass, and upstream's fill loop only catches the latter — so raising it aborts the
  whole schedule build. A starved cooldown must degrade to "allow a repeat", never to a crash.
- **A cooldown applies to the whole cluster.** Registering the window against every sibling
  realpath is what turns a recency rule into perceptual dedupe. Upstream needs no change for
  it, because it is the same mechanism.
"""

from __future__ import annotations

import bisect
import datetime
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from fs42.catalog import ShowCatalog
from fs42.catalog_entry import MatchingContentNotFound, NoFillerContentFound

DEFAULT_COOLDOWN = datetime.timedelta(minutes=45)


# Where the ring's phase is measured from. Arbitrary but fixed: moving it would shift every
# channel's position in its series, which is the one thing this exists to keep still.
ORDER_EPOCH = datetime.datetime(2020, 1, 1)


@dataclass
class Ring:
    """A series in order, laid end to end on the clock, wrapping forever.

    The point of this being a *ring* rather than a cursor is that it holds no state. The
    episode airing in a block is a pure function of that block's start time, so there is
    nothing to persist, nothing to resynchronise after a power cut, and a rebuild of an hour
    that already aired reproduces exactly what aired. A cursor would need a table, a
    validation rule for when the library changes underneath it, and an answer for a crash
    between choosing and committing; none of that exists here because there is nothing to
    get out of step with.

    The mechanism: give every episode a *span* — the block length it will actually occupy,
    which is its duration rounded up to the schedule grid, exactly as upstream sizes a block.
    Prefix-sum the spans into a cycle of length `total`. Then the episode for a block
    beginning at time *t* is the one whose span contains `(t - epoch) mod total`.

    Because a block's length **is** its span, the offset inside a span is carried across the
    block boundary exactly, so consecutive blocks yield consecutive episodes — and the wrap
    at the end of the cycle lands back on the first episode with no special case.
    """

    order: list = field(default_factory=list)
    prefix: list = field(default_factory=list)
    total: float = 0.0

    def at(self, when: datetime.datetime):
        if not self.order or self.total <= 0:
            return None
        # Naive subtraction, deliberately: the position follows the wall clock, so a DST
        # change costs one repeated or skipped episode twice a year rather than shifting
        # every channel by an hour for half the year.
        offset = (when - ORDER_EPOCH).total_seconds() % self.total
        return self.order[bisect.bisect_right(self.prefix, offset) - 1]


class Tub3Catalog(ShowCatalog):
    """ShowCatalog that will not repeat a spot, or its twin, inside a cooldown window."""

    # Set before construction; ShowCatalog.__init__ does the catalog work, so anything the
    # override needs must exist before super().__init__ runs.
    members: dict[str, list[str]] = {}
    cooldown: datetime.timedelta = DEFAULT_COOLDOWN

    # tag -> "rotate" | "marathon". A tag not named here is chosen the way it always was:
    # weighted-random within the ask, which is right for a film channel and wrong for a
    # series nobody wants to watch out of order.
    ordered: dict[str, str] = {}
    # The grid each tag is scheduled on, so a span can be computed the way upstream sizes a
    # block. Supplied by the caller because it lives in the station config, not the catalog.
    increments: dict[str, int] = {}
    default_increment: int = 30

    # How far to relax the ask before giving up. A fixed cooldown is a wish, not a plan: the
    # sustainable spacing is bounded by the inventory, roughly (pool size x gap between ads).
    # Thirteen spots airing every ~20s cannot support 45 minutes no matter how it is asked.
    # So try the full window, then progressively shorter ones, and take the best that the
    # library can actually deliver. Falling straight from "45 minutes" to "no rule at all"
    # produces immediate repeats, which is the worst available answer rather than the
    # second-best.
    RELAXATION = (1.0, 0.5, 0.25, 0.1, 0.0)

    def __init__(self, config, *args, **kwargs):
        # History rather than a prebuilt exclusion index, so the window can be recomputed at
        # any width without rebuilding state.
        self._history: dict[str, list[datetime.datetime]] = {}
        self._cursor: datetime.datetime | None = None
        self.repeats_prevented = 0
        self.starved = 0
        self.relaxations: dict[float, int] = {}
        self._last_bump: datetime.datetime | None = None
        self.bumps_aired = 0
        self.bumps_suppressed = 0
        # Built lazily per tag, and before super() like the rest of the instance state,
        # because ShowCatalog.__init__ does the catalog work and may reach the override.
        self._rings: dict[str, Ring | None] = {}
        self.ordered_picks = 0
        super().__init__(config, *args, **kwargs)

    # ---------- programme order ----------

    def _series_of(self, entry) -> str:
        """Which series a pool entry belongs to.

        `_link_pool` names every symlink `<series>__<original name>`, so the series is
        already carried in the filename and no directory structure is needed. That matters:
        nesting the pools to get series folders is what upstream's own sequence subsystem
        requires, and doing so would put every series name through its schedule-hint parser,
        where a show normalising to `may` or `friday` silently becomes airtime-restricted.
        """
        return Path(entry.path).name.split("__", 1)[0]

    @staticmethod
    def _episode_key(entry) -> tuple:
        """Sort key within a series: season, then episode, then path.

        `tuner.titles.parse_episode` handles S04E12, 4x12 and bare 0412 alike. Sorting on the
        parsed numbers rather than the filename is what keeps S01E10 after S01E2 — a plain
        path sort puts it before, and that is the single most likely way for "in order" to be
        quietly wrong.
        """
        from tuner.titles import parse_episode  # noqa: PLC0415 - build-time only

        season, episode = parse_episode(Path(entry.path).name)
        # Unparsed files sort last, in path order, rather than colliding at the front.
        return (season if season is not None else 9999,
                episode if episode is not None else 9999,
                entry.path)

    def _ring(self, tag: str, mode: str) -> Ring | None:
        """Lay a tag's episodes out in order, once, and keep it.

        `marathon` runs each series to its end before starting the next — right for a channel
        showing one thing. `rotate` interleaves them round-robin, which is what a strip does:
        an episode of each in turn, every one of them advancing by one each cycle.
        """
        if tag in self._rings:
            return self._rings[tag]

        entries = [e for e in (self.clip_index.get(tag) or []) if (e.duration or 0) >= 1]
        if not entries:
            self._rings[tag] = None
            return None

        series: dict[str, list] = {}
        for entry in entries:
            series.setdefault(self._series_of(entry), []).append(entry)
        for episodes in series.values():
            episodes.sort(key=self._episode_key)

        names = sorted(series)
        if mode == "marathon":
            order = [episode for name in names for episode in series[name]]
        else:
            # Spread each series evenly across the whole cycle, rather than taking one from
            # each in turn.
            #
            # Round-robin by index looks like the obvious way to interleave and is wrong at
            # the end: the short series run out first, so the tail is whatever series is
            # longest, alone. Measured on channel 9, whose travel pool is 79 episodes of
            # Drive to Survive against a single Bourdain — the last 37 entries were an
            # unbroken Formula 1 marathon, and the schedule landed in it.
            #
            # Placing each episode at its fractional position within its own series and
            # sorting on that gives every series a share of the cycle proportional to its
            # size, evenly distributed. A big series comes round often, a small one rarely,
            # and neither ever bunches. Ties break on the series name so the layout is
            # deterministic, and episodes within a series keep their order because their
            # positions increase with their index.
            placed = []
            for name in names:
                episodes = series[name]
                for index, episode in enumerate(episodes):
                    placed.append(((index + 0.5) / len(episodes), name, episode))
            placed.sort(key=lambda item: (item[0], item[1]))
            order = [episode for _, _, episode in placed]

        # A span is the block the episode will actually occupy: its duration rounded up to
        # the grid, which is exactly how upstream sizes a block. Using the same arithmetic is
        # what makes consecutive blocks land on consecutive episodes rather than drifting.
        grid = (self.increments.get(tag) or self.default_increment) * 60
        prefix, running = [], 0.0
        for entry in order:
            prefix.append(running)
            running += max(grid, math.ceil((entry.duration or 0) / grid) * grid)

        ring = Ring(order=order, prefix=prefix, total=running)
        unparsed = sum(1 for e in entries if self._episode_key(e)[0] == 9999)
        print(f"    order: {tag} {mode} — {len(names)} series, {len(order)} episodes, "
              f"cycle {running / 3600:.1f}h"
              + (f", {unparsed} unnumbered" if unparsed else ""))
        self._rings[tag] = ring
        return ring

    def find_candidate(self, tag, seconds, when, exclusion_index=None,
                       proposed_start=None, meta_hints=None):
        """Pick the episode whose turn it is, when the tag is an ordered one.

        Falls through to upstream for everything else, and also whenever the ring's answer
        will not do — too long for the ask, or barred from this hour by a path-derived
        schedule hint. Forcing it in either case would be worse than being out of order.
        """
        mode = self.ordered.get(tag)
        if mode:
            ring = self._ring(tag, mode)
            entry = ring.at(when) if ring else None
            if entry is not None and seconds > (entry.duration or 0) >= 1:
                self.ordered_picks += 1
                entry.count = (getattr(entry, "count", 0) or 0) + 1
                return entry
        return super().find_candidate(tag, seconds, when, exclusion_index,
                                      proposed_start, meta_hints)

    # ---------- the one override ----------

    def make_reel_block(self, when, bumpers=True, *args, **kwargs):
        """A station ident at most once per block, instead of around every break.

        Upstream puts a bumper on each end of every break. Over a day that is 320 airings —
        the channel announcing itself every eight minutes to someone who has not moved.

        This used to switch them off entirely on ad-supported channels, which was right when
        the bump pool held four generated cards reading CHANNEL 4 in a system font. It stopped
        being right the moment the channels got actual identities: Sludge, Miss Birdie, the
        Director-General and his monocle are the personality of the dial, and a personality
        nobody ever sees is just a file on a disk. The complaint was never "show me no
        bumpers", it was that the same placeholder appeared between every commercial roll.

        So the rule is frequency, not absence: one ident per `schedule_increment` of airtime.
        That number is already the channel's own sense of how long a programme block is, so it
        tunes itself — a half-hour sitcom strip idents about once a show, a two-hour film
        channel idents about once a film, and neither needed a second setting to say so.

        `bumpers=False` is upstream's own parameter — `make_reel_block` skips both `find_bump`
        calls and returns `ReelBlock(None, reels, None)`. No fork, and no risk of the empty
        `bump_dir` trap, because the bump content still exists and is still catalogued.

        Commercial-free channels are always allowed one: with no advertising to fill a break,
        upstream fills it from the bump pool instead, so suppressing them there would leave it
        with nothing to schedule at all.
        """
        if self.config.get("commercial_free"):
            return super().make_reel_block(when, bumpers, *args, **kwargs)

        allow = bool(bumpers) and self._bump_is_due(when)
        if allow:
            self._last_bump = when
            self.bumps_aired += 1
        elif bumpers:
            self.bumps_suppressed += 1
        return super().make_reel_block(when, allow, *args, **kwargs)

    def _bump_is_due(self, when) -> bool:
        """Has a block's worth of airtime passed since the last ident?

        `when` is whatever upstream hands the reel builder. It is a datetime in every path
        this project uses, but the guard costs nothing and the alternative is a TypeError
        deep inside a forty-minute build.
        """
        if not isinstance(when, datetime.datetime):
            return True
        if self._last_bump is None:
            return True
        gap = datetime.timedelta(minutes=self.config.get("schedule_increment", 30) or 30)
        return abs(when - self._last_bump) >= gap

    def find_commercial(self, seconds, when, commercial_dir):
        tag = commercial_dir if commercial_dir else self.config.get("commercial_dir")
        if not tag or not self.clip_index.get(tag):
            raise NoFillerContentFound(f"No commercials indexed under tag={tag!r}")

        # Every commercial in a block is offered the block's own start time, so a rule keyed
        # on `when` alone cannot separate two picks inside one pod. The cursor gives each pick
        # a distinct instant.
        at = self._cursor if (self._cursor and self._cursor > when) else when

        last_error: Exception | None = None
        for scale in self.RELAXATION:
            window = self.cooldown * scale
            index = self._index_for(window, at) if scale > 0 else None
            try:
                pick = self.find_candidate(
                    tag, seconds, when,
                    exclusion_index=index,
                    proposed_start=at if index is not None else None,
                )
            except MatchingContentNotFound as exc:
                last_error = exc
                continue
            if pick is None:
                continue

            if scale >= 1.0:
                self.repeats_prevented += 1
            elif scale > 0:
                self.relaxations[scale] = self.relaxations.get(scale, 0) + 1
            else:
                self.starved += 1
            self._note(pick, at, window)
            return pick

        # Never raise NoFillerContentFound from here on a starved pool: it is a *sibling* of
        # MatchingContentNotFound, not a subclass, so upstream's fill loop does not catch it
        # and the entire schedule build would abort.
        raise MatchingContentNotFound(
            f"No commercial under {seconds}s in tag={tag!r}"
        ) from last_error

    # ---------- bookkeeping ----------

    def _index_for(
        self, window: datetime.timedelta, at: datetime.datetime
    ) -> dict[str, list[tuple[datetime.datetime, datetime.datetime]]]:
        """Build the exclusion index at a given width, from airing history."""
        if not window:
            return {}
        floor = at - window
        index: dict[str, list[tuple[datetime.datetime, datetime.datetime]]] = {}
        for realpath, times in self._history.items():
            recent = [(t, t + window) for t in times if t >= floor]
            if recent:
                index[realpath] = recent
        return index

    def _note(self, pick, at: datetime.datetime, window: datetime.timedelta) -> None:
        duration = float(getattr(pick, "duration", 0.0) or 0.0)
        self._cursor = at + datetime.timedelta(seconds=max(duration, 0.5))

        realpath = getattr(pick, "realpath", None)
        if not realpath:
            return
        realpath = os.path.realpath(realpath)

        # Record against every member of the cluster. That single step is what turns a
        # recency rule into perceptual dedupe — two rips of one spot block each other.
        for sibling in self.members.get(realpath, [realpath]):
            times = self._history.setdefault(sibling, [])
            times.append(at)
            # Keep history bounded; nothing older than the widest window can ever matter.
            if len(times) > 64:
                cutoff = at - self.cooldown
                self._history[sibling] = [t for t in times if t >= cutoff][-64:]

    def seed(self, airings: list[tuple[str, datetime.datetime]]) -> None:
        """Carry cooldowns across builds.

        Upstream never persists commercial play counts — the write is commented out — so each
        `add_week` otherwise restarts rotation from zero, and the seam between two builds is
        exactly where a repeat shows up.
        """
        for realpath, aired in airings:
            if not realpath:
                continue
            realpath = os.path.realpath(realpath)
            for sibling in self.members.get(realpath, [realpath]):
                self._history.setdefault(sibling, []).append(aired)


def install(
    commercial_dir: Path | None = None,
    *,
    cooldown_minutes: int = 45,
    refresh_clusters: bool = False,
    ordered: dict[str, str] | None = None,
    increments: dict[str, int] | None = None,
    default_increment: int = 30,
) -> type[Tub3Catalog]:
    """Bind Tub3Catalog into the schedule builder and load perceptual clusters.

    `fs42.liquid_schedule` imports the *name* `ShowCatalog` and constructs it at call time,
    so rebinding the module attribute substitutes our subclass with no fork. That module is
    the only construction site on the ad-selection path.
    """
    import fs42.liquid_schedule as liquid_schedule

    Tub3Catalog.cooldown = datetime.timedelta(minutes=cooldown_minutes)
    # Per-station, so it must be reset every install rather than accumulated — two channels
    # can use the same tag name with different ordering.
    Tub3Catalog.ordered = dict(ordered or {})
    Tub3Catalog.increments = dict(increments or {})
    Tub3Catalog.default_increment = default_increment

    if commercial_dir is not None:
        from .clusters import load_or_compute

        result = load_or_compute(Path(commercial_dir), refresh=refresh_clusters)
        Tub3Catalog.members = result.members()

    liquid_schedule.ShowCatalog = Tub3Catalog
    return Tub3Catalog
