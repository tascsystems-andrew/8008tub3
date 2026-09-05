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

**And a part of the day.** A month's clips are not interchangeable: "Paris Balcony Jazz at
Night" is wrong at half past four in the afternoon, which is exactly what it did. So a clip
whose name pins it to a time of day is only offered at that time of day, and one that says
nothing is offered always. If nothing fits the hour, the whole month plays anyway — a
seasonally right clip at the wrong hour still beats a channel with nothing on it.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".mpg", ".mpeg"}

MONTH_NAMES = ("january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december")

DAYPARTS = ("morning", "afternoon", "evening", "night")

# Words that pin a clip to a part of the day.
#
# Deliberately narrow. "day" is NOT here: "Rainy Day in a Cozy Room" and "Start A New Day" are
# about weather and encouragement, not the clock, and a false match is worse than no match —
# it would hide a clip that was fine at any hour. Anything unrecognised stays unmarked, and
# unmarked means always eligible, so the cost of omitting a word is far lower than the cost of
# guessing one wrong.
DAYPART_WORDS = {
    "morning": ("morning", "sunrise", "dawn", "daybreak"),
    "afternoon": ("afternoon", "midday", "noon"),
    "evening": ("evening", "sunset", "dusk", "twilight", "golden hour"),
    "night": ("night", "midnight", "candlelit", "candlelight", "moonlit", "starry", "nocturne"),
}

# Fixed hours rather than real sunrise and sunset. Actual daylight would be more correct in
# June and December, but it needs a location and an almanac to answer "is it evening", and the
# whole point of this channel is that it answers instantly and predictably.
#
# Evening starts at four, not five. Andrew's own calibration: half past four reads as dusk,
# two o'clock plainly does not. That is generous in June and about right for the darker two
# thirds of the year, which is when anyone actually wants a candlelit coffee house on.
DAYPART_HOURS = ((5, "morning"), (11, "afternoon"), (16, "evening"), (21, "night"))

# Which clips suit which hour. Not simply "the matching one": the dark half of the day pools,
# because a month usually holds exactly one dark clip and splitting dusk from night would
# leave whichever half missed out with nothing seasonal to play. So a clip named for the night
# is welcome from four in the afternoon, and a sunset still works at ten.
#
# The light half stays strict. That is where the complaint came from — "Paris Balcony Jazz at
# Night" at two in the afternoon — and morning and afternoon each have enough candidates that
# borrowing is not needed.
DAYPART_ELIGIBLE = {
    "morning": ("morning",),
    "afternoon": ("afternoon",),
    "evening": ("evening", "night"),
    "night": ("night", "evening"),
}


def month_folders(when: float | None = None) -> tuple[str, ...]:
    """The names this month's folder might have, most specific first."""
    stamp = datetime.fromtimestamp(time.time() if when is None else when)
    name = MONTH_NAMES[stamp.month - 1]
    return (f"{stamp.month:02d}", str(stamp.month), name, name.capitalize(), name.upper())


def _clips_for_month(folder: Path | None, when: float | None = None) -> list[Path]:
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
    general = _videos_in(root, skip=skip)
    if general:
        return general

    # Still nothing, so borrow from the nearest month that has something rather than leave
    # the dial a channel short.
    #
    # The channel vanishing is the worse failure. A viewer does not read it as "there is no
    # September folder", they read it as the box having lost a channel — which is what
    # happened: August ended, nothing had been put in `09`, and the ambiance channel simply
    # stopped existing with nothing anywhere saying why.
    #
    # Nearest by distance round the year, so October borrows from September before it
    # borrows from March, and the substitution is at least seasonally adjacent.
    stamp = datetime.fromtimestamp(time.time() if when is None else when)
    others = []
    for month in range(1, 13):
        if month == stamp.month:
            continue
        distance = min((month - stamp.month) % 12, (stamp.month - month) % 12)
        for name in (f"{month:02d}", str(month), MONTH_NAMES[month - 1],
                     MONTH_NAMES[month - 1].capitalize(), MONTH_NAMES[month - 1].upper()):
            candidate = root / name
            if candidate.is_dir():
                found = _videos_in(candidate)
                if found:
                    others.append((distance, month, found))
                break
    if others:
        others.sort(key=lambda item: (item[0], item[1]))
        distance, month, found = others[0]
        print(f"  ambiance: nothing for {MONTH_NAMES[stamp.month - 1]}, "
              f"using {MONTH_NAMES[month - 1]} ({len(found)} clip(s))")
        return found
    return []


def daypart_at(when: float | None = None) -> str:
    """Which part of the day it is now."""
    hour = datetime.fromtimestamp(time.time() if when is None else when).hour
    current = DAYPART_HOURS[-1][1]          # before the first boundary is still last night
    for start, name in DAYPART_HOURS:
        if hour >= start:
            current = name
    return current


def daypart_of(clip: Path, root: Path) -> str | None:
    """The part of the day a clip is for, or None if it never says.

    A folder beats a filename. `09/evening/rain.mp4` is a decision somebody made; a title is a
    guess we are making on their behalf, and the guess should lose when there is a decision.
    """
    try:
        parents = clip.relative_to(root).parts[:-1]
    except ValueError:
        parents = ()
    for part in parents:
        folder = part.strip().lower()
        for daypart, words in DAYPART_WORDS.items():
            if folder == daypart or folder in words:
                return daypart

    name = clip.stem.lower()
    for daypart in DAYPARTS:                # fixed order, so two markers resolve the same way twice
        if any(word in name for word in DAYPART_WORDS[daypart]):
            return daypart
    return None


def for_daypart(clips: list[Path], root: Path, when: float | None = None) -> list[Path]:
    """Narrow a month to the clips that suit this hour, or leave it alone if none do."""
    if not clips:
        return clips
    now = daypart_at(when)
    welcome = DAYPART_ELIGIBLE[now]
    fitting = [c for c in clips if (d := daypart_of(c, root)) is None or d in welcome]
    if not fitting:
        # Every clip this month is pinned to some other hour. Play them all rather than show
        # nothing: wrong time of day is a blemish, an empty channel is a fault.
        print(f"  ambiance: nothing suits {now}, playing all {len(clips)} clip(s)")
        return clips
    if len(fitting) != len(clips):
        print(f"  ambiance: {now}, {len(fitting)} of {len(clips)} clip(s) suit the hour")
    return fitting


def clips_for(folder: Path | None, when: float | None = None) -> list[Path]:
    """This month's loop, narrowed to the clips that suit this time of day."""
    clips = _clips_for_month(folder, when)
    if not clips or not folder:
        return clips
    return for_daypart(clips, Path(folder), when)


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
