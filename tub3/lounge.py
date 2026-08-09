"""YouTube's lounge protocol — the half that turns a cast into a video id.

`tub3.dial` gets the box into the phone's cast list and receives a launch. That launch does
not carry the video: it carries a **pairing code**, because YouTube does not push media at a
television. The television joins a session and is *told* what to play, which is why casting
survives the phone locking, leaving the room, or running out of battery.

    phone taps cast
        -> DIAL launch, with pairingCode              (tub3.dial)
        -> register_pairing_code  binds that code to our screen id
        -> get_lounge_token_batch exchanges the screen id for a token
        -> bind, long-polled      yields setPlaylist / nowPlaying with video ids
        -> mpv plays it

The long poll is the whole design and it is worth understanding: a `bind` GET hangs for as
long as nothing happens and returns the moment something does. So the box is not polling
YouTube every few seconds; it is holding one request open and being pushed to. That also means
a dropped connection is normal rather than exceptional, and reconnecting is the main job.

No dependencies, deliberately — this is urllib and a chunked-length parser. The wire format is
Google's old browser-channel: each frame is a decimal byte length on its own line, then that
many bytes of JSON. It is odd but it is stable, and parsing it is thirty lines.

Nothing here is authenticated and nothing here sees an account. The pairing code is issued by
the phone, scoped to one session, and grants exactly the ability to be told a video id.
"""

from __future__ import annotations

import json
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Iterator

BASE = "https://www.youtube.com/api/lounge"
PAIRING = f"{BASE}/pairing"
BIND = f"{BASE}/bc/bind"

# Google rejects the default urllib agent outright. Anything browser-shaped is accepted; this
# says what it is, because a receiver that lies about being Chrome is harder to debug and no
# more likely to work.
AGENT = "Mozilla/5.0 (compatible; 8008TUB3/0.1; +https://github.com/tub3)"

# The app name the lounge expects from a screen. Not cosmetic: an unrecognised value is
# refused at bind time with an error that does not mention the app name.
APP = "lb-v4"


def _post(url: str, fields: dict, timeout: float = 15.0) -> str:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.youtube.com",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def screen_id_for(name: str) -> str:
    """A stable id for this box.

    Derived rather than random so it survives a restart. A screen that reintroduces itself
    under a new id every boot accumulates dead entries in the phone's list, and YouTube keeps
    offering them long after they stop existing.
    """
    seed = uuid.uuid5(uuid.NAMESPACE_DNS, f"tub3-lounge-{name}-{socket.gethostname()}")
    return seed.hex + uuid.uuid5(seed, "screen").hex[:32]


