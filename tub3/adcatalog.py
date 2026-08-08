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

import datetime
import os
from pathlib import Path

from fs42.catalog import ShowCatalog
from fs42.catalog_entry import MatchingContentNotFound, NoFillerContentFound

DEFAULT_COOLDOWN = datetime.timedelta(minutes=45)


class Tub3Catalog(ShowCatalog):
    """ShowCatalog that will not repeat a spot, or its twin, inside a cooldown window."""

    # Set before construction; ShowCatalog.__init__ does the catalog work, so anything the
    # override needs must exist before super().__init__ runs.
    members: dict[str, list[str]] = {}
    cooldown: datetime.timedelta = DEFAULT_COOLDOWN

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
        super().__init__(config, *args, **kwargs)

    # ---------- the one override ----------

    def make_reel_block(self, when, bumpers=True, *args, **kwargs):
        """Break pods without a station ident wrapped round them.

        Upstream puts a bumper on each end of every break. Over a day that is 320 airings of
        the same four generated cards — the channel announcing itself every eight minutes to
        someone who has not moved. Real stations did ident around breaks, but they had
        hundreds of them and did not use the same four on a loop.

        The channel identification belongs on the *change*, which is where the tuner already
        puts it: `BugState` shows channel, name and what is on for four seconds after a tune,
        fades on its own, and comes back on a BACK press. That is the moment someone actually
        wants to know what they are watching.

        `bumpers=False` is upstream's own parameter — `make_reel_block` skips both `find_bump`
        calls and returns `ReelBlock(None, reels, None)`. No fork, and no risk of the empty
        `bump_dir` trap, because the bump content still exists and is still catalogued.

        Left on for commercial-free channels: with no advertising to fill a break, upstream
        fills it from the bump pool instead, so switching bumpers off there would leave it
        with nothing to schedule at all.
        """
        if self.config.get("commercial_free"):
            return super().make_reel_block(when, bumpers, *args, **kwargs)
        return super().make_reel_block(when, False, *args, **kwargs)

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
) -> type[Tub3Catalog]:
    """Bind Tub3Catalog into the schedule builder and load perceptual clusters.

    `fs42.liquid_schedule` imports the *name* `ShowCatalog` and constructs it at call time,
    so rebinding the module attribute substitutes our subclass with no fork. That module is
    the only construction site on the ad-selection path.
    """
    import fs42.liquid_schedule as liquid_schedule

    Tub3Catalog.cooldown = datetime.timedelta(minutes=cooldown_minutes)

    if commercial_dir is not None:
        from .clusters import load_or_compute

        result = load_or_compute(Path(commercial_dir), refresh=refresh_clusters)
        Tub3Catalog.members = result.members()

    liquid_schedule.ShowCatalog = Tub3Catalog
    return Tub3Catalog
