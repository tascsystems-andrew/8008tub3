"""The ambiance channel: one thing, looping, changed by hand once a month.

A yule log is not a programme and does not want a schedule. It has no start, no end and no
episode after it — the whole appeal is that it is *always* the same thing, so tuning to it at
any hour gives you exactly what you expected. Building a timetable for that would mean
cataloguing it, generating blocks, and topping them up forever, to express "play this".

So it works like the guide: a channel that hands mpv a playlist with `loop-playlist`, and
nothing else. No catalogue, no schedule, no ffprobe — which also means it is immune to
everything that makes the rest of the dial slow when the NAS is busy.

**A folder per month.** Andrew supplies a video or two each month, so the month is the only
axis that ever changes. `12/` in December, `07/` in July, falling back to whatever sits at the
top level when a month has nothing of its own. Named folders work too, because typing
`December` is easier to get right than remembering whether it is `12` or `1`.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".mpg", ".mpeg"}

MONTH_NAMES = ("january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december")


def month_folders(when: float | None = None) -> tuple[str, ...]:
    """The names this month's folder might have, most specific first."""
    stamp = datetime.fromtimestamp(time.time() if when is None else when)
    name = MONTH_NAMES[stamp.month - 1]
    return (f"{stamp.month:02d}", str(stamp.month), name, name.capitalize(), name.upper())


def clips_for(folder: Path | None, when: float | None = None) -> list[Path]:
    """This month's loop, or the general one.

    Returns [] when nothing is configured. The caller then leaves the channel off the dial
    entirely rather than offering a channel that plays nothing — an ambiance channel with no
    ambiance is worse than no channel, because it looks like a fault.
    """
    if not folder:
        return []
    root = Path(folder)
    if not root.is_dir():
        return []

    for name in month_folders(when):
        candidate = root / name
        if candidate.is_dir():
            found = _videos_in(candidate)
            if found:
                return found

    # Nothing for this month: anything at the top level, ignoring the month folders so
    # December's fire does not turn up in June.
    skip = {n.lower() for m in range(1, 13)
            for n in (f"{m:02d}", str(m), MONTH_NAMES[m - 1])}
    return _videos_in(root, skip=skip)


def _videos_in(folder: Path, *, skip: set[str] | None = None) -> list[Path]:
    lowered = skip or set()
    try:
        return sorted(
            p for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
            and not p.name.startswith(".")
            and not lowered & {part.lower() for part in p.relative_to(folder).parts[:-1]}
        )
    except OSError:
        return []
