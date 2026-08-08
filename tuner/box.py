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

There is no spinner and no "loading" state anywhere. Measured channel change is ~17ms median
and 50ms worst through real mpv, so there is nothing to hide — the correct design is to show
nothing at all rather than a progress indicator that flashes for one frame.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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


class Box:
    def __init__(
        self,
        lineup: Lineup,
        player: MpvPlayer,
        *,
        start_channel: int | None = None,
        state: dict | None = None,
    ):
        self.lineup = lineup
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

    # How long a set of listings rows stays good for. Rebuilding them queries every channel's
    # schedule across ninety minutes; the scroll is redrawn far more often than this, because
    # that part is arithmetic.
    GUIDE_ROWS_TTL = 20.0

    # ---------- tuning ----------

    def tune(self, channel: int, *, announce: bool = True) -> None:
        """Put a channel on screen.

        `announce` is what separates the two reasons this gets called. A viewer pressing
        up/down wants to be told where they landed; the box stepping to the next programme
        on the channel they are already watching does not — that is the same channel it was
        a moment ago, and saying so every time a programme starts is the box talking to
        itself. Nothing in the schedule is a channel change.
        """
        if getattr(self.lineup.get(channel), "is_guide", False):
            self._tune_guide(channel, announce=announce)
            return

        # Leaving the guide: undo its looping playlist, or the next channel inherits it and
        # repeats one programme forever.
        if self._guide is not None:
            self.player.clear_loop()
            self.player.hide_overlay(overlay_id=4)
            self._guide = None
            self._guide_ass = None

        airing = self.lineup.now(channel, time.time())
        if airing is None or airing.off_air:
            # Dead air. A real station showed a sign-off card rather than a black screen.
            self.channel = channel
            self.bug = BugState()
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
        self.channel = channel
        # Not announcing: keep the old timestamp so the bug stays however faded it already
        # was, rather than resetting its clock. Carrying the *airing* forward still matters,
        # because a BACK press must describe what is on now, not what was on before.
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
        self.channel = channel
        self.bug = BugState()
        self._guide = Guide(guide_channel=channel,
                            network=getattr(station, "name", "BOOBTUBE"))
        self._guide_rows = []
        self._guide_rows_at = 0.0
        self._guide_ass = None
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

        # Only push when the picture actually changed. A static guide changes once a minute,
        # when the clock does; pushing it four times a second regardless is five kilobytes an
        # update down a socket nobody is draining, which is what filled mpv's buffer and
        # crashed the tuner.
        if ass == self._guide_ass:
            return
        self._guide_ass = ass
        self.player.show_overlay(ass, overlay_id=4)

    def surf(self, delta: int) -> None:
        self.tune(self.lineup.surf(self.channel, delta))

    def _commit_digits(self) -> None:
        if not self._pending_digits:
            return
        try:
            channel = int(self._pending_digits)
        except ValueError:
            channel = self.channel
        self._pending_digits = ""
        if self.lineup.get(channel):
            self.tune(channel)

    # ---------- drawing ----------

    def _redraw(self) -> None:
        if self.mode is Mode.MENU and self.menu.visible:
            self.player.show_overlay(self.menu.render_ass(), overlay_id=1)
            return
        self.player.hide_overlay(overlay_id=1)
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
            if event.verb is Verb.DIGIT and event.digit is not None:
                # Direct channel entry, where the remote has digits. Never required — the
                # same channels are all reachable with up and down alone.
                self._pending_digits += str(event.digit)
                self._digit_deadline = time.monotonic() + 1.5
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

        try:
            while self.running:
                time.sleep(0.25)
                if not self.player.alive:
                    # mpv exited — usually because the viewer quit. Ending the loop here is
                    # the difference between a clean shutdown and a broken-pipe traceback.
                    self.running = False
                    break
                with self._lock:
                    if self._pending_digits and time.monotonic() > self._digit_deadline:
                        self._commit_digits()
                    # The bug fades on its own; redraw only on the transition.
                    if self.mode is Mode.WATCH and not self.bug.visible and self.bug.airing:
                        self.bug.airing = None
                        self._redraw()
                    if self._guide is not None:
                        # The listings scroll, so this one *does* redraw every tick — 4 Hz
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
