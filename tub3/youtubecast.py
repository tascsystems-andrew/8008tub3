"""Casting from the YouTube app, end to end.

Two halves that are useless apart:

    tub3.dial     puts the box in the phone's cast list, receives a launch + pairing code
    tub3.lounge   turns that code into a session, and the session into video ids

This owns both and reports one thing: play this video. Which is the entire point of choosing
DIAL over AirPlay — the phone sends an id, not pixels, so the box fetches the video itself and
hands it to the player it already has. Nothing is mirrored, nothing is re-encoded, the screen
never changes hands, and the phone can lock or leave the room.

A launch arrives with a *new* pairing code every time, so a session is joined per launch and
the previous one is dropped. Keeping the old one alive would leave two long polls racing to
say what is on screen.
"""

from __future__ import annotations

from typing import Callable

from .dial import DialService
from .lounge import Lounge, screen_id_for


class CastReceiver:
    """One YouTube cast target.

    `on_video(video_id, position)` fires when the phone says what to play; `on_stop()` when it
    stops. Both are called from a background thread, so whatever they touch has to expect it.
    """

    def __init__(self, name: str = "BoobTube",
                 on_video: Callable[[str, float], None] | None = None,
                 on_stop: Callable[[], None] | None = None):
        self.name = name
        self.on_video = on_video
        self.on_stop = on_stop
        self.lounge: Lounge | None = None
        # Settled once, here, rather than per session: DIAL publishes it in the app
        # description before any launch arrives, and every lounge session has to register its
        # pairing code against that same id or a phone reading the description is pointed at a
        # screen nobody is listening on.
        self.screen_id = screen_id_for(name)
        self.dial = DialService(name=name, screen_id=self.screen_id,
                                on_launch=self._launched, on_stop=self._stopped)

    def _launched(self, app: str, params: dict) -> None:
        code = params.get("pairingCode")
        if not code:
            # A launch with no code is YouTube asking us to come to the foreground, which is
            # meaningless on a box with no app switcher. Nothing to join, nothing to do.
            print(f"  cast: {app} launched with no pairing code — ignoring")
            return

        if self.lounge:
            self.lounge.close()
        lounge = Lounge(name=self.name, on_video=self.on_video, on_stop=self.on_stop,
                        theme=params.get("theme", "cl"), screen_id=self.screen_id)
        self.lounge = lounge
        print(f"  cast: {app} launched, joining session")
        if not lounge.register(code):
            print("  cast: could not register the pairing code")
            return
        lounge.start()

    def _stopped(self, app: str) -> None:
        if self.lounge:
            self.lounge.close()
            self.lounge = None
        if self.on_stop:
            self.on_stop()

    def start(self) -> None:
        self.dial.start()

    def close(self) -> None:
        if self.lounge:
            self.lounge.close()
        self.dial.close()


def main() -> int:
    """Run the receiver alone and print what the phone asks for.

    Nothing here needs the television, the tuner or a schedule — which is the point. Casting
    either reaches this box or it does not, and that should be answerable on its own.
    """
    import time

    receiver = CastReceiver(
        on_video=lambda v, t: print(f"\n  PLAY  https://youtu.be/{v}  at {t:.0f}s\n"),
        on_stop=lambda: print("\n  STOP\n"))
    receiver.start()
    print("  waiting — open YouTube on a phone and press cast\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        receiver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
