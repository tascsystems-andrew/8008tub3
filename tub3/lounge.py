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

import io
import json
import os
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

# What this box tells YouTube it is, sent as a JSON blob on the bind.
DEVICE_INFO = {
    "brand": "8008TUB3",
    "model": "BoobTube",
    "year": 0,
    "os": "Linux",
    "osVersion": "6.1",
    "chipset": "",
    "clientName": "TVHTML5",
    "dialAdditionalDataSupportLevel": "unsupported",
    "mdxDialServerType": "MDX_DIAL_SERVER_TYPE_UNKNOWN",
}

# What this screen can be asked to do. `dsp` is display, and it is the member every shipping
# receiver declares — a real LG TV sends dsp,mic,dpa,ntb,que,mus. Declaring nothing does not
# mean "everything": YouTube picks a default, and the one it picked for this box was `que,mus`
# — a screen that can hold a queue and cannot show anything. Observed in a live `loungeStatus`.
CAPABILITIES = "dsp,mic,dpa,ntb,que,mus"


def _post_bytes(url: str, fields: dict, timeout: float = 15.0) -> bytes:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.youtube.com",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _post(url: str, fields: dict, timeout: float = 15.0) -> str:
    return _post_bytes(url, fields, timeout).decode("utf-8", "replace")


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
                 theme: str = "cl", screen_id: str | None = None):
        self.name = name
        # Carried from the DIAL launch and echoed back on every bind. YouTube checks it and
        # hangs up on a mismatch — `forceDisconnect: unmatchingTheme` — immediately after
        # handing out a session id, so the connection *looks* successful and then silently
        # goes nowhere. That presents at the phone as connecting forever.
        self.theme = theme
        # Passed in when the DIAL side already published one, so the id a phone reads out of
        # the app description is the id the pairing code is registered against. Deriving it
        # twice happens to agree today and is a coincidence, not a guarantee.
        self.screen_id = screen_id or screen_id_for(name)
        # Two identities, deliberately. The screen id is what the pairing code is attached to;
        # the device id is what the bind speaks as. YouTube treats them as different things,
        # but has to be told they belong together — which is what `device_id` at registration
        # is for. Binding as a 32-character prefix of the screen id was neither one nor the
        # other: not the screen the code was paired to, and not a device id anything knew.
        self.device_id = uuid.uuid5(uuid.NAMESPACE_DNS,
                                    f"tub3-device-{name}-{socket.gethostname()}").hex
        # The sender tags playback with this and expects to see it come back.
        self.cpn = uuid.uuid4().hex[:16]
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
        # Last event ordinal received, sent back as `AID` on the next poll. This is browser
        # channel's acknowledgement: it says "I have everything up to here". Pinning it at 0
        # tells the server nothing has arrived, so it replays the whole session on every poll.
        self._aid = 0
        self._seen_commands: set[str] = set()
        # Set TUB3_LOUNGE_TRACE=1 to print every frame. Two wrong fixes came from reasoning
        # about this protocol instead of watching it.
        self.trace = bool(os.environ.get("TUB3_LOUNGE_TRACE"))
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
                "device_id": self.device_id,
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

    def _bind_params(self, kind: str = "bind") -> dict:
        """The query half of a bind call. `kind` is one of bind / rpc / send.

        The three shapes differ in exactly two places: the poll carries no `deviceInfo`, and
        only the opening bind carries `CVER`.
        """
        params = {
            "device": "LOUNGE_SCREEN",
            "app": APP,
            "id": self.device_id,
            "name": self.name,
            "loungeIdToken": self.token or "",
            "theme": self.theme,
            "capabilities": CAPABILITIES,
            "mdxVersion": "2",
            "obfuscatedGaiaId": "",
            "cst": "m",
            "VER": "8",
            "v": "2",
            "RID": str(self._rid),
            "zx": uuid.uuid4().hex[:12],
            "t": "1",
        }
        if kind != "rpc":
            params["deviceInfo"] = json.dumps(DEVICE_INFO, separators=(",", ":"))
        if kind == "bind":
            params["CVER"] = "1"
        return params

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
        params = self._bind_params("send")
        params.update({"SID": self.sid, "gsessionid": self.gsession or "",
                       "RID": str(self._rid), "AID": str(self._aid)})
        fields = {"count": "1", "ofs": str(self._ofs), "req0__sc": command}
        for key, value in (payload or {}).items():
            fields[f"req0_{key}"] = str(value)
        try:
            reply = _post(f"{BIND}?{urllib.parse.urlencode(params)}", fields)
        except (urllib.error.HTTPError, OSError) as exc:
            # The offset is not consumed by a message that never landed. Leaving a hole in the
            # sequence costs the *next* message its delivery, which is a failure that shows up
            # one message later than the one that caused it.
            print(f"  lounge: could not send {command} ({exc})")
            return False
        self._ofs += 1
        if self.trace:
            # The reply to a command was thrown away for the whole time this did not work.
            # A 200 here does not mean accepted: the lounge answers a malformed or
            # out-of-order command with a body that says so, and that body was never read.
            print(f"  lounge: -> {command} {fields} => {reply!r}")
        return True

    # Player states the lounge understands. -1 is the one that matters on joining: it means
    # "a screen is here and has nothing loaded", which is what invites a video.
    STOPPED, PLAYING = "-1", "1"

    def _report_state(self) -> None:
        """What this screen is doing, in the shape the lounge expects.

        Two messages, not one: `nowPlaying` says what is loaded and `onStateChange` says what
        it is doing, and senders read them for different things.

        An idle screen sends an empty `nowPlaying` rather than a videoId of `""`. A blank id is
        a claim about a video that does not exist, and it reads as one.
        """
        if not self.playing:
            self.send("nowPlaying", {})
            self.send("onStateChange", {
                "state": self.STOPPED, "currentTime": "0.000", "duration": "0.000",
                "loadedTime": "0.000", "seekableStartTime": "0.000",
                "seekableEndTime": "0.000", "cpn": self.cpn,
            })
            return
        common = {
            "currentTime": "0.000",
            "duration": "0.000",
            "loadedTime": "0.000",
            "seekableStartTime": "0.000",
            "seekableEndTime": "0.000",
            "cpn": self.cpn,
            "state": self.PLAYING,
        }
        self.send("nowPlaying", {**common, "videoId": self.playing,
                                 "currentIndex": "0", "listId": ""})
        self.send("onStateChange", common)

    @staticmethod
    def frames(stream) -> Iterator[list]:
        """Browser-channel frames off a live byte stream, yielded as each one completes.

        Two things here are load-bearing and both were wrong.

        **Frames are yielded as they arrive**, not after the body is complete — because on the
        long poll the body is never complete. The lounge answers immediately and then holds the
        connection open to push down it for as long as the session lasts. Reading to the end of
        something with no end is what threw away every video id YouTube ever sent this box.

        **The length prefix counts bytes**, so framing happens before decoding. Slicing a
        decoded string by a byte count over-reads by one position per multi-byte character, and
        an iPhone named `Andrew's iPhone` with a typographic apostrophe puts three bytes and one
        character into the very first frame of every session. The over-read swallows the next
        frame's length line and there is no way back: the parser is desynchronised for the rest
        of the connection.
        """
        while True:
            line = stream.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                size = int(line)
            except ValueError:
                continue
            chunk = b""
            while len(chunk) < size:
                part = stream.read(size - len(chunk))
                if not part:
                    return
                chunk += part
            try:
                yield json.loads(chunk.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue

    @classmethod
    def frames_from_text(cls, body: str) -> Iterator[list]:
        """Frames from a captured body. Re-encodes, because the framing is byte-counted and a
        decoded string has already lost the counts."""
        return cls.frames(io.BytesIO(body.encode("utf-8")))

    # Commands that arrive, are understood, and require nothing of us. Listed so the unknown
    # command log stays quiet enough to be worth reading.
    IGNORED = frozenset({
        "remoteConnected", "remoteDisconnected", "loungeStatus", "loungeScreenConnected",
        "onAutoplayModeChanged", "autoplayUpNext", "playlistModified",
        "onSubtitlesTrackChanged", "onAutoplayDismissed", "onVolumeChanged",
        "onHasPreviousNextChanged", "onPlaylistModified", "onUserActivity",
        "noop", "c", "S",
    })

    def _handle(self, events: list) -> None:
        """One decoded frame: a list of [ordinal, [name, payload]] entries.

        Written as a dispatch rather than a chain because a chain is what broke it. An earlier
        version put the catch-all *before* `forceDisconnect` and `onStop`, so every branch
        after it was unreachable: a refused session was reported as an unknown command and a
        stop never stopped anything. `elif name not in (...)` is always true by the time it is
        reached, and that is not obvious enough to be safe.
        """
        for entry in events:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            # Browser-channel numbers every event. The number is the acknowledgement the next
            # poll carries, so it is tracked here rather than anywhere the payload is read.
            if isinstance(entry[0], int):
                self._aid = max(self._aid, entry[0])
            body = entry[1]
            if not isinstance(body, list) or not body:
                continue
            name = body[0]
            payload = body[1] if len(body) > 1 and isinstance(body[1], dict) else {}
            if self.trace:
                # Untruncated on purpose, and with the ordinal. The 110-character summary
                # below is right for normal running and wrong for debugging: the one field
                # worth reading in `loungeStatus` is the device list, and it starts past
                # character 110. The ordinal is what `AID` acknowledges.
                print(f"  lounge: <- [{entry[0]}] {name} {payload}")

            if name == "c":
                self.sid = body[1] if len(body) > 1 else self.sid
            elif name == "S":
                self.gsession = body[1] if len(body) > 1 else self.gsession
            elif name in ("nowPlaying", "setPlaylist", "onStateChange", "updatePlaylist"):
                video = payload.get("videoId") or payload.get("video_id")
                position = float(payload.get("currentTime") or 0.0)
                if video and video != self.playing:
                    self.playing = video
                    if self.on_video:
                        self.on_video(video, position)
                    # Say it out loud. Until the screen reports the id back, the sender has no
                    # evidence anything happened and sits on "trying to start playing" —
                    # obeying the instruction is not the same as being seen to obey it.
                    self._report_state()
            elif name == "getNowPlaying":
                self._report_state()
            elif name == "getDiscoveryDeviceId":
                self.send("discoveryDeviceId", {"deviceId": self.device_id})
            elif name == "getVolume":
                # There is no volume control here — the television has one. A fixed level is
                # honest enough, and is what the sender is waiting on before it hands over
                # media. These interrogations are a handshake, not small talk.
                self.send("onVolumeChanged", {"volume": "100", "muted": "false"})
            elif name == "setVolume":
                # Echoed back unchanged. Not obeying it is fine; not acknowledging it is not.
                self.send("onVolumeChanged", {
                    "volume": str(payload.get("volume", "100")),
                    "muted": str(payload.get("muted", "false")),
                })
            elif name == "getSubtitlesTrack":
                self.send("onSubtitlesTrackChanged", {
                    "videoId": self.playing or "", "trackName": "",
                    "languageCode": "", "languageName": "", "kind": "",
                })
            elif name == "getPartyGamesMode":
                self.send("onPartyGamesModeChanged",
                          {"partyGamesMode": "PARTY_GAMES_MODE_NONE"})
            elif name in ("onStop", "stopVideo"):
                self.playing = None
                if self.on_stop:
                    self.on_stop()
            elif name == "forceDisconnect":
                reason = payload.get("reason", "unknown")
                print(f"  lounge: YouTube refused the session ({reason})")
                self.sid = None
            elif name not in self.IGNORED and name not in self._seen_commands:
                # Everything the lounge says that this does not act on, printed once each.
                # The reason casting sat at "connecting" was a command nobody had looked at,
                # and guessing the protocol from documentation is what produced a
                # receive-only client in the first place.
                self._seen_commands.add(name)
                detail = str(payload)[:110] if payload else ""
                print(f"  lounge: <- {name} {detail}")

    def connect(self) -> bool:
        """Open the channel. Returns False if the lounge will not have us."""
        # A new channel restarts the server's event numbering, so the acknowledgement has to go
        # back to zero with it or the first events of the session look already-seen. The
        # *outgoing* counter restarts for the same reason and was being carried over: an offset
        # from a session that no longer exists is not a position the server can place, so it
        # drops the message and answers 200 anyway.
        self._aid = 0
        self._ofs = 0
        self._rid += 1
        params = self._bind_params("bind")
        url = f"{BIND}?{urllib.parse.urlencode(params)}"
        try:
            body = _post_bytes(url, {"count": "0"})
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"  lounge: bind failed ({exc})")
            return False
        for frame in self.frames(io.BytesIO(body)):
            self._handle(frame)
        if self.sid:
            # Volunteered, not waited for. The lounge asks `getNowPlaying` on joining, but
            # saying it unprompted costs one request and removes an ordering assumption.
            self._report_state()
        return self.sid is not None

    def _poll_once(self, timeout: float = 120.0) -> bool:
        """Hold one poll open and act on frames as they land.

        Not read-then-parse, which is what this used to be and why nothing ever played. The
        lounge answers this request immediately and then keeps the connection to push down, so
        a body read waits for an end that does not come. Worse, the server sends `noop`
        keepalives that reset the socket's per-recv timer, so even the timeout never fires:
        the read blocks forever with the events sitting unparsed in the buffer.

        `timeout` is therefore "no bytes at all for this long", not a poll duration. Returning
        True means the connection ended and should simply be reopened; False means the session
        itself is gone and needs rebinding.
        """
        params = self._bind_params("rpc")
        params.update({"RID": "rpc", "SID": self.sid or "", "CI": "0",
                       "AID": str(self._aid),
                       "gsessionid": self.gsession or "", "TYPE": "xmlhttp"})
        url = f"{BIND}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for frame in self.frames(response):
                    self._handle(frame)
                    if not self._alive:
                        return False
        except socket.timeout:
            # Nothing at all for two minutes. Normal enough on a quiet session — and note that
            # anything received before this point has already been dispatched, rather than
            # discarded along with the buffer.
            if self.trace:
                print(f"  lounge: poll idle (aid {self._aid})")
            return True
        except urllib.error.HTTPError as exc:
            # 400 Unknown SID / 410 Gone mean the channel is finished. Anything else is worth
            # retrying on the same session.
            if self.trace:
                print(f"  lounge: poll HTTP {exc.code}")
            return exc.code not in (400, 410)
        except OSError as exc:
            if self.trace:
                print(f"  lounge: poll ended ({exc})")
            return True
        if self.trace:
            print(f"  lounge: poll closed cleanly (aid {self._aid})")
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
