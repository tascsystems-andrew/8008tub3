"""The virtual clock.

The thing that makes this feel like television rather than IPTV: **channels are timetables,
not streams.** Nothing is decoded, transcoded or buffered until somebody tunes in. Thirty
channels cost nothing while nobody is watching them, and tuning is a local file open plus a
seek.

The clock always knows what *should* be on air. Tuning just asks it, then punches in at the
right offset — measured end to end through mpv at 17ms median and 50ms worst, against a
1000ms budget. That is exactly how a real cable box behaved: the broadcast was always
happening, you simply joined it mid-flight.

Two channel kinds:

- **Loop** — a playlist that repeats forever from a fixed epoch. What is airing is pure
  arithmetic on the current time, so there is nothing to store and nothing to regenerate.
  Crucially it *cannot expire*, which makes it the right fallback when a real schedule runs
  out — but it can never carry ad breaks, because FieldStation42's loop blocks emit
  back-to-back content with no reel blocks at all. Fallback only, never the main event.
- **Liquid** — reads FieldStation42's generated schedule, which is a timetable that already
  has the commercial pods resolved into it. Added in the FS42 integration; see INTEGRATION.md.

Both answer one question, which is the only question the tuner ever asks:
"what is on channel N right now, and how far into it are we?"
"""

from __future__ import annotations

import bisect
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Program:
    path: Path
    duration: float
    title: str = ""

    @property
    def name(self) -> str:
        return self.title or self.path.stem.replace("_", " ").replace(".", " ")


@dataclass(frozen=True)
class PlanEntry:
    """One item inside a scheduled block: a program half, an ad, a station ident.

    `skip` is the load-bearing field. A program interrupted by a mid-roll appears twice, and
    the second appearance carries the offset at which it resumes. Tuning into an entry means
    seeking to `skip + offset`, never to `offset` alone.
    """

    path: Path
    skip: float
    duration: float
    content_type: str = "feature"
    is_stream: bool = False

    @property
    def name(self) -> str:
        return self.path.stem.replace("_", " ")


# Upstream tags every plan entry it writes: "commercial" for a spot, "bump" for an ident
# (fs42/liquid_blocks.py:356-360). Anything else is the programme.
BREAK_TYPES = frozenset({"commercial", "bump"})


def _feature_slot(entries: tuple, index: int) -> int:
    """Which entry in the plan is the *programme*, seen from `index`.

    Backwards first. During a mid-roll the programme is the thing that was already playing,
    and the viewer's question is "what am I watching", not "what is this advert". Forwards is
    the fallback for a block that opens with an ident, where there is nothing behind us yet.
    """
    if not entries or not (0 <= index < len(entries)):
        return index
    if entries[index].content_type not in BREAK_TYPES:
        return index
    for position in range(index - 1, -1, -1):
        if entries[position].content_type not in BREAK_TYPES:
            return position
    for position in range(index + 1, len(entries)):
        if entries[position].content_type not in BREAK_TYPES:
            return position
    return index