class Lounge:
    """One screen's connection to the lounge.

    `on_video(video_id, position)` is called each time the session says what should be
    playing. It is called for a change of video, not for every status frame.
    """

    def __init__(self, name: str = "BoobTube",
                 on_video: Callable[[str, float], None] | None = None,
                 on_stop: Callable[[], None] | None = None,
                 theme: str = "cl"):
        self.name = name
        # Carried from the DIAL launch and echoed back on every bind. YouTube checks it and
        # hangs up on a mismatch — `forceDisconnect: unmatchingTheme` — immediately after
        # handing out a session id, so the connection *looks* successful and then silently
        # goes nowhere. That presents at the phone as connecting forever.
        self.theme = theme
        self.screen_id = screen_id_for(name)
        self.on_video = on_video
        self.on_stop = on_stop
        self.token: str | None = None
        self.sid: str | None = None
        self.gsession: str | None = None
        self.playing: str | None = None
        self._rid = random.randint(10000, 99999)
        # Outgoing message ordinal. The lounge rejects a command whose offset it has already
        # seen, so this only ever moves forward.
        self._ofs = 0
        self._seen_commands: set[str] = set()
        self._alive = False

    # ---------- pairing ----------

    def register(self, pairing_code: str) -> bool:
        """Attach the phone's one-time code to this screen."""
        try:
            _post(f"{PAIRING}/register_pairing_code", {
                "access_type": "permanent",
                "pairing_code": pairing_code,
                "screen_id": self.screen_id,
                "screen_name": self.name,
                "app": APP,
            })
        except urllib.error.HTTPError as exc:
            print(f"  lounge: register_pairing_code refused ({exc.code})")
            return False
        except OSError as exc:
            print(f"  lounge: register_pairing_code failed ({exc})")
            return False
        return True

    def refresh_token(self) -> bool:
        """Exchange the screen id for a lounge token. Tokens expire; this is re-callable."""
        try:
            raw = _post(f"{PAIRING}/get_lounge_token_batch",
                        {"screen_ids": self.screen_id})
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"  lounge: get_lounge_token_batch failed ({exc})")
            return False
        try:
            screens = json.loads(raw).get("screens") or []
            self.token = screens[0]["loungeToken"]
        except (json.JSONDecodeError, KeyError, IndexError):
            print(f"  lounge: no token in response: {raw[:120]}")
            return False
        return True

    # ---------- the channel ----------

    def _bind_params(self) -> dict:
        return {
            "device": "LOUNGE_SCREEN",
            "app": APP,
            "id": self.screen_id[:32],
            "name": self.name,
            "loungeIdToken": self.token or "",
            "VER": "8",
            "v": "2",
            "RID": str(self._rid),
            "CVER": "1",
            "theme": self.theme,
            "zx": uuid.uuid4().hex[:12],
            "t": "1",
        }

    def send(self, command: str, payload: dict | None = None) -> bool:
        """Say something back.

        The lounge is a conversation, not a feed, and missing that is what makes a receiver
        look connected and never play anything. On joining, YouTube asks `getNowPlaying` and
        `getDiscoveryDeviceId`; until those are answered it does not consider the screen ready
        and never sends a video — the phone sits on "connecting" indefinitely.

        Commands go back on the same bind URL as a form post: `req0__sc` names the command and
        `req0_<field>` carries each argument.
        """
        if not self.sid:
            return False
        self._rid += 1
        params = self._bind_params()
        params.update({"SID": self.sid, "gsessionid": self.gsession or "",
                       "RID": str(self._rid), "AID": "0"})
        fields = {"count": "1", "ofs": str(self._ofs), "req0__sc": command}
        for key, value in (payload or {}).items():
            fields[f"req0_{key}"] = str(value)
        self._ofs += 1
        try:
            _post(f"{BIND}?{urllib.parse.urlencode(params)}", fields)
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"  lounge: could not send {command} ({exc})")
            return False
        return True

    # Player states the lounge understands. -1 is the one that matters on joining: it means
    # "a screen is here and has nothing loaded", which is what invites a video.
    STOPPED, PLAYING = "-1", "1"

    def _report_state(self) -> None:
        """What this screen is doing, in the shape the lounge expects.

        Every field, not the obvious four. A partial answer is accepted and then ignored: the
        session stays open, the phone shows "connecting", and nothing is ever sent. Working
        receivers reply with the whole set, so this does too.
        """
        playing = bool(self.playing)
        self.send("nowPlaying", {
            "videoId": self.playing or "",
            "currentTime": "0.000",
            "duration": "0.000",
            "seekableStartTime": "0.000",
            "seekableEndTime": "0.000",
            "currentIndex": "0" if playing else "-1",
            "listId": "",
            "state": self.PLAYING if playing else self.STOPPED,
        })

    @staticmethod
    def frames(body: str) -> Iterator[list]:
        """Split Google's browser-channel framing into JSON chunks.

        Each frame is a decimal length on its own line followed by exactly that many bytes.
        Counting characters rather than trusting newlines matters: the payloads contain
        newlines of their own, and a naive line split silently truncates every frame holding a
        video title.
        """
        index = 0
        while index < len(body):
            newline = body.find("\n", index)
            if newline == -1:
                return
            try:
                size = int(body[index:newline])
            except ValueError:
                return
            chunk = body[newline + 1:newline + 1 + size]
            index = newline + 1 + size
            try:
                yield json.loads(chunk)
            except json.JSONDecodeError:
                continue

    def _handle(self, events: list) -> None:
        """One decoded frame: a list of [ordinal, [name, payload]] entries."""
        for entry in events:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            body = entry[1]
            if not isinstance(body, list) or not body:
                continue
            name = body[0]
            payload = body[1] if len(body) > 1 and isinstance(body[1], dict) else {}

            if name == "c":
                self.sid = body[1] if len(body) > 1 else self.sid
            elif name == "S":
                self.gsession = body[1] if len(body) > 1 else self.gsession
            elif name in ("nowPlaying", "setPlaylist", "onStateChange"):
                video = payload.get("videoId") or payload.get("video_id")
                position = float(payload.get("currentTime") or 0.0)
                if video and video != self.playing:
                    self.playing = video
                    if self.on_video:
                        self.on_video(video, position)
            elif name == "getNowPlaying":
                self._report_state()
            elif name == "getDiscoveryDeviceId":
                self.send("discoveryDeviceId", {"deviceId": self.screen_id[:32]})
            elif name not in ("nowPlaying", "setPlaylist", "onStateChange",
                              "getNowPlaying", "getDiscoveryDeviceId"):
                # Everything the lounge says that this does not act on. Printed once each,
                # because the reason casting sat at "connecting" was a command nobody had
                # looked at — and guessing the protocol from documentation was what produced
                # a receive-only client in the first place.
                if name not in self._seen_commands:
                    self._seen_commands.add(name)
                    detail = str(payload)[:110] if payload else ""
                    print(f"  lounge: <- {name} {detail}")
            elif name == "forceDisconnect":
                reason = payload.get("reason", "unknown")
                print(f"  lounge: YouTube refused the session ({reason})")
                self.sid = None
            elif name in ("remoteConnected", "remoteDisconnected", "loungeStatus",
                          "loungeScreenConnected", "onAutoplayModeChanged",
                          "autoplayUpNext", "playlistModified", "onSubtitlesTrackChanged",
                          "onAutoplayDismissed", "onVolumeChanged", "c", "S", "noop"):
                pass          # known and uninteresting
            elif name in ("onStop", "stopVideo"):
                self.playing = None
                if self.on_stop:
                    self.on_stop()

    def connect(self) -> bool:
        """Open the channel. Returns False if the lounge will not have us."""
        params = self._bind_params()
        url = f"{BIND}?{urllib.parse.urlencode(params)}"
        try:
            body = _post(url, {"count": "0"})
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"  lounge: bind failed ({exc})")
            return False
        for frame in self.frames(body):
            self._handle(frame)
        if self.sid:
            # Volunteered, not waited for. The lounge asks `getNowPlaying` on joining, but
            # saying it unprompted costs one request and removes an ordering assumption.
            self._report_state()
        return self.sid is not None

    def _poll_once(self, timeout: float = 45.0) -> bool:
        params = self._bind_params()
        params.update({"RID": "rpc", "SID": self.sid or "", "CI": "0", "AID": "0",
                       "gsessionid": self.gsession or "", "TYPE": "xmlhttp"})
        url = f"{BIND}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except socket.timeout:
            # Nothing happened for the length of the poll. Entirely normal.
            return True
        except (urllib.error.HTTPError, OSError):
            return False
        for frame in self.frames(body):
            self._handle(frame)
        return True

    def run(self) -> None:
        """Hold the channel open until closed, reconnecting as needed."""
        self._alive = True
        backoff = 2.0
        while self._alive:
            if not self.token and not self.refresh_token():
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            if not self.connect():
                self.token = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 2.0
            while self._alive and self._poll_once():
                pass

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def close(self) -> None:
        self._alive = False


def main() -> int:
    """Join a session from a pairing code and print what it asks for.

    Usage:  python3 -m tub3.lounge <pairingCode>

    The code comes from a DIAL launch — run `python3 -m tub3.dial`, cast from the phone, and
    paste the pairingCode it prints.
    """
    import sys

    if len(sys.argv) < 2:
        print(main.__doc__)
        return 1
    lounge = Lounge(on_video=lambda v, t: print(f"  PLAY https://youtu.be/{v} at {t:.0f}s"),
                    on_stop=lambda: print("  STOP"))
    print(f"  screen id {lounge.screen_id[:16]}…")
    if not lounge.register(sys.argv[1]):
        return 1
    print("  registered; joining the session")
    lounge.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
