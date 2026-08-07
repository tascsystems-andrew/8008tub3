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
class Airing:
    """What is on, and where we are in it."""

    channel: int
    channel_name: str
    program: Program
    offset: float          # seconds into the file, right now
    started_at: float      # wall-clock epoch seconds when it began
    ends_at: float

    @property
    def remaining(self) -> float:
        return max(0.0, self.program.duration - self.offset)


class Channel:
    def __init__(self, number: int, name: str):
        self.number = number
        self.name = name

    def now(self, at: float) -> Airing | None:  # pragma: no cover - interface
        raise NotImplementedError


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