@dataclass(frozen=True)
class Airing:
    """What is on, and where we are in it."""

    channel: int
    channel_name: str
    program: Program
    offset: float          # seconds into the current entry, right now
    started_at: float      # wall-clock epoch seconds when the entry began
    ends_at: float
    # Present when the airing came from a real schedule. A block is a list of entries — a
    # program, its ad pod, and the rest of the program — so advancing at the end of one entry
    # means stepping to the next, not re-querying the clock.
    plan: tuple[PlanEntry, ...] = ()
    index: int = 0
    skip: float = 0.0
    content_type: str = "feature"
    off_air: bool = False

    @property
    def remaining(self) -> float:
        """Seconds left in this *entry* — until the next thing plays, ad or otherwise.

        Used to decide when to advance, where it is exactly right. It is the wrong number to
        show a viewer, which is what `programme_remaining` is for.
        """
        return max(0.0, self.program.duration - self.offset)

    @property
    def feature_path(self) -> Path:
        """The programme's file, not whatever is on screen this second.

        `program.path` is what the player is told to open, so during a break it is the advert
        — correct for playback and wrong for the bug, which was showing the name of a
        commercial as though it were the show. Kept as a separate property rather than
        changing `program`, because `Box.tune` opens `program.path` and must keep doing so.
        """
        if not self.plan:
            return self.program.path
        return self.plan[_feature_slot(self.plan, self.index)].path

    @property
    def in_break(self) -> bool:
        if not self.plan or not (0 <= self.index < len(self.plan)):
            return False
        return self.plan[self.index].content_type in BREAK_TYPES

    @property
    def programme_remaining(self) -> float:
        """Wall-clock seconds until this programme actually finishes, ads included.

        A show interrupted by mid-rolls appears in the plan several times — a 23-minute Bill
        Nye is four 5m45s entries with breaks between them — so the entry countdown says
        "3 min left" eighteen minutes before the episode ends. It is counting to the next
        commercial, which reads as counting to the end of the show.

        This walks forward to the last entry that is the same file and includes everything in
        between, because the ad breaks are part of how long you will be sitting there. That
        is what someone reading "18 min left" is actually asking.
        """
        if not self.plan:
            return self.remaining

        # Keyed on the programme, not on the current entry. Measured from an advert it used
        # to look forward for more of *that advert*, find none, and report the seconds left
        # in the spot — so the countdown collapsed to "ending" every time a break started.
        here = self.feature_path
        last = self.index
        for position in range(self.index + 1, len(self.plan)):
            if self.plan[position].path == here:
                last = position
        if last == self.index:
            return self.remaining

        total = sum(entry.duration for entry in self.plan[self.index:last + 1])
        return max(0.0, total - self.offset)

    @property
    def seek(self) -> float:
        """Where to punch into the file. The only position the player should ever be given."""
        return self.skip + self.offset

    @property
    def next_entry(self) -> PlanEntry | None:
        if self.plan and self.index + 1 < len(self.plan):
            return self.plan[self.index + 1]
        return None


class Channel:
    def __init__(self, number: int, name: str):
        self.number = number
        self.name = name

    def now(self, at: float) -> Airing | None:  # pragma: no cover - interface
        raise NotImplementedError


class GuideChannel(Channel):
    """Channel 2: the listings, over music.

    It has no schedule of its own and never goes off air, which is the whole idea — the guide
    is the one thing always there to tell you what everything else is doing. `now` therefore
    returns a placeholder airing rather than reading any timetable, and the real content is
    drawn by `tuner.guide` from the *other* channels in this lineup.

    Carrying `is_guide` on the channel rather than sniffing the number keeps the special case
    in one place. `Box` asks the channel what it is; nothing else has to know that 2 is
    spoken for.
    """

    is_guide = True

    def __init__(self, number: int, name: str, music: list[Path] | None = None):
        super().__init__(number, name)
        self.music = list(music or [])

    def now(self, at: float) -> Airing:
        return Airing(
            channel=self.number,
            channel_name=self.name,
            program=Program(Path(""), 0.0, self.name),
            offset=0.0,
            started_at=at,
            ends_at=at + 3600.0,
        )


class AmbianceChannel(Channel):
    """A fire, a fish tank, a train window. One thing, looping, never off air.

    Same shape as `GuideChannel` and for the same reason: there is no timetable to express, so
    there is nothing to schedule, catalogue or top up. `Box` asks the channel what it is and
    hands mpv a looping playlist.

    Unlike the guide it needs no synthetic backdrop — the content is video, so the picture is
    its own surface — and it keeps the ordinary channel bug, because it *is* an ordinary
    channel to look at and someone arriving on it should be told where they are.
    """

    is_ambiance = True

    def __init__(self, number: int, name: str, folder: Path | None = None):
        super().__init__(number, name)
        self.folder = Path(folder) if folder else None

    @property
    def clips(self) -> list[Path]:
        """Resolved at tune time, so the month rolls over without restarting the box."""
        from .ambiance import clips_for
        return clips_for(self.folder)

    def now(self, at: float) -> Airing:
        clips = self.clips
        title = clips[0].stem.replace("_", " ").replace(".", " ") if clips else self.name
        return Airing(
            channel=self.number,
            channel_name=self.name,
            program=Program(clips[0] if clips else Path(""), 0.0, title[:44]),
            offset=0.0,
            started_at=at,
            ends_at=at + 3600.0,
            off_air=not clips,
        )


