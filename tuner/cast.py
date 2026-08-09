"""AirPlay sessions, reduced to two events.

The receiver is `uxplay`, running as its own systemd unit. This does not start it, own it or
link against it — it follows the unit's journal and reports when a session begins and ends.
That separation is the GPL boundary made literal: uxplay is GPL-3.0, this project is MPL, and
what passes between them is lines of text.

Why the journal rather than a pipe: uxplay has to advertise for the whole time the box is on,
because a phone can only offer what it can already see on the network. Something that runs
that long belongs to systemd, not to a tuner that gets restarted whenever the dial changes.
Following the journal also means the tuner can restart mid-session and pick up where it was.

**The timing is the whole design.** uxplay idle touches no display; it builds its GStreamer
pipeline, and therefore asks DRM for the screen, only once a client connects. mpv holds DRM
master until it exits. So the tuner has to be out of the way *before* the pipeline starts, and
what makes that possible is that uxplay announces the client during RTSP negotiation — several
exchanges before any video flows.

    Client identified as User-Agent: AirPlay/xxx     <- a session is starting, yield now
    TEARDOWN                                          <- it is over, take the screen back
"""

from __future__ import annotations

import re
import subprocess
import threading
from typing import Callable

UNIT = "tub3-airplay"

# Negotiation, not playback. This lands well before the first frame, which is the only reason
# the handover can be done in time.
STARTED = re.compile(r"Client identified as User-Agent")

# TEARDOWN is the protocol's own goodbye and the one to trust. "Stopping" covers uxplay
# itself going down — a restart, a crash, the unit being stopped — which leaves the screen
# free and must be treated as the end of the session rather than waited on forever.
ENDED = re.compile(r"TEARDOWN|Stopping\.\.\.")


class CastWatcher:
    """Follow the receiver's journal and call back on each transition.

    Only transitions are reported. uxplay logs the user-agent line more than once during a
    negotiation, and a second "started" while already casting would take the screen away from
    a session that already has it.
    """

    def __init__(self, on_start: Callable[[], None], on_end: Callable[[], None],
                 unit: str = UNIT):
        self.on_start = on_start
        self.on_end = on_end
        self.unit = unit
        self.casting = False
        self._proc: subprocess.Popen | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._follow, daemon=True).start()

    def _follow(self) -> None:
        try:
            # -n0: only what happens from now on. Replaying the boot's worth of journal would
            # hand the screen to a session that ended hours ago.
            self._proc = subprocess.Popen(
                ["journalctl", "-u", self.unit, "-f", "-n", "0", "-o", "cat"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except OSError:
            # No journalctl, or no permission to read the unit. AirPlay simply does not
            # happen; nothing else about the television changes.
            return

        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if not self._running:
                break
            self.feed(line)

    def feed(self, line: str) -> None:
        """One log line. Split out from the reader so it can be tested without a journal."""
        if STARTED.search(line):
            if not self.casting:
                self.casting = True
                self._safely(self.on_start)
            return
        if ENDED.search(line) and self.casting:
            self.casting = False
            self._safely(self.on_end)

    def ended(self) -> None:
        """Force the session closed — the viewer pressed a button and wants the dial back.

        A phone that leaves the network mid-session never sends TEARDOWN, so without this the
        box would sit on a dead AirPlay screen with no way out but a restart. Any button is
        the escape hatch, and it has to work from the remote rather than from a terminal.
        """
        if self.casting:
            self.casting = False
            self._safely(self.on_end)

    @staticmethod
    def _safely(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - a failed handover must not kill the watcher
            pass

    def close(self) -> None:
        self._running = False
        if self._proc:
            self._proc.terminate()
