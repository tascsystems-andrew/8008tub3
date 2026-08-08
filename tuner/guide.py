"""Channel 2: the listings, scrolling, with music under them.

Every cable system had one, and everybody remembers the same thing about it — a grid
crawling up the screen at its own unhurried pace while smooth jazz played, and instrumental
carols through December. It is the most-watched channel nobody chose to watch.

The implementation follows from what it actually is: **an audio track with text over it.**
mpv plays a music file; this draws the grid as an ASS overlay on top. No video is rendered,
no frames are encoded, nothing has to be rebuilt when the schedule changes — the listings
are read live and redrawn a few times a second. That also means the guide cannot go stale,
which matters, because a guide showing the wrong programme is worse than no guide.

Three details are what make it read as the real thing rather than a table:

**It scrolls continuously and slowly.** Not paged, not jumping a row at a time. The
original crawled at a pace you could not rush, and the pace is most of the character.

**Ninety minutes, three columns.** Half-hour columns starting at the current half hour.
More than that and the type gets too small to read from a sofa.

**A programme that started before the window still shows**, clipped to the left edge, with
its title — otherwise the channel you are half-watching appears blank, which is exactly
when you looked up.

The music is a folder the user points at, and it ships empty: nothing here bundles audio.
In December a `Christmas` subfolder is preferred if one exists, which is the single most
evocative detail available for the cost of one `if`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PHOSPHOR = "&H55FF33&"
GOLD = "&H3CC8FF&"          # ASS is BGR: RGB(255, 200, 60)
PURPLE = "&HF65CA8&"        # RGB(168, 92, 246)
INK = "&H101010&"
PANEL = "&H1F1A17&"
DIM = "&H999999&"
WHITE = "&HFFFFFF&"

AUDIO_SUFFIXES = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wma"}

# Layout, in the 1920x1080 space the overlay declares.
ROW_H = 96
HEADER_H = 210
LEFT_W = 300              # the channel column
COL_W = 490               # each half-hour
COLUMNS = 3               # 90 minutes
SCROLL_PX_PER_SEC = 22.0  # slow on purpose


@dataclass
class Slot:
    """One programme as the grid needs it."""

    title: str
    start: float
    end: float

    def clipped(self, window_start: float) -> bool:
        return self.start < window_start


@dataclass
class Row:
    number: int
    name: str
    slots: list[Slot] = field(default_factory=list)


# The festive season, Andrew's dates: 15 November through 1 January inclusive. Not "December"
# — a guide that turns Christmassy on the 1st of December has missed the half of the season
# that everyone actually decorates for, and one that stops on the 31st stops a day early.
FESTIVE_FROM = (11, 15)
FESTIVE_TO = (1, 1)

# Where the festive bed lives. One list, used both to find it in season and to keep it out of
# the ordinary playlist out of season — two places would drift, and the drift is silent.
FESTIVE_FOLDERS = ("Christmas", "christmas", "Xmas", "Holiday")


def is_festive(when: float | None = None) -> bool:
    stamp = datetime.fromtimestamp(time.time() if when is None else when)
    month, day = stamp.month, stamp.day
    if month == FESTIVE_FROM[0]:
        return day >= FESTIVE_FROM[1]
    if month == FESTIVE_TO[0]:
        return day <= FESTIVE_TO[1]
    # Everything strictly between the two months — December, and nothing else.
    return month > FESTIVE_FROM[0] or month < FESTIVE_TO[0]


def music_for(folder: Path | None, when: float | None = None) -> list[Path]:
    """The playlist. In the festive window, a Christmas subfolder wins if there is one.

    Returns [] when nothing is configured, and the caller plays the guide silent rather
    than refusing to show it — the listings are the point, the music is the atmosphere.
    """
    if not folder:
        return []
    root = Path(folder)
    if not root.is_dir():
        return []

    if is_festive(when):
        for name in FESTIVE_FOLDERS:
            festive = root / name
            if festive.is_dir():
                tracks = _audio_in(festive)
                if tracks:
                    return tracks

    # Recurse, but never into the festive folder — otherwise it is swept into the ordinary
    # playlist and carols turn up in July. Refusing to recurse at all would have been the
    # cheaper fix and the wrong one: it silently ignores music filed in any subfolder, which
    # is how anyone with more than a couple of tracks would organise them.
    return _audio_in(root, skip=FESTIVE_FOLDERS)


def _audio_in(folder: Path, *, skip: tuple[str, ...] = ()) -> list[Path]:
    lowered = {name.lower() for name in skip}
    try:
        return sorted(
            p for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            and not p.name.startswith(".")
            and not lowered & {part.lower() for part in p.relative_to(folder).parts[:-1]}
        )
    except OSError:
        return []


def window(now: float) -> tuple[float, float]:
    """The 90 minutes shown: from the current half hour, three columns."""
    stamp = datetime.fromtimestamp(now)
    start = stamp.replace(minute=0 if stamp.minute < 30 else 30, second=0, microsecond=0)
    begin = start.timestamp()
    return begin, begin + COLUMNS * 1800


def rows_from_lineup(lineup, now: float, guide_channel: int) -> list[Row]:
    """Read what is actually scheduled. Live, never cached.

    The guide's own channel is included and labelled, because a viewer parked on it should
    see where they are. It has no programmes, which is correct — it is always this.
    """
    begin, finish = window(now)
    rows: list[Row] = []

    for number in getattr(lineup, "numbers", []):
        # `Lineup.channels` is a list and `get` is the lookup — this asked the list for `.get`
        # and raised AttributeError on the first channel. Never seen, because the module has
        # never been called.
        channel = lineup.get(number) if hasattr(lineup, "get") else None
        name = getattr(channel, "name", f"CH {number}")
        row = Row(number=number, name=name)

        if number == guide_channel:
            rows.append(row)
            continue

        # Walk the window in steps, asking the channel what is on. Cheaper and far simpler
        # than reaching into each channel's storage, and it works for any channel type —
        # a looping channel and a scheduled one answer the same question.
        cursor = begin
        guard = 0
        while cursor < finish and guard < 40:
            guard += 1
            airing = None
            try:
                airing = lineup.now(number, cursor)
            except Exception:  # noqa: BLE001 - one bad channel must not blank the guide
                break
            if airing is None:
                cursor += 300
                continue
            title = _title_of(airing)
            # `programme_remaining`, not `remaining`: the latter counts to the next thing in
            # the plan, which during a show is its next ad break. A guide built from that
            # would chop every programme into five-minute slivers.
            #
            # Both are properties returning a float. This line used to call the result —
            # `getattr(airing, "remaining", lambda: 1800)()` — which raises TypeError against
            # any real Airing, so the first channel with a schedule would have blanked the
            # guide. Nothing has ever called this module, which is why it never surfaced.
            span = getattr(airing, "programme_remaining", None)
            if span is None:
                span = getattr(airing, "remaining", 1800.0)
            end = cursor + max(60.0, float(span))
            if row.slots and row.slots[-1].title == title:
                row.slots[-1].end = end          # same programme, extend it
            else:
                row.slots.append(Slot(title, cursor, end))
            cursor = end + 1
        rows.append(row)
    return rows


def _title_of(airing) -> str:
    program = getattr(airing, "program", None)
    name = getattr(program, "name", None) or getattr(airing, "title", None)
    if not name:
        return "—"
    return tidy(str(name))


def tidy(name: str) -> str:
    """Turn a filename into something a person would read.

    Pool symlinks carry a `folder__` prefix so two series cannot collide, and release names
    carry the entire pipeline's history. Neither belongs on screen.
    """
    if "__" in name:
        name = name.split("__", 1)[1]
    name = Path(name).stem
    for noise in ("WEBRip", "WEB-DL", "WEBDL", "BluRay", "HDTV", "DVDRip", "PDTV",
                  "1080p", "720p", "480p", "2160p", "x264", "x265", "H.264", "H264",
                  "XviD", "AAC", "AC3", "DDP", "SDTV", "REPACK", "PROPER"):
        name = name.replace(noise, " ")
    name = name.replace(".", " ").replace("_", " ")
    return " ".join(name.split())[:64] or "—"


class Guide:
    """Renders the listings. Stateless apart from the scroll clock."""

    RES = (1920, 1080)

    def __init__(self, guide_channel: int = 2, network: str = "BOOBTUBE"):
        self.guide_channel = guide_channel
        self.network = network
        self._started = time.time()

    # -- drawing helpers ---------------------------------------------------------
    @staticmethod
    def _rect(x1, y1, x2, y2, colour, alpha="&H00&") -> str:
        return (f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{colour}\\1a{alpha}\\p1}}"
                f"m {int(x1)} {int(y1)} l {int(x2)} {int(y1)} "
                f"l {int(x2)} {int(y2)} l {int(x1)} {int(y2)}{{\\p0}}")

    @staticmethod
    def _text(x, y, body, *, size, align=4, colour=WHITE, bold=0) -> str:
        safe = str(body).replace("{", "(").replace("}", ")").replace("\\", "/")
        return (f"{{\\an{align}\\pos({int(x)},{int(y)})\\fnMonospace\\fs{size}\\b{bold}"
                f"\\bord0\\shad2\\4c&H000000&\\1c{colour}}}{safe}")

    def render_ass(self, rows: list[Row], now: float | None = None) -> str:
        now = time.time() if now is None else now
        width, height = self.RES
        begin, _ = window(now)
        events: list[str] = []

        events.append(self._rect(0, 0, width, height, INK))

        # --- the grid, clipped to below the header ------------------------------
        # Drawn first so the header covers anything scrolling up behind it.
        visible_h = height - HEADER_H
        total_h = max(1, len(rows) * ROW_H)

        # Only scroll when the dial does not fit. Eleven channels at 96px sit inside 870px of
        # screen with room to spare, so scrolling a list that is entirely visible is motion
        # for its own sake — and it is expensive motion: a moving guide has to be redrawn
        # continuously, and each redraw is five kilobytes of ASS down mpv's IPC socket. At
        # four a second that filled the socket buffer, blocked the write, and took the whole
        # tuner down with a timeout. A static guide is pushed once and then left alone.
        self.scrolling = total_h > visible_h
        offset = (((now - self._started) * SCROLL_PX_PER_SEC) % total_h
                  if self.scrolling else 0.0)

        # Drawn twice when scrolling, so the wrap never shows a seam; once when it does not.
        for repeat in ((0, 1) if self.scrolling else (0,)):
            for index, row in enumerate(rows):
                y = HEADER_H + index * ROW_H - offset + repeat * total_h
                if y < HEADER_H - ROW_H or y > height:
                    continue
                events += self._row(row, y, begin)

        events += self._header(now, begin)
        return "\n".join(events)

    def _row(self, row: Row, y: float, begin: float) -> list[str]:
        events = []
        band = PANEL if row.number % 2 == 0 else "&H171310&"
        events.append(self._rect(0, y, self.RES[0], y + ROW_H - 4, band))

        # Channel number and name, in the fixed left column.
        events.append(self._rect(0, y, LEFT_W - 6, y + ROW_H - 4, "&H2A2118&"))
        events.append(self._text(24, y + ROW_H / 2 - 14, f"{row.number}",
                                 size=44, colour=GOLD, bold=1))
        events.append(self._text(96, y + ROW_H / 2 - 12, row.name[:16],
                                 size=28, colour=PHOSPHOR))

        if row.number == self.guide_channel:
            events.append(self._text(LEFT_W + 20, y + ROW_H / 2 - 12,
                                     "You are here", size=28, colour=DIM))
            return events

        if not row.slots:
            events.append(self._text(LEFT_W + 20, y + ROW_H / 2 - 12,
                                     "Off air", size=28, colour=DIM))
            return events

        for slot in row.slots:
            x1 = LEFT_W + (slot.start - begin) / 1800.0 * COL_W
            x2 = LEFT_W + (slot.end - begin) / 1800.0 * COL_W
            x1 = max(LEFT_W, x1)
            x2 = min(LEFT_W + COLUMNS * COL_W, x2)
            if x2 - x1 < 40:
                continue
            events.append(self._rect(x1 + 3, y + 6, x2 - 3, y + ROW_H - 10, "&H241E1A&"))
            # A programme already running when the window opens keeps its title, marked
            # with a leading arrow — a blank cell on the channel you are watching is the
            # one thing a guide must never show.
            label = ("< " if slot.clipped(begin) else "") + slot.title
            room = int((x2 - x1 - 26) / 15)          # monospace at size 26
            events.append(self._text(x1 + 16, y + ROW_H / 2 - 12, label[:max(4, room)],
                                     size=26, colour=WHITE))
        return events

    def _header(self, now: float, begin: float) -> list[str]:
        width = self.RES[0]
        events = [self._rect(0, 0, width, HEADER_H, "&H120E0B&")]

        stamp = datetime.fromtimestamp(now)
        events.append(self._text(36, 56, self.network, size=54, bold=1, colour=GOLD))
        events.append(self._text(36, 122, stamp.strftime("%A %-d %B"),
                                 size=30, colour=DIM))
        events.append(self._text(width - 36, 56, stamp.strftime("%-I:%M %p"),
                                 size=54, align=6, bold=1, colour=PHOSPHOR))

        # Column headings, on the half hour.
        for column in range(COLUMNS):
            slot_time = datetime.fromtimestamp(begin + column * 1800)
            x = LEFT_W + column * COL_W
            events.append(self._rect(x + 3, HEADER_H - 54, x + COL_W - 3, HEADER_H - 6,
                                     "&H2A2118&"))
            events.append(self._text(x + 16, HEADER_H - 30,
                                     slot_time.strftime("%-I:%M"), size=30,
                                     colour=GOLD, bold=1))
        events.append(self._rect(0, HEADER_H - 6, width, HEADER_H, PURPLE))
        return events
