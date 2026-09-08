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

import os
import re
import time
from datetime import datetime, timedelta
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
    "night": ("night", "midnight", "candlelit", "candlelight", "moonlit", "moonlight",
              "starry", "nocturne"),
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

# A clip is not always for one part of the day. "Morning / daytime" is a real instruction and
# there was no way to say it: the light half had to be spelled as two folders and two copies of
# the same file. A group folder says it once.
#
# Only usable as a folder name, never inferred from a title. `day` in a title is what the
# vocabulary above deliberately refuses to read, and that judgement does not change just
# because there is now a group with a similar name.
DAYPART_GROUPS = {
    "day": ("morning", "afternoon"),
    "daytime": ("morning", "afternoon"),
    "light": ("morning", "afternoon"),
    "dark": ("evening", "night"),
    "anytime": DAYPARTS,
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


def seconds_to_next_daypart(when: float | None = None) -> float:
    """How long this hour's choice of clip stays the right one.

    The box notices four o'clock on its own run loop, a quarter of a second after it passes.
    A client that is handed one clip and plays it to the end has no way to notice at all — a
    three-hour rain loop started at half past three is still raining at bedtime — so the slot
    it is given has to carry the boundary inside it.

    Built with `replace` rather than arithmetic on the epoch, so the two clock changes a year
    do not move a boundary by an hour.
    """
    at = time.time() if when is None else when
    stamp = datetime.fromtimestamp(at)
    for hour, _ in DAYPART_HOURS:
        edge = stamp.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()
        if edge > at:
            return edge - at
    first = (stamp + timedelta(days=1)).replace(
        hour=DAYPART_HOURS[0][0], minute=0, second=0, microsecond=0)
    return first.timestamp() - at


def daypart_at(when: float | None = None) -> str:
    """Which part of the day it is now."""
    hour = datetime.fromtimestamp(time.time() if when is None else when).hour
    current = DAYPART_HOURS[-1][1]          # before the first boundary is still last night
    for start, name in DAYPART_HOURS:
        if hour >= start:
            current = name
    return current


def dayparts_of(clip: Path, root: Path) -> tuple[str, ...]:
    """The parts of the day a clip is for. Empty means it never says, so it suits all of them.

    A folder beats a filename. `09/evening/rain.mp4` is a decision somebody made; a title is a
    guess we are making on their behalf, and the guess should lose when there is a decision.
    A folder can also name a group — `01/daytime/` — which a title can never do.
    """
    try:
        parents = clip.relative_to(root).parts[:-1]
    except ValueError:
        parents = ()
    for part in parents:
        folder = part.strip().lower()
        if folder in DAYPART_GROUPS:
            return DAYPART_GROUPS[folder]
        for daypart, words in DAYPART_WORDS.items():
            if folder in (daypart, f"{daypart}s") or folder in words:
                return (daypart,)
            if folder in {f"{word}s" for word in words}:
                return (daypart,)

    # A video that ships as `Paris Balcony Jazz at Night/video.mp4` carries its title on the
    # folder, which is the ordinary shape of a rip on a NAS. The exact-match pass above only
    # sees folders that ARE a daypart word, so read the folder names the same way as a title —
    # deepest first, because that is the one naming this particular clip.
    for part in reversed(parents):
        named = {d for d in DAYPARTS if _names_daypart(d, part.lower())}
        if len(named) == 1:
            return (named.pop(),)

    name = clip.stem.lower()
    found = {d for d in DAYPARTS if _names_daypart(d, name)}
    if len(found) != 1:
        # Nothing, or two. "Sunrise to Sunset" spans the whole day and belongs to no single
        # part of it, so treating it as unmarked — eligible always — is right for both cases,
        # and is better than the old behaviour of silently taking whichever word came first.
        return ()
    return (found.pop(),)


def _names_daypart(daypart: str, name: str) -> bool:
    """Whether a lowercased title names this part of the day.

    Whole words, not substrings. "night" lives inside Knightsbridge, fortnight, nightingale
    and nightshade, and matching those pinned a London rain walk and a birdsong recording to
    the small hours — hidden for two thirds of the day by a coincidence of spelling. A plain
    plural is still the same word, so `nocturnes` and `evenings` count.
    """
    tokens = set(re.findall(r"[a-z]+", name))
    for word in DAYPART_WORDS[daypart]:
        if " " in word:                     # a phrase, so look for it as written
            if word in name:
                return True
        elif word in tokens or f"{word}s" in tokens:
            return True
    return False


_ANNOUNCED = ""


def _note(message: str) -> None:
    """Log a selection once, not every time it is recomputed.

    Even cached, this is re-resolved twice a minute, and a line each time buried everything
    else in the journal — the log is where you go when something looks wrong, so it has to
    stay readable.
    """
    global _ANNOUNCED
    if message != _ANNOUNCED:
        _ANNOUNCED = message
        print(message)


def for_daypart(clips: list[Path], root: Path, when: float | None = None) -> list[Path]:
    """Narrow a month to the clips that suit this hour, or leave it alone if none do."""
    if not clips:
        return clips
    now = daypart_at(when)
    welcome = set(DAYPART_ELIGIBLE[now])
    fitting = [c for c in clips
               if not (d := dayparts_of(c, root)) or welcome.intersection(d)]
    if not fitting:
        # Every clip this month is pinned to some other hour. Play them all rather than show
        # nothing: wrong time of day is a blemish, an empty channel is a fault.
        _note(f"  ambiance: nothing suits {now}, playing all {len(clips)} clip(s)")
        return clips
    if len(fitting) != len(clips):
        _note(f"  ambiance: {now}, {len(fitting)} of {len(clips)} clip(s) suit the hour")
    return fitting


def clips_for(folder: Path | None, when: float | None = None) -> list[Path]:
    """This month's loop, narrowed to the clips that suit this time of day."""
    clips = _clips_for_month(folder, when)
    if not clips or not folder:
        return clips
    return for_daypart(clips, Path(folder), when)


def _videos_in(folder: Path, *, skip: set[str] | None = None) -> list[Path]:
    lowered = skip or set()
    found: list[Path] = []
    seen: set[str] = set()
    try:
        # `os.walk(followlinks=True)`, not `rglob`. Pathlib's `**` silently refuses to descend
        # into a symlinked directory, so a daypart folder that is a link — the obvious way to
        # share one clip across months without copying it — would read as empty, and a month
        # made entirely of links would report itself missing and borrow from another month
        # while its own clips sat one hop away. The same trap already cost us a pruning pass
        # that "tested clean and changed nothing".
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=True):
            here = Path(dirpath)
            # Following links means a cycle is possible, and a cycle here is a hung channel.
            real = os.path.realpath(dirpath)
            if real in seen:
                dirnames[:] = []
                continue
            seen.add(real)
            if lowered & {part.lower() for part in here.relative_to(folder).parts}:
                dirnames[:] = []
                continue
            for name in filenames:
                if name.startswith("."):
                    continue
                if Path(name).suffix.lower() in VIDEO_SUFFIXES:
                    found.append(here / name)
    except OSError:
        return []
    return sorted(found)
