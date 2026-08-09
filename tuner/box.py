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

import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .input import Driver, Event, Verb
from .menu import Menu, build_root
from .player import MpvPlayer
from .schedule import Airing, Lineup


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
    ):
        self.lineup = lineup
        self._rescan_for = rescan
        self._rescanned_at = 0.0
        # Injected rather than imported, so `tuner` keeps not depending on `tub3`. Takes one
        # argument: whether the television should now be on.
        self._power = power
        self.asleep = False
        self._power_pending = False
        # An AirPlay session owns the screen. Unlike standby, this is not something the
        # viewer asked the box for — a phone decided — so the box's job is to get out of the
        # way quickly and take the screen back the moment the session ends.
        self.casting = False
        self._cast = None
        self.player = player
        self.mode = Mode.WATCH
        self.menu = Menu(build_root(state or {}))
        self.bug = BugState()
        self.channel = start_channel or (lineup.numbers[0] if lineup.numbers else 0)
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

    def surf(self, delta: int) -> None:
        self.select(self.lineup.surf(self.channel, delta))

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
        for overlay in (1, 2, 3, 4):
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

    def _apply_power(self) -> None:
        """The slow half: the television itself."""
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

    def _redraw(self) -> None:
        if self.mode is Mode.MENU and self.menu.visible:
            self.player.show_overlay(self.menu.render_ass(), overlay_id=1)
            return
        self.player.hide_overlay(overlay_id=1)
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

            if event.verb is Verb.POWER:
                self.toggle_power()
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
                self.player.nudge_volume(+5)
                return
            if event.verb is Verb.VOLUME_DOWN:
                self.player.nudge_volume(-5)
                return
            if event.verb is Verb.MUTE:
                self.player.toggle_mute()
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
                self.menu.visible = True
                self._redraw()
            elif event.verb is Verb.BACK:
                # Re-show the bug. On a four-button remote this is the "what am I watching"
                # affordance, and it costs nothing.
                if self.bug.airing:
                    self.bug.shown_at = time.monotonic()
                self._redraw()

    # ---------- run ----------

    def run(self, drivers: list[Driver]) -> None:
        self.running = True
        self.tune(self.channel)

        for driver in drivers:
            threading.Thread(target=self._pump, args=(driver,), daemon=True).start()

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
                with self._lock:
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
        if self._settle_at or self._tuning or self.asleep:
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