class LoopChannel(Channel):
    """A playlist repeating forever from a fixed epoch.

    Storage is one epoch and the playlist. Everything else is arithmetic, which is why a
    channel can have been "broadcasting" since 1993 without a scheduler ever running.
    """

    def __init__(self, number: int, name: str, programs: list[Program], epoch: float = 0.0):
        super().__init__(number, name)
        self.programs = programs
        self.epoch = epoch
        # Prefix sums so locating the current program is a binary search rather than a walk.
        self._edges: list[float] = []
        total = 0.0
        for program in programs:
            total += program.duration
            self._edges.append(total)
        self.total = total

    def now(self, at: float) -> Airing | None:
        if not self.programs or self.total <= 0:
            return None

        elapsed = (at - self.epoch) % self.total
        index = bisect_right(self._edges, elapsed)
        index = min(index, len(self.programs) - 1)

        started_before = self._edges[index - 1] if index else 0.0
        offset = elapsed - started_before
        program = self.programs[index]

        return Airing(
            channel=self.number,
            channel_name=self.name,
            program=program,
            offset=offset,
            started_at=at - offset,
            ends_at=at - offset + program.duration,
        )

    def next_after(self, at: float) -> Program | None:
        current = self.now(at)
        if current is None:
            return None
        index = self.programs.index(current.program)
        return self.programs[(index + 1) % len(self.programs)]


@dataclass(frozen=True)
class Block:
    """A scheduled block with its plan already resolved into entries."""

    start: float                    # epoch seconds
    end: float
    title: str
    entries: tuple[PlanEntry, ...]
    edges: tuple[float, ...]        # cumulative entry ends, relative to `start`


