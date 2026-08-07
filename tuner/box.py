"""The appliance loop: clock, player, input, menu.

Deliberately small. The scheduling engine is FieldStation42's, the player is mpv, the input
devices are the kernel's. What lives here is the thing none of them do: turn four button
presses into a television.

Two modes, because four buttons is the whole vocabulary:

    WATCH   UP/DOWN change channel   SELECT opens the menu   BACK shows the bug
    MENU    UP/DOWN move the cursor  SELECT activates        BACK goes up a level

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
    """ASS markup for the corner bug — channel, name, what's on, how long is left."""
    minutes_left = int(airing.remaining // 60)
    remaining = f"{minutes_left} min left" if minutes_left else "ending"
    lines = [
        f"{{\\b1}}{airing.channel}  {airing.channel_name}{{\\b0}}",
        airing.program.name[:40],
        remaining,
    ]
    header = (
        r"{\an1\pos(70,1010)\fnmonospace\fs30\bord0\shad3"
        r"\1c&H00D7FF&\4c&H000000&}"
    )
    return header + r"\N".join(lines)


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

    # ---------- tuning ----------

    def tune(self, channel: int) -> None:
        airing = self.lineup.now(channel, time.time())
        if airing is None:
            # Dead air. A real station showed a sign-off card rather than a black screen.
            self.channel = channel
            self.player.show_overlay(
                r"{\an5\pos(960,540)\fnmonospace\fs42\1c&H00D7FF&}"
                f"CHANNEL {channel}\\NOFF AIR", overlay_id=2,
            )
            return

        self.player.hide_overlay(overlay_id=2)
        result = self.player.tune(airing.program.path, airing.offset)
        self.last_latency_ms = result.latency_ms
        self.channel = channel
        self.bug = BugState(airing=airing, shown_at=time.monotonic())
        self._redraw()

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

            if event.verb is Verb.UP:
                self.surf(-1)
            elif event.verb is Verb.DOWN:
                self.surf(1)
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
                with self._lock:
                    if self._pending_digits and time.monotonic() > self._digit_deadline:
                        self._commit_digits()
                    # The bug fades on its own; redraw only on the transition.
                    if self.mode is Mode.WATCH and not self.bug.visible and self.bug.airing:
                        self.bug.airing = None
                        self._redraw()
                    self._advance_if_ended()
        except KeyboardInterrupt:
            self.running = False

    def _advance_if_ended(self) -> None:
        """When a program runs out, join whatever the clock says is on now.

        The clock is authoritative, not the playlist — if the file ended early or the box
        was asleep, the right answer is still "what should be airing right now".
        """
        if self.mode is Mode.MENU:
            return
        idle = self.player.get_property("idle-active")
        if idle:
            self.tune(self.channel)

    def _pump(self, driver: Driver) -> None:
        try:
            for event in driver.events():
                if not self.running:
                    break
                self.handle(event)
        except Exception:  # noqa: BLE001 - a dead driver must not take the box down
            pass
