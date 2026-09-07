"""The appliance loop: clock, player, input, menu.

Deliberately small. The scheduling engine is FieldStation42's, the player is mpv, the input
devices are the kernel's. What lives here is the thing none of them do: turn four button
presses into a television.

Two modes, because four buttons is the whole vocabulary:

    WATCH   UP/DOWN change channel   SELECT opens the menu   BACK shows the bug
    MENU    UP/DOWN move the cursor  SELECT activates        BACK goes up a level

BACK is a shortcut everywhere it appears, never a requirement. No clicker has a button
labelled anything like it — on the Elan it is the long-press of the down arrow — so every
menu screen carries its own `Back` item and the whole tree is navigable with three verbs.

There is no spinner and no "loading" state anywhere, but there is a deliberate split between
*answering the button* and *changing the picture*. The number goes up the instant a button is
pressed, on the outgoing channel's picture, and the file opens afterwards. That is what a
television did, and it is why changing channel felt immediate on hardware far slower than
this. A box that shows nothing until the picture is ready feels broken at any speed.

The tune itself is deferred by `Box.SETTLE` after the last press, because the interaction
being designed for is rapid-fire clicks — clack-clack-clack up the dial — and every channel
passed through at speed is a file nobody is going to watch.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .input import Driver, Event, Verb
from .menu import Menu, build_root
from .player import MpvPlayer
from .schedule import Airing, Lineup

# Set TUB3_VOLUME_TRACE=1 to log every volume key from arrival to the wire. Volume is the one
# path where the interesting failures are about *rate* — dropped repeats, a bus falling
# behind, a set that stops answering partway through a hold — and none of those are visible
# in a still picture of the code.
VOLUME_TRACE = bool(os.environ.get("TUB3_VOLUME_TRACE"))


class Mode(Enum):
    WATCH = "watch"
    MENU = "menu"


@dataclass
class BugState:
    """The translucent channel identifier after a change. Fades, never blocks."""

    airing: Airing | None = None
    shown_at: float = 0.0
    duration: float = 4.0

    @property
    def visible(self) -> bool:
        return self.airing is not None and (time.monotonic() - self.shown_at) < self.duration


def render_bug(airing: Airing) -> str:
    """The channel identifier, the way a CRT drew it.

    Two blocks, deliberately far apart. The channel number goes top right and is enormous —
    that is what a television actually did, and it is the one thing readable from across a
    room while you are still pressing the button. Everything else goes bottom right at a
    normal size, because it is for someone who has already stopped and is now curious.

    The programme name comes from `tuner.titles`, which resolves the file against the Plex
    library at build time. A filename is not a title, and a television showing
    `Bill.Nye.-..The.Science.Guy.S04E12.SDTV.Ocean.Life` undoes a lot of other work.
    """
    from .titles import describe

    # Zero-padded, like every set-top box and every CRT tuner: CH 03, not CH 3.
    channel = (
        r"{\an9\pos(1840,54)\fnmonospace\fs150\b1\bord0\shad6"
        r"\1c&H55FF33&\4c&H000000&}"
        f"CH {airing.channel:02d}"
    )

    minutes_left = int(airing.programme_remaining // 60)
    remaining = f"{minutes_left} min left" if minutes_left else "ending"
    show, detail = describe(airing.feature_path)

    lines = [f"{{\\b1}}{airing.channel_name}{{\\b0}}", show]
    if detail:
        lines.append(f"{{\\fs26\\1c&HAAAAAA&}}{detail}{{\\fs34\\1c&H55FF33&}}")
    lines.append(remaining)

    info = (
        r"{\an3\pos(1840,1020)\fnmonospace\fs34\bord0\shad3"
        r"\1c&H55FF33&\4c&H000000&}"
    ) + r"\N".join(lines)

    return channel + "\n" + info


def render_menu_now(airing: Airing) -> str:
    """One line under the menu saying what is still playing behind it.

    The menu covers the picture, so opening it used to mean losing track of what you were
    watching — which matters most in the one place you would want to know, the channel list.
    The bug cannot simply be left up: it sits top-right and bottom-right, where the panel is,
    and it fades after four seconds anyway.

    So this is its own strip, in the margin below the panel (which ends at y=960), and it
    does not fade — while the menu is open, what is on stays on.
    """
    from .titles import describe

    show, detail = describe(airing.feature_path)
    minutes = int(airing.programme_remaining // 60)
    parts = [f"CH {airing.channel:02d}", airing.channel_name, show]
    if detail:
        parts.append(detail)
    parts.append(f"{minutes} min left" if minutes else "ending")
    # Braces and backslashes are ASS control characters, and an episode title is user data.
    body = "   ·   ".join(parts).replace("{", "(").replace("}", ")").replace("\\", "/")
    return (
        r"{\an2\pos(960,1024)\fnMonospace\fs30\b0\bord0\shad3"
        r"\4c&H000000&\1c&H55FF33&}" + body
    )


def render_menu_health(verdict: dict) -> str:
    """A single line at the top of the menu when the box is quietly broken.

    On the menu and nowhere else. The dial degrades so gracefully that a failing build shows
    no symptom for a day or more, and the one place to say so is the screen someone opened on
    purpose — not the picture, which would put a maintenance notice in front of a family
    watching television, and not a fade-away toast nobody would be looking at.

    Deliberately one line and deliberately terse. It says that something is wrong and where to
    read the detail; the settings page has room for the rest, and this does not.
    """
    messages = verdict.get("messages") or []
    if not messages:
        return ""
    body = messages[0]
    if len(messages) > 1:
        body += f"   (+{len(messages) - 1} more)"
    # Amber for a schedule running down, red for a build that failed outright.
    colour = "&H5A6EFF&" if verdict.get("level") == "fault" else "&H3CB4FF&"
    # The box's own name, not a literal — this box answers to `boobtube.local` today, but a
    # line telling you to visit the wrong host is worse than one telling you nothing.
    try:
        import socket  # noqa: PLC0415
        host = socket.gethostname().split(".")[0] or "boobtube"
    except Exception:  # noqa: BLE001
        host = "boobtube"
    body = f"! {body}   ·   {host}.local:8008"
    body = body.replace("{", "(").replace("}", ")").replace("\\", "/")
    return (
        r"{\an8\pos(960,26)\fnMonospace\fs28\b1\bord0\shad3"
        r"\4c&H000000&\1c" + colour + "}" + body
    )


def render_tuning(label: str, name: str = "") -> str:
    """The half of the bug that needs no disk: the number, and the network if we know it.

    Drawn the instant a button is pressed, before anything is opened. Both blocks sit exactly
    where `render_bug` puts them, so when the programme details arrive they fill in underneath
    rather than shifting what is already on screen.

    `label` is passed in rather than formatted from a number because it also carries the
    half-typed case — `CH 1_` while the box waits to find out whether that meant 1 or 12.
    """
    channel = (
        r"{\an9\pos(1840,54)\fnmonospace\fs150\b1\bord0\shad6"
        r"\1c&H55FF33&\4c&H000000&}"
        f"{label}"
    )
    if not name:
        return channel

    info = (
        r"{\an3\pos(1840,1020)\fnmonospace\fs34\bord0\shad3"
        r"\1c&H55FF33&\4c&H000000&}"
        f"{{\\b1}}{name}{{\\b0}}"
    )
    return channel + "\n" + info


class Box:
    def __init__(
        self,
        lineup: Lineup,
        player: MpvPlayer,
        *,
        start_channel: int | None = None,
        state: dict | None = None,
        rescan: Callable[[], list] | None = None,
        power: Callable[[bool], None] | None = None,
        volume: Callable[[str], None] | None = None,
        tv_state: Callable[[], str] | None = None,
        take_input: Callable[[], None] | None = None,
        select_input: Callable[[int], None] | None = None,
        other_port: int | None = None,
        our_address: str | None = None,
    ):
        self.lineup = lineup
        self._rescan_for = rescan
        self._rescanned_at = 0.0
        # Injected rather than imported, so `tuner` keeps not depending on `tub3`. Takes one
        # argument: whether the television should now be on.
        self._power = power
        # Whether the television is on, as the television sees it. Injected for the same
        # reason as the others: `tuner` does not import `tub3`.
        self._tv_state = tv_state
        # Announcing ourselves as the active source, without touching the set's power — the
        # message that switches inputs and nothing else.
        self._take_input = take_input
        # The mirror of it: telling the set to show something else. Together they make
        # one button that swaps the television between this box and whatever else is
        # plugged in, which is what SOURCE means on the remote it is printed on.
        self._select_input = select_input
        # Which socket everything else is plugged into. Ours is read from EDID;
        # this one has to be told, because nothing on the bus reliably says.
        self._other_port = other_port
        self._handover_pending = False
        # Our physical address, as the television numbers its ports. Compared against
        # whatever the bus last announced, to answer "are we what is showing?".
        self._our_address = our_address
        # The CEC driver, once running, so the power logic can read what it has heard. Found
        # among the drivers rather than constructed here, because `tuner` does not know how
        # to build one.
        self._cec_driver = None
        # Same arrangement for volume, which is the television's to set and not the player's.
        # Takes a CEC ui-cmd name.
        self._volume = volume
        # Volume goes out on its own thread, because a `cec-ctl` invocation costs about
        # 150ms and the input thread cannot wait on a television.
        #
        # Not a queue of presses, which is what this was and why holding barely worked. The
        # remote autorepeats at roughly 20/s; a queue of individual press-and-release pairs
        # could retire about three of those a second, so the ramp crawled and then kept
        # crawling after the thumb came off. What is kept instead is the *latest intent* and
        # when it was last heard — the worker repeats it at whatever rate the bus manages,
        # and stops when the repeats stop arriving. Overshoot is then one message, not a
        # backlog, and the ramp speed is the bus's rather than the remote's.
        self._volume_cmd: str | None = None
        self._volume_seen = 0.0
        self._volume_holding = False
        self._volume_seq = 0
        self.asleep = False
        self._power_pending = False
        # Set when a power press should consult the set before deciding. Cleared once the run
        # loop has done so.
        self._power_query = False
        # The last few presses, as (verb, when, channel-before-it-was-acted-on), for matching
        # the unlock code against. Bounded by the code's own length, so it costs one tuple per
        # press and never grows.
        self._recent: deque[tuple[Verb, float, int]] = deque(maxlen=len(self.UNLOCK))
        self._unlock_channel = 0
        self._wake_pending = False
        # An AirPlay session owns the screen. Unlike standby, this is not something the
        # viewer asked the box for — a phone decided — so the box's job is to get out of the
        # way quickly and take the screen back the moment the session ends.
        self.casting = False
        self._cast = None
        # A video a phone told us to play. Not a channel: it has no schedule, nothing
        # follows it, and `self.channel` keeps pointing at where the dial was so that
        # stopping returns the viewer there.
        self.cast_video: str | None = None
        self._cast_bug_at = 0.0
        self.player = player
        self.mode = Mode.WATCH
        self.menu = Menu(build_root(state or {}))
        self.bug = BugState()
        self.channel = start_channel or (lineup.numbers[0] if lineup.numbers else 0)
        # Where the dial was before this channel, for PREV.CH. Deliberately recorded in
        # select() and not in tune(): tune() is also how the schedule moves to the next
        # programme on the channel you are already watching, and counting that as a
        # channel change would make PREV.CH mean "the channel I am on".
        self._previous_channel: int | None = None
        self.running = False
        self._lock = threading.Lock()
        self._pending_digits = ""
        self._digit_deadline = 0.0
        self.last_latency_ms: float = 0.0
        self._guide = None
        self._guide_rows: list = []
        self._guide_rows_at = 0.0
        self._guide_ass = None
        self._guide_pushed_at = 0.0
        self._looping = False
        # What mpv actually has open, as opposed to where the viewer thinks they are. The two
        # differ for as long as a settle window, and after a burst they may turn out to agree
        # — up then down again is a change of mind, not a channel change.
        self._on_air: int | None = None
        # Which part of the day the ambiance playlist was chosen for, so the loop can notice
        # the hour moving on underneath a channel nobody has touched since breakfast.
        self._ambiance_daypart: str | None = None
        # The health verdict, refreshed on the run loop's own tick. Never computed while
        # drawing: see `_refresh_health`.
        self._health: dict = {}
        self._settle_at = 0.0
        self._tune_seq = 0
        self._tuning = ""
        self._tuning_name = ""

    # How long a set of listings rows stays good for. Rebuilding them queries every channel's
    # schedule across ninety minutes; the scroll is redrawn far more often than this, because
    # that part is arithmetic.
    GUIDE_ROWS_TTL = 20.0

    # How long the box waits after the last press before it opens anything. Long enough to
    # swallow a burst of clicks at the speed a child produces them, short enough that a single
    # press is not perceptibly deferred — and the number is already on screen throughout, so
    # this window costs nothing anybody can see.
    SETTLE = 0.22

    # How long a partial channel number waits for a second digit. Only ever paid by a prefix
    # of a longer channel: with 2-13 on the dial, `7` can only mean 7 and commits at once.
    DIGIT_WAIT = 1.0

    # Two clocks. The settle timer needs fine resolution or the deferral meant to remove the
    # drag becomes the drag; everything else in the loop talks to mpv over IPC and must not
    # run at that rate — flooding that socket is what took the tuner down once already.
    TICK = 0.05
    HOUSEKEEPING = 0.25

    # How often the dial is re-read. Channels arrive *while the box is on*: a schedule build
    # runs for hours and finishes one station at a time, so the dial legitimately grows over
    # an evening. Until now that needed a restart to show up — the box discovered its channels
    # once and never looked again — which is a strange thing to ask of a television, and it
    # showed up exactly as you would expect: two stations finished building, and neither the
    # dial nor the guide knew they existed.
    RESCAN = 60.0

    # The way back when the television is on the wrong input and its own remote is lost.
    #
    # The box cannot simply wake the set whenever it feels like it — `tub3.cec.tv_on` says so
    # in its own docstring, and it is right: an appliance that can turn a television on by
    # itself eventually does it at 3am. So this is the compromise. It is a deliberate act on
    # the remote in your hand, it cannot happen by accident, and it needs nothing on screen to
    # find — which matters, because when you need it the screen is showing something else.
    #
    # Every press still does its normal job on the way through, so a partial code is
    # indistinguishable from ordinary surfing. On completion the channel you started on is
    # restored and the menu is suppressed, so the code leaves no trace but the television
    # coming back.
    UNLOCK = (Verb.UP, Verb.UP, Verb.DOWN, Verb.DOWN, Verb.UP, Verb.UP, Verb.SELECT)

    # Long enough to be typed deliberately at a dark screen, short enough that six presses
    # spread across an evening's surfing never accumulate into it.
    UNLOCK_GAP = 2.5

    # How long a hold keeps running after the remote stops reporting it — and it is this
    # large because the remote is lying.
    #
    # The clicker is an ELAN wireless presenter: a slide-advance device whose dongle releases
    # the key on its own after about 700ms whether or not a thumb is still on it. Captured
    # during a deliberate five-second hold:
    #
    #     17:08:57.289  down
    #     17:08:57.548  REPEAT        (every 40ms, kernel autorepeat, working correctly)
    #     17:08:57.988  REPEAT
    #     17:08:57.993  up            <- 704ms in, button still physically down
    #
    # Every "it only goes up three dashes" was this, and the television was never at fault:
    # at 300ms a step, 704ms buys three steps. Tuning the rate could not fix it, which is why
    # four attempts at tuning the rate did not.
    #
    # So a hold runs on past the phantom release. Repeats are the signal that a hold was
    # *intended* — they only start 250ms in, so a genuine short press never produces one and
    # is still exactly one step. What this buys is a hold that behaves like a hold; what it
    # costs is that letting go early still spends the rest of the run.
    # Conservative until the remote's behaviour is confirmed. Running a hold on past the
    # reported release would compensate for a lying remote, but if the remote is telling the
    # truth it is just volume moving on its own after the button is up.
    VOLUME_HOLD_GAP = 0.25

    # The longest a volume key is ever held on the bus, regardless of what the remote says.
    # A held CEC key ramps until it is released, so a release that goes missing leaves the
    # television running to the end of its range on its own — which it did. Five seconds is
    # longer than any deliberate hold and short enough that the worst case is survivable.
    VOLUME_MAX_HOLD = 5.0

    # How often a held key is re-sent. This television steps once per `<User Control
    # Pressed>` and does not ramp on its own — confirmed from a trace: one press held for two
    # seconds moved the volume exactly one step — so a ramp has to be a stream, and the only
    # question is how fast.
    #
    # Not as fast as the bus allows. At the bus rate of about eleven a second the volume
    # crossed its whole range in a few seconds and read as a control that would not stop.
    # Five a second gives roughly ten steps for a two-second hold, which is about what a
    # television's own remote does.
    VOLUME_STEP = 0.20

    # Minimum time between key events while a button is held, and it exists because the
    # television has a queue.
    #


    # ---------- tuning ----------

    def select(self, channel: int) -> None:
        """A viewer changing channel. Feedback now, television shortly.

        Everything here is arithmetic and one overlay write, so it returns in well under a
        millisecond and can be called as fast as a thumb can move. The file is opened later,
        by the run loop, once `SETTLE` has passed with no further press.

        Two things fall out of that, and the second is the one that makes it feel right:

        - Eight presses up the dial open one file instead of eight. The box stops loading
          channels nobody is stopping on.
        - The number on screen keeps up with the button. The tuner obviously cannot, and the
          whole trick of a dial that feels fast is that the display never admits it.

        Called for every deliberate change — surfing and direct entry both. Not called when
        the schedule steps to the next programme, which is not a channel change and must not
        pay a settle.
        """
        station = self.lineup.get(channel)
        if self.cast_video:
            # Pressing a channel button is the plainest statement there is that the
            # viewer wants television back, so it wins over the phone.
            self.cast_video = None
        if channel != self.channel:
            self._previous_channel = self.channel
        self.channel = channel
        self._settle_at = time.monotonic() + self.SETTLE
        self._tune_seq += 1
        self._tuning = f"CH {channel:02d}"
        self._tuning_name = getattr(station, "name", "") if station else ""
        self._redraw()

    def tune(self, channel: int, *, announce: bool = True) -> None:
        """Put a channel on screen.

        `announce` is what separates the two reasons this gets called. A viewer pressing
        up/down wants to be told where they landed; the box stepping to the next programme
        on the channel they are already watching does not — that is the same channel it was
        a moment ago, and saying so every time a programme starts is the box talking to
        itself. Nothing in the schedule is a channel change.

        This is the slow half — it opens a file — so it runs on the run loop's thread and
        never under the lock. `select` is what input calls.
        """
        seq = self._tune_seq
        station = self.lineup.get(channel)
        if getattr(station, "is_guide", False):
            self._tune_guide(channel, announce=announce)
            return
        if getattr(station, "is_ambiance", False):
            self._tune_ambiance(channel, announce=announce)
            return

        # Leaving any looping channel: undo it, or the next channel inherits `loop-playlist`
        # and repeats one programme forever instead of advancing.
        #
        # Tracked as a flag rather than inferred from `self._guide`. That worked while the
        # guide was the only looping channel and silently stopped working the moment ambiance
        # arrived — leaving channel 13 for a scheduled one left the loop set, and the failure
        # would show up as a programme that never ends, a long way from the code that caused
        # it.
        if self._looping:
            self.player.clear_loop()
            self._looping = False
        if self._guide is not None:
            self.player.hide_overlay(overlay_id=4)
            self._guide = None
            self._guide_ass = None

        airing = self.lineup.now(channel, time.time())
        if airing is None or airing.off_air:
            # Dead air. A real station showed a sign-off card rather than a black screen.
            self.channel = channel
            self._on_air = channel
            self._tuning = ""
            self.bug = BugState()
            # The card is the whole screen, so the tuning number has to come down with it —
            # otherwise the last thing drawn before the card is left sitting on top of it.
            self.player.hide_overlay(overlay_id=3)
            self.player.show_overlay(
                r"{\an5\pos(960,540)\fnmonospace\fs42\1c&H55FF33&}"
                f"CHANNEL {channel}\\NOFF AIR", overlay_id=2,
            )
            return

        self.player.hide_overlay(overlay_id=2)
        # `seek`, never `offset`. A programme interrupted by a mid-roll appears twice in the
        # plan and its second half carries a non-zero `skip` — punching in at `offset` alone
        # would restart it from the top of the file.
        result = self.player.tune(airing.program.path, airing.seek,
                                  duration=airing.program.duration)
        self.last_latency_ms = result.latency_ms
        self._on_air = channel
        if seq != self._tune_seq:
            # A press landed while the file was opening, and it owns the screen now — its own
            # tune is already queued behind this one. Writing this channel's bug here would put
            # the wrong number up for a fifth of a second, which is worse than the drag it was
            # meant to cure.
            return
        self.channel = channel
        self._tuning = ""
        # Not announcing: keep the old timestamp so the bug stays however faded it already
        # was, rather than resetting its clock. Carrying the *airing* forward still matters,
        # because a BACK press must describe what is on now, not what was on before.
        shown_at = time.monotonic() if announce else self.bug.shown_at
        self.bug = BugState(airing=airing, shown_at=shown_at)
        self._redraw()

    def _tune_ambiance(self, channel: int, *, announce: bool = True) -> None:
        """Put the loop up. No schedule, no catalogue, no backdrop — the video is the picture.

        The bug is kept, unlike the guide. This looks like an ordinary channel, so someone
        landing on it should be told where they are in the ordinary way.
        """
        station = self.lineup.get(channel)
        from .ambiance import daypart_at  # noqa: PLC0415
        self._ambiance_daypart = daypart_at()
        # Leaving the guide behind, if that is where we came from.
        if self._guide is not None:
            self.player.hide_overlay(overlay_id=4)
            self._guide = None
            self._guide_ass = None
        self.player.clear_loop()

        clips = list(getattr(station, "clips", []) or [])
        airing = self.lineup.now(channel, time.time())
        if not clips:
            # Configured but empty — say so rather than showing black and looking broken.
            self.channel = channel
            self._on_air = channel
            self._tuning = ""
            self.bug = BugState()
            self.player.hide_overlay(overlay_id=3)
            self.player.show_overlay(
                r"{\an5\pos(960,540)\fnmonospace\fs42\1c&H55FF33&}"
                f"CHANNEL {channel}\\NNOTHING LOADED", overlay_id=2,
            )
            return

        self.player.hide_overlay(overlay_id=2)
        self.player.play_loop(clips)
        self._looping = True
        self.channel = channel
        self._on_air = channel
        self._tuning = ""
        shown_at = time.monotonic() if announce else self.bug.shown_at
        self.bug = BugState(airing=airing, shown_at=shown_at)
        self._redraw()

    def _tune_guide(self, channel: int, *, announce: bool = True) -> None:
        """Put the listings up, with music under them.

        The bug is deliberately not shown here. It exists to tell you what channel you are on
        and what is playing, and the guide is a full screen already saying both — the row for
        channel 2 is highlighted and the header carries the network name.
        """
        from .guide import Guide, rows_from_lineup

        station = self.lineup.get(channel)
        self.player.hide_overlay(overlay_id=2)
        self.player.hide_overlay(overlay_id=3)
        # A picture first, always. The listings are an overlay, so without something behind
        # them the screen keeps showing whatever channel you came from — and a guide with no
        # music must still look like a guide, not like a failed channel change.
        music = list(getattr(station, "music", []) or [])
        if music:
            self.player.play_loop(music, backdrop=True)
        else:
            self.player.show_backdrop()
        self._looping = True
        self.channel = channel
        self._on_air = channel
        self._tuning = ""
        self.bug = BugState()
        self._guide = Guide(guide_channel=channel,
                            network=getattr(station, "name", "BOOBTUBE"))
        self._guide_rows = []
        self._guide_rows_at = 0.0
        self._guide_ass = None
        self._guide_pushed_at = 0.0
        self._redraw_guide()

    def _redraw_guide(self) -> None:
        """One frame of the listings.

        The rows are recomputed on a slow timer and the scroll on every tick. Rebuilding rows
        means asking every channel what is on across a 90-minute window, which is a schedule
        query per channel per step — far too much to repeat four times a second, and it cannot
        change meaningfully between two ticks anyway. The scroll position is arithmetic.
        """
        if self._guide is None:
            return
        from .guide import rows_from_lineup

        now = time.time()
        if not self._guide_rows or now - self._guide_rows_at > self.GUIDE_ROWS_TTL:
            try:
                self._guide_rows = rows_from_lineup(self.lineup, now, self.channel)
            except Exception:  # noqa: BLE001 - a broken row must not blank the screen
                self._guide_rows = self._guide_rows or []
            self._guide_rows_at = now
        if not self._guide_rows:
            return

        try:
            ass = self._guide.render_ass(self._guide_rows, now)
        except Exception:  # noqa: BLE001 - a listings glitch must not take the box down
            return

        # Pushed on the animation's own clock, not the tick. Each frame carries `\move` tags
        # describing the next `Guide.WINDOW` seconds, so libass keeps the grid moving between
        # pushes and there is nothing to gain from sending more often — which is the whole
        # reason the listings can scroll continuously without refilling mpv's IPC buffer.
        #
        # Sent slightly before the window expires so the next frame is in place before the
        # last one finishes, otherwise the scroll stalls for a tick at every boundary.
        due = now - self._guide_pushed_at >= self._guide.WINDOW * 0.9
        if not due and ass == self._guide_ass:
            return
        self._guide_ass = ass
        self._guide_pushed_at = now
        self.player.show_overlay(ass, overlay_id=4)

    def show_bug_again(self) -> None:
        """What am I watching. The channel number, and the programme under it.

        Looks the airing up rather than re-showing `self.bug`, and it has to: the run
        loop sets `bug.airing = None` once the bug fades, so four seconds after a
        channel change there is nothing left to re-show. Guarding on `bug.airing`, as
        this did, therefore meant the button worked only while the bug was already on
        screen — which is precisely when nobody presses it.

        Looking it up is also the more honest answer. The schedule steps on while you
        sit there, so the programme that was on when you tuned in is not necessarily
        the one playing now, and the menu already looks it up for that reason.
        """
        if self._guide is not None:
            return          # the listings are the picture here; a bug would be
                            # furniture on top of furniture
        try:
            airing = self.lineup.now(self.channel, time.time())
        except Exception:  # noqa: BLE001 - never let a schedule query blank the screen
            airing = self.bug.airing
        if airing is None:
            return
        self.bug = BugState(airing=airing, shown_at=time.monotonic())
        self._redraw()

    def surf(self, delta: int) -> None:
        self.select(self.lineup.surf(self.channel, delta))

    def jump_back(self) -> None:
        """PREV.CH. The button every television has had since remotes had wires.

        Pressed twice it returns you to where you started, because `select` records
        the outgoing channel on the way past — so the pair of channels swaps rather
        than the history growing. That is what the button does on a real set, and a
        deeper history would be a different, worse button.
        """
        if self._previous_channel is None or self._previous_channel == self.channel:
            return
        self.select(self._previous_channel)

    def jump_to_guide(self) -> None:
        """Straight to the listings, wherever they sit on the dial.

        Asks the lineup rather than assuming channel 2: the number lives in one place
        and this is not it.
        """
        for station in self.lineup.channels:
            if getattr(station, "is_guide", False):
                if station.number != self.channel:
                    self.select(station.number)
                return

    # ---------- volume ----------

    def _send_volume(self, ui_cmd: str, fallback: int | None, repeat: bool = False) -> None:
        """Record what the volume button currently wants; the worker does the talking.

        Called on the input thread holding the lock, so it does nothing that waits. The
        fallback exists for `--windowed` on a desktop, where there is no CEC device and
        software gain is the only volume there is.
        """
        if self._volume is None:
            if fallback is None:
                self.player.toggle_mute()
            else:
                self.player.nudge_volume(fallback)
            return
        if VOLUME_TRACE:
            print(f"  vol: <- {ui_cmd}{' (held)' if repeat else ''}", flush=True)
        self._volume_cmd = ui_cmd
        self._volume_seen = time.monotonic()
        # A fresh key-down is a tap until its own repeats say otherwise — which is also what
        # makes changing direction mid-hold work: pressing up while down was held arrives as
        # a key-down, so the hold ends rather than silently continuing in the new direction.
        self._volume_holding = bool(repeat)
        # Bumped on every press so the worker can tell "nothing has happened since I started
        # this message" from "the viewer has moved on". Clearing state without that check is
        # what made volume-up dead after a volume-down hold: the trailing release takes about
        # 150ms, and the up pressed during it was wiped by the worker tidying up after down.
        self._volume_seq += 1

    def _volume_worker(self) -> None:
        """Hold the key down while the button is down, and let the television ramp.

        One press per gesture, not a stream of them. In CEC a follower receiving
        `<User Control Pressed>` starts repeating the action itself and continues until
        `<User Control Released>` arrives — so sending presses repeatedly does not step it
        repeatedly, it keeps *restarting* the repeat, and the volume never stops. Which is
        what happened: the set ran to the end of its range and stayed there.

        The previous design sent a stream deliberately, and was right about the television it
        was measured on: the set at the old house ignored held keys and stepped only on a
        completed press-and-release. This one honours them properly. Tuning to one set and
        then moving house is how a design becomes wrong without a line of it changing.

        Press once, hold while the remote keeps saying so, release when it stops. That is the
        specification's own idiom; on a set that ramps it is correct, and on a set that does
        not it degrades to one step per gesture rather than to a runaway.

        `VOLUME_MAX_HOLD` is the backstop. A release that is lost — a busy bus, a crash, a
        restart mid-gesture — would otherwise leave the set ramping with nothing coming to
        stop it, and that is the one failure here bad enough to design against twice.
        """
        wire: str | None = None
        pressed_at = 0.0
        stepped_at = 0.0

        def release(reason: str) -> None:
            nonlocal wire
            if wire is None:
                return
            for _ in range(2):
                # Twice: a lost release is the difference between a volume control and a
                # runaway, and the message is idempotent.
                try:
                    self._volume("release", wire)
                except Exception:  # noqa: BLE001
                    pass
            if VOLUME_TRACE:
                print(f"  vol: -> release ({reason})", flush=True)
            wire = None

        try:
            while self.running:
                ui_cmd = self._volume_cmd
                now = time.monotonic()

                if ui_cmd is None:
                    release("idle")
                    time.sleep(0.02)
                    continue

                holding = self._volume_holding

                # Held too long, whatever the remote claims. Never negotiable.
                if wire is not None and now - pressed_at > self.VOLUME_MAX_HOLD:
                    release("held too long")
                    self._volume_cmd = None
                    self._volume_holding = False
                    continue

                # The remote has gone quiet, so the button is up.
                if wire is not None and now - self._volume_seen > self.VOLUME_HOLD_GAP:
                    seq = self._volume_seq
                    release("button up")
                    if self._volume_seq == seq:
                        self._volume_cmd = None
                        self._volume_holding = False
                    continue

                if wire is not None and wire != ui_cmd:
                    release("direction changed")

                # One press per step, paced. `wire` still tracks that a key is outstanding
                # so the release at the end is never skipped.
                # A second press only when the remote says the button is still down.
                # Without the `holding` test a tap re-presses at VOLUME_STEP (0.20s) while
                # the quiet check does not fire until VOLUME_HOLD_GAP (0.25s), so every
                # single press became two steps.
                if wire is None or (holding and now - stepped_at >= self.VOLUME_STEP):
                    try:
                        self._volume("press", ui_cmd)
                        stepped_at = time.monotonic()
                        if wire is None:
                            wire, pressed_at = ui_cmd, stepped_at
                        if VOLUME_TRACE:
                            print(f"  vol: -> press {ui_cmd}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        if VOLUME_TRACE:
                            print(f"  vol: -> {ui_cmd} RAISED {exc}", flush=True)
                        self._volume_cmd = None
                        self._volume_holding = False

                time.sleep(0.02)
        finally:
            # However this thread ends — shutdown, exception, the box going down — the key
            # must not be left down on the bus.
            release("worker stopping")

    # ---------- the way back ----------

    def _unlocked(self, verb: Verb) -> bool:
        """Record the press and report whether the last few spell the code.

        A rolling window rather than a cursor that advances and resets. The obvious cursor is
        wrong in a way that only shows up when someone fumbles: after `up up up`, a cursor
        that restarts on the mismatch has thrown away the fact that the last two presses are
        a valid opening, and the code typed straight afterwards silently fails. Which is
        precisely the situation it exists for — you press up, notice the screen is dark, and
        immediately enter the code without pausing first.

        Comparing the tail of a window has no such state to get wrong. Every press is also
        stamped with the channel that was showing *before* it was acted on, so the first
        press of a matching window carries the channel to hand back.
        """
        now = time.monotonic()
        self._recent.append((verb, now, self.channel))
        if len(self._recent) < len(self.UNLOCK):
            return False
        if tuple(entry[0] for entry in self._recent) != self.UNLOCK:
            return False
        # Typed as one gesture, not assembled out of an evening's surfing.
        gaps = [b[1] - a[1] for a, b in zip(self._recent, list(self._recent)[1:])]
        if any(gap > self.UNLOCK_GAP for gap in gaps):
            return False
        self._unlock_channel = self._recent[0][2]
        self._recent.clear()
        return True

    def _wake_television(self) -> None:
        """The code landed. Put the channel back and ask the run loop to wake the set.

        Called from the input thread holding the lock, so it does the instant half only. The
        CEC sequence shells out and waits on a television answering when it feels like it —
        the better part of a second — and that belongs to the run loop, exactly as standby
        does.
        """
        print("  unlock: waking the television")
        # The six presses that spelled the code moved the dial. Give it back — usually for
        # free, since a burst inside one settle window never opened anything.
        if self.channel != self._unlock_channel:
            self.select(self._unlock_channel)
        self._wake_pending = True

    def _reannounce(self, channel: int) -> None:
        """The burst ended on the channel already showing. Nothing to open — just say so.

        More common than it sounds: up then down again inside one settle window, or a digit
        for the channel already on. Re-opening the file would re-seek and restart the
        programme, a visible glitch bought in exchange for nothing.
        """
        self._tuning = ""
        if self._guide is not None:
            self.player.hide_overlay(overlay_id=3)
            self._redraw_guide()
            return

        airing = self.lineup.now(channel, time.time())
        if airing is None or airing.off_air:
            self._redraw()
            return
        self.bug = BugState(airing=airing, shown_at=time.monotonic())
        self._redraw()

    # ---------- power ----------

    def toggle_power(self) -> None:
        """Sleep or wake. The Pi never goes down.

        "Off" means: stop the stream, clear the screen, and put the television into standby
        over CEC. "On" means the reverse — wake the set, and rejoin whatever channel was on
        **at the point the schedule has reached**, not where it was abandoned. That last part
        is the difference between a television and a paused video, and it is the whole reason
        the clock stays authoritative rather than the player.

        Called from the input thread, so it does only the instant half. The CEC call is a
        subprocess that waits on a television answering when it feels like it — the better
        part of a second — and that belongs to the run loop. Stopping the picture is one
        fire-and-forget IPC command, so it happens here and the screen goes dark under the
        thumb.
        """
        self.asleep = not self.asleep
        self._power_pending = True
        print(f"  power: {'standby' if self.asleep else 'wake'}")
        if not self.asleep:
            return

        # Abandon anything in flight. A settle that fired after the screen went dark would
        # open a file for a television that is off.
        self._settle_at = 0.0
        self._tuning = ""
        self._tune_seq += 1
        self.bug = BugState()
        self.mode = Mode.WATCH
        self.menu.visible = False
        self._guide = None
        self._guide_ass = None
        self._looping = False
        for overlay in (1, 2, 3, 4, 5):
            self.player.hide_overlay(overlay_id=overlay)
        self.player.stop()

    # ---------- airplay ----------

    def begin_cast(self) -> None:
        """A phone is taking the screen. Yield it, now.

        Called from the watcher thread the instant uxplay names the client, which happens
        several RTSP exchanges before any video flows. That head start is the whole budget:
        mpv holds DRM master until it exits, so it has to be gone before uxplay's pipeline
        asks for the display.

        Which is why this quits the player rather than pausing it. There is no way to make
        mpv let go of DRM while it lives — no property, no command — so the only lever is the
        process. It costs a relaunch coming back, which against a session measured in minutes
        is not a cost at all.

        No handover card. There is nowhere to draw one: the player that would draw it is the
        thing being taken down, and a card that flashes for a fifth of a second and is then
        replaced by a phone's screen is worse than a clean cut.
        """
        if self.casting:
            return
        self.casting = True
        self._settle_at = 0.0
        self._tuning = ""
        self._tune_seq += 1
        self.bug = BugState()
        self.mode = Mode.WATCH
        self.menu.visible = False
        self._guide = None
        self._guide_ass = None
        self._looping = False
        self._on_air = None
        print("  airplay: session starting — releasing the screen")
        self.player.release()

    def end_cast(self) -> None:
        """The session is over. Take the television back."""
        if not self.casting:
            return
        self.casting = False
        print("  airplay: session ended — resuming the dial")
        try:
            self.player.resume()
        except Exception as exc:  # noqa: BLE001
            # A player that will not come back is fatal in a way nothing else here is, and
            # the run loop's own liveness check is the right place to notice it.
            print(f"  airplay: could not restart the player: {exc}")
            self.running = False
            return
        self.tune(self.channel)

    # ---------- casting from a phone ----------

    def play_cast(self, video_id: str, position: float = 0.0) -> None:
        """A phone has said what to play. Put it up.

        Nothing is mirrored and no screen changes hands: the phone sent an id, mpv fetches
        the video itself, and this is an ordinary `loadfile` with a URL instead of a path.
        That is the whole reason DIAL beat AirPlay here — casting costs the box no more than
        a channel change does.

        Deliberately not a channel. It has no schedule, nothing follows it, and the dial must
        remember where the viewer was so that stopping the cast returns them to it rather
        than to whatever channel one happens to be first.
        """
        self._settle_at = 0.0
        self._tuning = ""
        self._tune_seq += 1
        if self._looping:
            self.player.clear_loop()
            self._looping = False
        if self._guide is not None:
            self.player.hide_overlay(overlay_id=4)
            self._guide = None
            self._guide_ass = None

        self.cast_video = video_id
        self.player.hide_overlay(overlay_id=2)
        print(f"  cast: playing {video_id} from {position:.0f}s")
        self.player.tune(f"https://www.youtube.com/watch?v={video_id}", position)

        # The bug says CAST rather than a channel number, because there is no channel to
        # name and a number here would be a lie about where the dial is.
        self.bug = BugState()
        self.player.show_overlay(render_tuning("CAST", "FROM A PHONE"), overlay_id=3)
        self._cast_bug_at = time.monotonic()

    def stop_cast(self) -> None:
        """The phone stopped. Go back to whatever the dial was on."""
        if not self.cast_video:
            return
        print("  cast: stopped — back to the dial")
        self.cast_video = None
        self.player.hide_overlay(overlay_id=3)
        self._on_air = None
        self.tune(self.channel)

    def _apply_power(self) -> None:
        """The slow half: the television itself.

        When the press came from the remote the direction is not decided yet — `_power_query`
        says to ask the set first, because the box being awake tells you nothing about
        whether there is a picture. Somebody can switch the television off with its own
        remote, or the Apple TV can take the input, and the tuner carries on regardless. In
        that state the only useful thing a power press can do is bring the picture back;
        toggling our own state would put a working tuner to sleep and leave the screen just
        as dark.

        Asking costs a round trip — measured at 34ms to this set — which is why it happens
        here and not under the thumb. It is only paid when the box is already awake, and in
        that case the screen the delay would have blanked is the one being asked about.
        """
        if self._power_query:
            self._power_query = False
            state = self._television_state()
            ours = self._screen_is_ours()

            # Four states, three actions. The one that was missing is the second: a
            # television already on, showing something else, should be *switched to*, not
            # turned off. Pressing power while watching the Apple TV used to put the set into
            # standby, which is the opposite of what anyone means by it.
            #
            #   set off,  not ours  -> wake, and take the input
            #   set off,  ours      -> wake, and say so again in case it was forgotten
            #   set on,   not ours  -> take the input, leave the power alone
            #   set on,   ours      -> we are what is showing, so turn it off
            #
            # "Not answering" counts as on, deliberately: a silent set is far more often on
            # than in standby, and guessing standby makes the button appear dead at exactly
            # the moment somebody wants the room quiet.
            if state == "standby":
                print("  power: set in standby — waking it and taking the input")
                if self.asleep:
                    self.toggle_power()      # unsleeps, and re-tunes below
                else:
                    self._wake_now()
                    return
            elif not ours:
                owner = self._screen_owner() or "another input"
                print(f"  power: set is on showing {owner} — taking the input")
                if self.asleep:
                    self.toggle_power()
                else:
                    self._take_input_now()
                    return
            else:
                print("  power: we are what is showing — sending both to standby")
                self.toggle_power()
                return

        if self._power is not None:
            try:
                self._power(not self.asleep)
            except Exception:  # noqa: BLE001 - a television that will not answer is not fatal
                pass
        if self.asleep:
            return
        # `_on_air` cleared so this is a real tune rather than the "already showing"
        # shortcut — nothing is showing, whatever the bookkeeping last recorded.
        self._on_air = None
        self.tune(self.channel)

    def _television_state(self) -> str:
        """"on", "standby", or "unknown" — whatever the set will admit to."""
        if self._tv_state is None:
            return "unknown"
        try:
            return self._tv_state() or "unknown"
        except Exception:  # noqa: BLE001 - an unanswerable set is not fatal
            return "unknown"

    def _screen_owner(self) -> str | None:
        """The physical address currently announced as being on screen, if anything has."""
        driver = self._cec_driver
        return getattr(driver, "screen_owner", None) if driver else None

    def _screen_is_ours(self) -> bool:
        """Are we what the television is showing?

        Answered from what the bus has announced rather than by asking, because only the
        *current* active source replies to a request — so silence cannot be told apart from
        an active device that does not answer.

        Unknown counts as ours. If nothing has claimed the screen since the box started, the
        conservative reading is that pressing power means "turn it off", which is what the
        button did before any of this and what a viewer expects when the television is
        plainly showing television.
        """
        owner = self._screen_owner()
        if owner is None or self._our_address is None:
            return True
        return owner == self._our_address

    def _take_input_now(self) -> None:
        """Announce ourselves as the active source, without touching the set's power."""
        if self._take_input is None:
            return
        try:
            self._take_input()
        except Exception:  # noqa: BLE001 - a television that will not answer is not fatal
            pass

    def _handover_now(self) -> None:
        """Swap the television between us and whatever else is plugged in.

        Which way round is read from the bus rather than remembered. The set announces
        every input change itself, so this stays right even when the switch was made
        with the television's own remote — a flag kept here would drift the first time
        anyone did that, and the button would then need pressing twice.

        Stepping forward is a cycle, not a jump: the set is being driven by its own
        INPUT button and that is what that button does. Coming back is exact.
        """
        if self._select_input is None:
            return
        from tub3.cec import port_of  # noqa: PLC0415 - tuner does not import tub3 at load

        ours = port_of(self._our_address)
        going_out = self._screen_is_ours()
        port = self._other_port if going_out else ours
        if port is None:
            print("  source: no idea which socket to ask for")
            return
        print(f"  source: {'handing the television over' if going_out else 'taking it back'}"
              f" — HDMI {port}")
        try:
            self._select_input(port)
        except Exception:  # noqa: BLE001 - a set that will not switch is not fatal
            pass

    def _wake_now(self) -> None:
        """Wake the television without touching the box's own power state.

        Deliberately not `toggle_power`. The box is already awake — that is how the code got
        entered — and what is wrong is on the television's side of the cable: it is off, or
        showing the Apple TV. So this sends the same CEC sequence standby's wake does and
        changes nothing else. Calling `toggle_power` here would put a working tuner to sleep.
        """
        if self._power is None:
            return
        try:
            self._power(True)
        except Exception:  # noqa: BLE001 - a television that will not answer is not fatal
            pass

    def _rescan(self) -> None:
        """Pick up channels that have started existing since the box came on.

        Additive only. A station whose schedule has run out must not vanish from under
        somebody watching it — it has its own OFF AIR card for that — and a dial that
        renumbers itself while in use is worse than one that is briefly out of date.

        Rebuilding the channel objects each pass is deliberate and free: `LiquidChannel`
        holds a path and a station name and reads nothing until asked, so the ones already
        known are simply dropped.
        """
        if self._rescan_for is None:
            return
        try:
            found = self._rescan_for()
        except Exception:  # noqa: BLE001 - a bad read must not take the television down
            return

        known = set(self.lineup.numbers)
        fresh = [channel for channel in found if channel.number not in known]
        if not fresh:
            return

        self.lineup.channels.extend(fresh)
        self.lineup.channels.sort(key=lambda channel: channel.number)
        for channel in sorted(fresh, key=lambda c: c.number):
            print(f"    {channel.number:>3}  {channel.name}  — now on air")
        # The listings cache is keyed on nothing but age, so force the next redraw to rebuild
        # rather than leave the new channel missing from the guide for up to its TTL.
        self._guide_rows_at = 0.0

    def _digits_are_final(self) -> bool:
        """True when no channel on the dial extends what has been typed.

        The 1.5s wait for a second digit used to be paid by every direct entry, and it is the
        single largest component of the measured latency — larger than opening the file. With
        2-13 on the dial only `1` is a prefix of anything, so only `1` should wait.
        """
        typed = self._pending_digits
        if not typed:
            return False
        return not any(
            len(str(number)) > len(typed) and str(number).startswith(typed)
            for number in self.lineup.numbers
        )

    def _commit_digits(self) -> None:
        if not self._pending_digits:
            return
        try:
            channel = int(self._pending_digits)
        except ValueError:
            channel = self.channel
        self._pending_digits = ""
        if self.lineup.get(channel):
            self.select(channel)
        else:
            # Nothing on that number. Drop the half-drawn entry rather than leaving `CH 9_`
            # sitting on the picture waiting for a digit that will not help.
            self._tuning = ""
            self._redraw()

    # ---------- drawing ----------

    def _refresh_health(self) -> None:
        """Recompute the health verdict, on the run loop and never while drawing.

        This forks `systemctl` and opens the schedule database. The first version called it
        from `_draw_health`, which runs on the input thread under the box lock — so opening
        the menu on a cold cache could block every button on the remote behind a subprocess
        with an eight-second timeout. Drawing now only reads what this leaves behind.
        """
        try:
            from tub3.health import check  # noqa: PLC0415 - keeps the desktop import light
            self._health = check()
        except Exception:  # noqa: BLE001 - health must never cost you the box
            self._health = {}

    def _draw_health(self) -> None:
        """Put the health line up, or take it down. A dictionary lookup, nothing more."""
        verdict = self._health or {}
        line = ""
        if verdict.get("level") not in (None, "ok"):
            try:
                line = render_menu_health(verdict)
            except Exception:  # noqa: BLE001 - a health line must never cost you the menu
                line = ""
        if line:
            self.player.show_overlay(line, overlay_id=5)
        else:
            self.player.hide_overlay(overlay_id=5)

    def _redraw(self) -> None:
        if self.mode is Mode.MENU and self.menu.visible:
            self.player.show_overlay(self.menu.render_ass(), overlay_id=1)
            # What is playing behind the panel. Looked up rather than taken from `self.bug`,
            # whose airing can be a programme old if the schedule has stepped on since the
            # channel was tuned — and the menu is exactly where a stale answer would be
            # believed.
            airing = None
            if self._guide is None:
                try:
                    airing = self.lineup.now(self.channel, time.time())
                except Exception:  # noqa: BLE001 - the menu must open regardless
                    airing = self.bug.airing
            if airing is not None and not airing.off_air:
                self.player.show_overlay(render_menu_now(airing), overlay_id=3)
            else:
                self.player.hide_overlay(overlay_id=3)
            self._draw_health()
            return
        self.player.hide_overlay(overlay_id=1)
        self.player.hide_overlay(overlay_id=5)
        if self._tuning:
            # A change is in flight — either settling, or waiting on a second digit. This is
            # the only thing on screen that is guaranteed to be true right now, so it wins
            # over both the old channel's bug and the listings.
            self.player.show_overlay(
                render_tuning(self._tuning, self._tuning_name), overlay_id=3)
            return
        if self._guide is not None:
            # The listings are the picture on this channel; the bug would be furniture on top
            # of furniture. Repaint them, since leaving the menu just wiped the frame.
            self._redraw_guide()
            return
        if self.bug.visible and self.bug.airing:
            self.player.show_overlay(render_bug(self.bug.airing), overlay_id=3)
        else:
            self.player.hide_overlay(overlay_id=3)

    # ---------- input ----------

    def handle(self, event: Event) -> None:
        with self._lock:
            if self.casting:
                # Any button takes the television back. A phone that walks off the network
                # never sends TEARDOWN, so without this the box sits on a dead AirPlay screen
                # with no way out but a power cycle — and the one thing to hand is the remote.
                if self._cast is not None:
                    self._cast.ended()
                else:
                    self.end_cast()
                return

            if self.asleep:
                # Anything wakes it. A box that is off and answers only one of its ten
                # buttons looks broken, and from the sofa there is no way to tell which
                # button was the special one — least of all with the screen dark.
                self.toggle_power()
                return

            # Before anything else acts on the press, so the tracker sees every verb and a
            # digit or a volume nudge resets it. Runs *alongside* the normal handling rather
            # than instead of it — an incomplete code has to behave exactly like the surfing
            # it looks like, or the box feels haunted.
            if self._unlocked(event.verb):
                self._wake_television()
                return

            if event.verb is Verb.POWER:
                # Which way this goes is the television's to answer, not ours. The box can be
                # wide awake with the set switched off or on another input — somebody used
                # the TV remote, or the Apple TV took it — and in that state the only useful
                # thing a long press can do is bring the picture back. Toggling our own state
                # would put a working tuner to sleep and leave the screen exactly as dark.
                #
                # Asking costs a CEC round trip, so the run loop does it.
                if self.asleep:
                    # Nothing to ask: the box is off, so the press means wake, and the
                    # answer is the same whatever the set says.
                    self.toggle_power()
                else:
                    self._power_query = True
                    self._power_pending = True
                return

            if event.verb is Verb.DIGIT and event.digit is not None:
                # Direct channel entry, where the remote has digits. Never required — the
                # same channels are all reachable with up and down alone.
                self._pending_digits += str(event.digit)
                if self._digits_are_final():
                    self._commit_digits()
                    return
                # Genuinely ambiguous, so show the entry rather than nothing: `CH 1_` is a
                # box waiting for you, where a blank screen is a box ignoring you.
                self._digit_deadline = time.monotonic() + self.DIGIT_WAIT
                self._tuning = f"CH {self._pending_digits:_<2}"
                self._tuning_name = ""
                self._redraw()
                return

            if self.mode is Mode.MENU:
                self.menu.handle(event)
                if not self.menu.visible:
                    self.mode = Mode.WATCH
                self._redraw()
                return

            # Volume: the side rockers on a clicker, and the TV remote over CEC. Not one of
            # the four verbs — nothing depends on it — but a television without volume on the
            # remote is a television people complain about.
            if event.verb is Verb.VOLUME_UP:
                self._send_volume("volume-up", +5, event.repeat)
                return
            if event.verb is Verb.VOLUME_DOWN:
                self._send_volume("volume-down", -5, event.repeat)
                return
            if event.verb is Verb.MUTE:
                self._send_volume("mute", None)
                return

            # UP goes to a *higher* channel number, which is the opposite of what the same
            # key does in the menu. That is not an inconsistency to be tidied away: a cursor
            # moves up a list toward the top, and a dial turns up toward channel 12. Every
            # television ever made agrees, and this had the list convention applied to the
            # dial — so the remote felt backwards while the menu felt right.
            if event.verb is Verb.UP:
                self.surf(1)
            elif event.verb is Verb.DOWN:
                self.surf(-1)
            elif event.verb is Verb.SELECT:
                self.mode = Mode.MENU
                # `open`, not `visible = True`: it resets to the root and rests the cursor on
                # the way out, so pressing the menu button twice closes it again.
                self.menu.open()
                self._redraw()
            elif event.verb is Verb.BACK:
                # On a four-button remote this is the "what am I watching" affordance,
                # and it costs nothing.
                self.show_bug_again()
            elif event.verb is Verb.INFO:
                # The same thing, on the button that actually says so. BACK keeps doing
                # it because a clicker has no INFO key and would otherwise lose it.
                self.show_bug_again()
            elif event.verb is Verb.LAST:
                self.jump_back()
            elif event.verb is Verb.GUIDE:
                self.jump_to_guide()
            elif event.verb is Verb.SOURCE:
                # Deferred, like every other CEC round trip: it shells out and waits
                # on a television, which is the better part of a second, and this is
                # the input thread.
                self._handover_pending = True

    # ---------- run ----------

    def run(self, drivers: list[Driver]) -> None:
        self.running = True
        self.tune(self.channel)

        for driver in drivers:
            if getattr(driver, "name", "") == "cec":
                self._cec_driver = driver
            threading.Thread(target=self._pump, args=(driver,), daemon=True).start()

        if self._volume is not None:
            threading.Thread(target=self._volume_worker, daemon=True).start()

        housekept_at = 0.0
        try:
            while self.running:
                time.sleep(self.TICK)
                if self.casting:
                    # The player is deliberately gone and a phone has the screen. Every check
                    # below assumes an mpv to talk to, and the liveness check immediately
                    # after this would read a released player as a crash and shut the box
                    # down — turning the first AirPlay session into the last one.
                    continue
                if not self.player.alive:
                    # mpv exited — usually because the viewer quit. Ending the loop here is
                    # the difference between a clean shutdown and a broken-pipe traceback.
                    self.running = False
                    break

                now = time.monotonic()
                due: int | None = None
                power = False
                wake = False
                handover = False
                with self._lock:
                    if self._wake_pending:
                        self._wake_pending = False
                        wake = True
                    if self._handover_pending:
                        self._handover_pending = False
                        handover = True
                    if self._power_pending:
                        self._power_pending = False
                        power = True
                    elif self._pending_digits and now > self._digit_deadline:
                        self._commit_digits()
                    if self._settle_at and now >= self._settle_at:
                        self._settle_at = 0.0
                        due = self.channel

                # Outside the lock, both of them. One shells out to `cec-ctl` and waits on a
                # television; the other opens a file. Holding the lock across either would
                # make every press arriving meanwhile wait for it, which is the drag.
                if handover:
                    self._handover_now()
                if wake:
                    # Not `continue` — the code does not change what is playing, so the
                    # settle and housekeeping below still have work to do on this tick.
                    self._wake_now()
                if power:
                    self._apply_power()
                    continue
                if self.asleep:
                    continue

                if due is not None:
                    if due == self._on_air:
                        self._reannounce(due)
                    else:
                        self.tune(due)
                    continue

                if now - housekept_at < self.HOUSEKEEPING:
                    continue
                housekept_at = now

                if now - self._rescanned_at >= self.RESCAN:
                    self._rescanned_at = now
                    with self._lock:
                        self._rescan()

                    self._refresh_health()

                # Outside the lock: retuning opens a file, and `tune` is documented as never
                # running under it.
                if self._ambiance_stale():
                    self.tune(self._on_air, announce=False)
                    continue

                with self._lock:
                    # The bug fades on its own; redraw only on the transition.
                    if self.mode is Mode.WATCH and not self.bug.visible and self.bug.airing:
                        self.bug.airing = None
                        self._redraw()
                    if self._guide is not None:
                        # The listings scroll, so this one *does* redraw every pass — 4 Hz
                        # against 22 px/sec is about five pixels a step, which reads as
                        # motion rather than as stepping. The menu still wins the screen.
                        if self.mode is Mode.WATCH:
                            self._redraw_guide()
                        continue
                self._advance_if_ended()
        except KeyboardInterrupt:
            self.running = False

    def _ambiance_stale(self) -> bool:
        """True when the ambiance loop was chosen for an hour that has since passed.

        The playlist is picked when you tune, and this is the one channel people leave on for
        a whole day — so without this, nothing would notice four o'clock arriving and the
        sunset clip becoming the right one. It was left on all Saturday playing "Paris Balcony
        Jazz at Night" through breakfast and lunch, which is what prompted any of this.

        Cheap enough to sit on the minute tick: comparing two short strings, and the folder is
        only re-read on the four transitions a day where the answer actually changed.
        """
        if self._on_air is None or self._ambiance_daypart is None:
            return False
        station = self.lineup.get(self._on_air)
        if not getattr(station, "is_ambiance", False):
            return False
        from .ambiance import daypart_at  # noqa: PLC0415
        return daypart_at() != self._ambiance_daypart

    def _advance_if_ended(self) -> None:
        """Step to the next plan entry when the current one runs out.

        A block is several entries — a programme, its ad pod, the rest of the programme — and
        they run back to back. Stepping directly is both cheaper and more correct than
        re-asking the clock: a pod is four to eight entries, and re-querying at each boundary
        races the very clock it is consulting.

        The clock stays authoritative for everything else. If the file ended early, or the
        plan is exhausted, or the box was asleep, the right answer is still "what should be
        airing right now".
        """
        # A channel change is already in flight. Advancing here would open a file on the
        # channel being left, and its bug would stamp over the number the viewer is watching
        # for — all to finish a programme nobody is going to see the end of.
        #
        # Asleep is the same argument at its limit: mpv is idle *because* the box was told to
        # stop, and "idle" is exactly what this method treats as "the programme ended".
        # Without this it would helpfully start the next one on a television that is off.
        if self._settle_at or self._tuning or self.asleep or self.cast_video:
            return
        if self.mode is Mode.MENU or not self.player.get_property("idle-active"):
            return

        current = self.bug.airing
        following = current.next_entry if current else None
        if following is not None:
            result = self.player.tune(following.path, following.skip,
                                      duration=following.duration)
            self.last_latency_ms = result.latency_ms
            if result.ok:
                # Re-read the clock for display rather than synthesising an Airing, so the
                # bug and the schedule can never drift apart.
                refreshed = self.lineup.now(self.channel, time.time())
                if refreshed and not refreshed.off_air:
                    self.bug = BugState(airing=refreshed, shown_at=self.bug.shown_at)
                return

        # Same channel, next programme — not a channel change, so it does not announce.
        self.tune(self.channel, announce=False)

    def _pump(self, driver: Driver) -> None:
        try:
            for event in driver.events():
                if not self.running:
                    break
                self.handle(event)
        except Exception:  # noqa: BLE001 - a dead driver must not take the box down
            pass