class LiquidChannel(Channel):
    """Reads FieldStation42's generated schedule.

    This is the channel kind that matters: an FS42 block is a timetable entry whose plan
    already has the ad pods resolved into it, so tuning here lands you mid-break exactly as
    it would mid-programme.

    Three things are done deliberately differently from upstream's own reader:

    - **Half-open bounds.** Upstream tests ``when >= start and when <= end``, so at an exact
      boundary two blocks match and the earlier one wins — replaying a second of programming
      already aired.
    - **Explicit off-air on gaps.** Upstream returns ``None`` for a schedule gap and then
      dereferences it, which raises inside the player. Gaps are producible in normal
      operation, so they get a real answer here and the box draws its OFF AIR card.
    - **Epoch seconds throughout.** Upstream stores naive local datetimes and marches forward
      with plain timedeltas. Twice a year that is wrong: on the autumn fall-back a linear
      scan finds the *earlier* of two identical local hours, and in spring an hour of
      programming silently never plays. Conversion happens only at the SQL boundary.
    """

    def __init__(
        self,
        number: int,
        name: str,
        db_path: Path,
        station: str,
        *,
        window_back: float = 3600.0,
        window_forward: float = 6 * 3600.0,
    ):
        super().__init__(number, name)
        self.db_path = Path(db_path)
        self.station = station
        self.window_back = window_back
        self.window_forward = window_forward
        self._blocks: list[Block] = []
        self._starts: list[float] = []
        self._loaded_from: float = 0.0
        self._loaded_to: float = 0.0

    # ---------- loading ----------

    def _load(self, at: float) -> None:
        import sqlite3
        from datetime import datetime

        start = at - self.window_back
        end = at + self.window_forward

        # Bind datetimes, never ISO strings. Upstream stores 'YYYY-MM-DD HH:MM:SS' and
        # compares as TEXT; 'T' (0x54) sorts above ' ' (0x20), so an isoformat() bound here
        # silently selects the wrong rows rather than failing.
        lo = datetime.fromtimestamp(start)
        hi = datetime.fromtimestamp(end)

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT start_time, end_time, title, plan_json FROM liquid_blocks "
                "WHERE station = ? AND start_time < ? AND end_time > ? ORDER BY start_time",
                (self.station, hi, lo),
            ).fetchall()
        finally:
            conn.close()

        blocks: list[Block] = []
        for start_text, end_text, title, plan_json in rows:
            block = self._to_block(start_text, end_text, title, plan_json)
            if block is not None:
                blocks.append(block)

        self._blocks = blocks
        self._starts = [b.start for b in blocks]
        self._loaded_from, self._loaded_to = start, end

    @staticmethod
    def _to_block(start_text: str, end_text: str, title: str, plan_json: str) -> Block | None:
        import json
        from datetime import datetime

        def _epoch(text: str) -> float:
            if isinstance(text, (int, float)):
                return float(text)
            text = str(text).replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt).timestamp()
                except ValueError:
                    continue
            return 0.0

        try:
            plan = json.loads(plan_json)
        except (TypeError, json.JSONDecodeError):
            return None

        entries: list[PlanEntry] = []
        edges: list[float] = []
        running = 0.0
        for item in plan:
            duration = float(item.get("duration") or 0.0)
            if duration <= 0:
                continue
            entries.append(PlanEntry(
                path=Path(item.get("path") or ""),
                skip=float(item.get("skip") or 0.0),
                duration=duration,
                content_type=item.get("content_type") or "feature",
                is_stream=bool(item.get("is_stream")),
            ))
            running += duration
            edges.append(running)

        if not entries:
            return None

        start = _epoch(start_text)
        return Block(
            start=start,
            end=_epoch(end_text),
            title=title or "",
            entries=tuple(entries),
            edges=tuple(edges),
        )

    # ---------- querying ----------

    def _off_air(self, at: float) -> Airing:
        return Airing(
            channel=self.number,
            channel_name=self.name,
            program=Program(Path(""), 0.0, "OFF AIR"),
            offset=0.0,
            started_at=at,
            ends_at=at,
            off_air=True,
        )

    def now(self, at: float) -> Airing | None:
        if not self._blocks or not (self._loaded_from <= at < self._loaded_to):
            self._load(at)
        if not self._blocks:
            return self._off_air(at)

        index = bisect.bisect_right(self._starts, at) - 1
        if index < 0:
            return self._off_air(at)

        block = self._blocks[index]
        if not (block.start <= at < block.end):
            return self._off_air(at)

        elapsed = at - block.start
        slot = bisect.bisect_right(block.edges, elapsed)
        if slot >= len(block.entries):
            # The plan is shorter than the block — upstream pads with be_right_back media,
            # but a rounding gap at the tail is still possible.
            return self._off_air(at)

        entry = block.entries[slot]
        began_before = block.edges[slot - 1] if slot else 0.0
        offset = elapsed - began_before

        # The bug shows what you are *watching*, which stays the programme's name even during
        # its ad break — that is what a cable box did, and "cola 30s" on screen would be worse.
        # The fallback has to name the feature too, or a block with no title reverts to the
        # advert's filename the moment a break starts.
        display = block.title or block.entries[_feature_slot(block.entries, slot)].name

        return Airing(
            channel=self.number,
            channel_name=self.name,
            program=Program(entry.path, entry.duration, display),
            offset=offset,
            started_at=block.start + began_before,
            ends_at=block.start + block.edges[slot],
            plan=block.entries,
            index=slot,
            skip=entry.skip,
            content_type=entry.content_type,
        )

    def horizon(self) -> tuple[float, float] | None:
        """How far the generated schedule actually runs — the supervisor's input."""
        import sqlite3
        from datetime import datetime

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT MIN(start_time), MAX(end_time) FROM liquid_blocks WHERE station = ?",
                (self.station,),
            ).fetchone()
        finally:
            conn.close()
        if not row or row[0] is None:
            return None

        def _epoch(text: str) -> float:
            return datetime.strptime(str(text).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()

        try:
            return _epoch(row[0]), _epoch(row[1])
        except ValueError:
            return None

    def invalidate(self) -> None:
        self._blocks = []
        self._loaded_from = self._loaded_to = 0.0


@dataclass
class Lineup:
    channels: list[Channel] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.channels.sort(key=lambda c: c.number)

    @property
    def numbers(self) -> list[int]:
        return [c.number for c in self.channels]

    def get(self, number: int) -> Channel | None:
        return next((c for c in self.channels if c.number == number), None)

    def now(self, number: int, at: float) -> Airing | None:
        channel = self.get(number)
        return channel.now(at) if channel else None

    def surf(self, current: int, delta: int) -> int:
        """Next/previous channel, wrapping — which is what makes it feel like a dial."""
        if not self.channels:
            return current
        numbers = self.numbers
        if current in numbers:
            index = numbers.index(current)
        else:
            index = bisect.bisect_left(numbers, current) % len(numbers)
            if delta > 0:
                index -= 1
        return numbers[(index + delta) % len(numbers)]
