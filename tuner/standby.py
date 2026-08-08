"""What the television shows when there is nothing to show.

The box had exactly one behaviour for an empty library: exit with a message on stderr. On a
Pi with no keyboard that means systemd restarts it every three seconds forever and the
screen keeps showing a Linux login prompt — the single most un-appliance-like thing a
television can do, and the state a first-time user is *guaranteed* to meet, because a box
with no media configured is where everyone starts.

So this is the first-boot screen, the between-jobs screen, and the progress screen. Three
rules behind it:

**Always say where to go.** The address of the settings page is the only actionable thing
on this screen, so it is the biggest thing on it after the mark.

**Say what is happening, not what went wrong.** "No channels have a schedule yet" is a
developer's sentence. A viewer needs "point this at your shows, here is where."

**Never look frozen.** Building a schedule over a network share takes minutes with no
natural progress signal, so the screen carries a moving element and a rotating line of
copy. A still screen and a hung screen are indistinguishable, and the difference decides
whether someone power-cycles the box halfway through a build.

The rotating copy is deliberately drawn from broadcast furniture — the things a real
station said between programmes — rather than quotes from television shows. It is the right
register for a channel that is pretending to be a station, it stays funny on the fiftieth
viewing rather than the second, and nothing here is anybody's script.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

PHOSPHOR = "&H55FF33&"
GOLD = "&H3CC8FF&"       # ASS is BGR: this is RGB(255, 200, 60)
PURPLE = "&HF65CA8&"     # RGB(168, 92, 246)
INK = "&H101010&"

# Broadcast idiom, not dialogue. A station says these; a sitcom does not.
STANDBY_LINES = (
    "Please stand by",
    "Do not adjust your set",
    "We are experiencing technical difficulties",
    "Normal service will resume shortly",
    "This station is now on the air",
    "Coming up next",
    "A word from our sponsors",
    "Stay tuned",
)

BUILDING_LINES = (
    "Sorting the schedule",
    "Threading the reels",
    "Cueing the commercials",
    "Timing the breaks",
    "Warming up the transmitter",
    "Aligning the heads",
    "Checking the levels",
)


@dataclass
class Standby:
    """Renders the standby card. Stateless except for the animation clock."""

    web_url: str = ""
    headline: str = "No channels yet"
    detail: str = "Open the settings page to point this at your shows."
    building: bool = False

    RES = (1920, 1080)

    def _lines(self) -> tuple[str, ...]:
        return BUILDING_LINES if self.building else STANDBY_LINES

    def render_ass(self, now: float | None = None) -> str:
        """One frame. Call repeatedly — the animation is derived from the clock.

        Vector shapes and independently positioned text, for the same reason the menu uses
        them: box-drawing characters only line up if the font renders them at exactly the
        ASCII advance width, and most fonts do not.
        """
        now = time.time() if now is None else now
        width, height = self.RES
        cx, cy = width // 2, 372
        events: list[str] = []

        def rect(x1, y1, x2, y2, colour, alpha="&H00&") -> str:
            return (f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{colour}\\1a{alpha}\\p1}}"
                    f"m {x1} {y1} l {x2} {y1} l {x2} {y2} l {x1} {y2}{{\\p0}}")

        def text(x, y, body, *, size, align=5, colour=PHOSPHOR, bold=0) -> str:
            safe = str(body).replace("{", "(").replace("}", ")").replace("\\", "/")
            return (f"{{\\an{align}\\pos({x},{y})\\fnMonospace\\fs{size}\\b{bold}"
                    f"\\bord0\\shad3\\4c&H000000&\\1c{colour}}}{safe}")

        events.append(rect(0, 0, width, height, INK, "&H00&"))

        # The mark: one filled annulus with a gap at twelve o'clock, and the centre
        # conductor in gold. Drawn rather than loaded, so the standby screen has no file
        # dependency — it has to work on a box where nothing is set up yet.
        #
        # A single closed polygon, out along the outer edge and back along the inner one.
        # The first version chained square blocks along the arc and looked exactly like
        # that on a 4K panel: a jagged bracelet. ASS has no stroke, so an annulus is the
        # way to get a smooth ring of even thickness.
        import math

        # Screen coordinates put y downwards, so 90 degrees is the BOTTOM of the circle,
        # not the top. Sweeping 140->400 therefore drew over the top and left the mouth
        # gaping at the bottom — the mark upside down. The drawn arc has to run 310->590
        # (that is, 310 round through 0, 90, 180 to 230), which leaves the 80 degrees
        # centred on 270 open, and 270 is twelve o'clock.
        outer, inner = 118, 88
        start, end = 310, 590
        def at(radius, degrees):
            angle = math.radians(degrees % 360)
            return (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

        first = at(outer, start)
        points = [f"m {first[0]:.0f} {first[1]:.0f}"]
        for degrees in range(start, end + 1, 4):
            x_, y_ = at(outer, degrees)
            points.append(f"l {x_:.0f} {y_:.0f}")
        for degrees in range(end, start - 1, -4):
            x_, y_ = at(inner, degrees)
            points.append(f"l {x_:.0f} {y_:.0f}")
        events.append(
            f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{PURPLE}\\p1}}{' '.join(points)}{{\\p0}}"
        )
        # Absolute coordinates with \an7\pos(0,0), exactly like the ring above. Drawn with
        # \an5\pos(cx,cy) the dot landed up and to the left of centre: for a drawing, ASS
        # aligns the shape's *bounding box* to the position rather than treating the
        # position as an origin, so the anchor and the geometry disagree.
        r = 34
        k = r * 0.5523                          # circle from four cubic beziers
        events.append(
            f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{GOLD}\\p1}}"
            f"m {cx - r} {cy} "
            f"b {cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r} "
            f"b {cx + k} {cy - r} {cx + r} {cy - k} {cx + r} {cy} "
            f"b {cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r} "
            f"b {cx - k} {cy + r} {cx - r} {cy + k} {cx - r} {cy}{{\\p0}}"
        )

        # No wordmark. The mark carries it, and a name under a logo on a standby screen is
        # what a product does — a television just comes on.
        events.append(text(cx, 600, self.headline, size=52))
        events.append(text(cx, 668, self.detail, size=32, colour="&HAAAAAA&"))

        # The rotating line. Four seconds each: long enough to read, short enough that the
        # screen is visibly alive if you glance up.
        line = self._lines()[int(now // 4) % len(self._lines())]
        events.append(text(cx, 776, line + "...", size=34, colour=PHOSPHOR))

        # A sweep, so the screen is never still. Position comes from the clock rather than a
        # frame counter, so it stays smooth however often this is called.
        bar_w, bar_h, bar_y = 620, 6, 836
        left = cx - bar_w // 2
        events.append(rect(left, bar_y, left + bar_w, bar_y + bar_h, "&H303030&"))
        travel = (now % 3.0) / 3.0
        knob = 150
        x = int(left + travel * (bar_w - knob))
        events.append(rect(x, bar_y, x + knob, bar_y + bar_h, PHOSPHOR))

        if self.web_url:
            events.append(text(cx, 928, self.web_url, size=44, bold=1, colour=GOLD))
            events.append(text(cx, 984, "Open this on any computer or phone on your network",
                               size=26, colour="&H888888&"))

        return "\n".join(events)
