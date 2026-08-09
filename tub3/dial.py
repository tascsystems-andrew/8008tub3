"""DIAL — how the YouTube app finds a television.

The cast button in YouTube is not AirPlay and never was. It is DIAL: the phone shouts on the
local network, anything that answers describes itself over HTTP, and the phone then asks it to
launch an app by name. That distinction is the whole reason this file exists — uxplay
implements AirPlay mirroring and audio and does not implement AirPlay *video* at all (no
`/play`, no `/scrub`, no `/rate`), so a phone casting from inside an app was only ever offered
the speaker.

What makes DIAL the right target rather than a consolation prize: **the phone sends a video
id, not pixels.** Nothing is mirrored, nothing is encoded, and the box fetches the video
itself and plays it with the player it already has. No handover, no DRM to yield, no race —
casting becomes another thing the tuner can put on screen, which is exactly what a channel is.

Three parts, and this file is the first two:

    1. SSDP        answer M-SEARCH so the phone knows we exist        <- here
    2. DIAL HTTP   describe ourselves, accept a launch                <- here
    3. Lounge      turn the launch into a stream of video ids         <- tub3.lounge

Deliberately no dependencies. SSDP is a UDP socket and a text response; DIAL is four HTTP
routes and two XML documents. Both are small enough that pulling in a UPnP stack would cost
more than it saves, and this box has one Python and no npm.
"""

from __future__ import annotations

import socket
import struct
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# What a DIAL client searches for. `ssdp:all` is answered too, because some clients enumerate
# rather than ask by name and a receiver that only answers the specific query is invisible to
# them.
DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"

# Not 8008: the settings server already has that, and a port collision here would look like a
# discovery failure — the least debuggable symptom available.
DEFAULT_PORT = 8009

DEVICE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <manufacturer>8008TUB3</manufacturer>
    <modelName>BoobTube</modelName>
    <UDN>uuid:{udn}</UDN>
  </device>
</root>
"""

APP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<service xmlns="urn:dial-multiscreen-org:schemas:dial" dialVer="2.1">
  <name>{app}</name>
  <options allowStop="true"/>
  <state>{state}</state>
  {extra}
</service>
"""


def local_ip() -> str:
    """The address this box is actually reachable on.

    Same trick as `tub3.app.local_ip` and for the same reason: the hostname resolves to
    127.0.0.1 on a box with more than one interface, and 127.0.0.1 is precisely the address
    a phone cannot use.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class DialService:
    """One DIAL receiver: discovery, description, and app launch.

    `on_launch(app, params)` is called when a phone asks for an app. It is handed the parsed
    body, which for YouTube carries the pairing code the lounge side needs. Whatever it
    returns is ignored; raising is caught, because a receiver that 500s on launch drops out of
    the phone's list and stays out.
    """

    def __init__(self, name: str = "BoobTube", port: int = DEFAULT_PORT,
                 apps: tuple[str, ...] = ("YouTube",),
                 on_launch: Callable[[str, dict], None] | None = None,
                 on_stop: Callable[[str], None] | None = None):
        self.name = name
        self.port = port
        self.apps = apps
        self.on_launch = on_launch
        self.on_stop = on_stop
        # Stable across restarts of the process but not across reinstalls. A UDN that changes
        # every boot makes the phone treat the box as a new device each time, which shows up
        # as duplicates in the cast list that never go away.
        self.udn = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tub3-{socket.gethostname()}"))
        self.running = ""          # which app is currently launched, "" for none
        self._http: ThreadingHTTPServer | None = None
        self._ssdp: socket.socket | None = None
        self._alive = False

    # ---------- discovery ----------

    def _ssdp_reply(self, st: str) -> bytes:
        return (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: http://{local_ip()}:{self.port}/dd.xml\r\n"
            "EXT:\r\n"
            "SERVER: Linux/6.1 UPnP/1.1 tub3/0.1\r\n"
            f"ST: {st}\r\n"
            f"USN: uuid:{self.udn}::{DIAL_ST}\r\n"
            "BOOTID.UPNP.ORG: 1\r\n"
            "CONFIGID.UPNP.ORG: 1\r\n"
            "\r\n"
        ).encode()

    def _serve_ssdp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", SSDP_PORT))
        except OSError as exc:
            print(f"  dial: cannot bind SSDP ({exc}) — the box will not be discoverable")
            return
        # Join on every interface. Binding INADDR_ANY is not enough for multicast: without
        # the membership the kernel drops the group's traffic before it reaches us, and the
        # symptom is a receiver that works over unicast and is invisible to every phone.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY))
        sock.settimeout(1.0)
        self._ssdp = sock

        while self._alive:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            if not text.startswith("M-SEARCH"):
                continue
            wanted = ""
            for line in text.split("\r\n"):
                if line.upper().startswith("ST:"):
                    wanted = line.split(":", 1)[1].strip()
            if wanted not in (DIAL_ST, "ssdp:all"):
                continue
            try:
                # Answered with the searcher's own ST, per SSDP. Replying with our canonical
                # one to an `ssdp:all` search makes some clients discard the response.
                sock.sendto(self._ssdp_reply(wanted), addr)
            except OSError:
                continue

    # ---------- http ----------

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A003 - silence the default stderr spam
                pass

            def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain",
                      extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = urlparse(self.path).path
                if path == "/dd.xml":
                    body = DEVICE_XML.format(name=service.name, udn=service.udn).encode()
                    # The header, not the XML, is what tells a client where apps live. A
                    # description served without it parses fine and is useless.
                    self._send(200, body, "application/xml",
                               {"Application-URL": f"http://{local_ip()}:{service.port}/apps/"})
                    return
                app = path[len("/apps/"):] if path.startswith("/apps/") else ""
                if app in service.apps:
                    state = "running" if service.running == app else "stopped"
                    extra = (f'<link rel="run" href="run"/>' if state == "running" else "")
                    body = APP_XML.format(app=app, state=state, extra=extra).encode()
                    self._send(200, body, "application/xml")
                    return
                self._send(404)

            def do_POST(self):  # noqa: N802
                path = urlparse(self.path).path
                app = path[len("/apps/"):] if path.startswith("/apps/") else ""
                if app not in service.apps:
                    self._send(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                params = {k: v[0] for k, v in parse_qs(raw).items()}
                service.running = app
                if service.on_launch:
                    try:
                        service.on_launch(app, params)
                    except Exception as exc:  # noqa: BLE001
                        # Never 500. A receiver that errors on launch is dropped from the
                        # phone's list and does not come back until the app is restarted.
                        print(f"  dial: launch handler failed: {exc}")
                self._send(201, b"", "text/plain",
                           {"LOCATION": f"http://{local_ip()}:{service.port}/apps/{app}/run"})

            def do_DELETE(self):  # noqa: N802
                path = urlparse(self.path).path
                if path.endswith("/run"):
                    app = path[len("/apps/"):-len("/run")]
                    if app in service.apps:
                        service.running = ""
                        if service.on_stop:
                            try:
                                service.on_stop(app)
                            except Exception:  # noqa: BLE001
                                pass
                        self._send(200)
                        return
                self._send(404)

        return Handler

    # ---------- lifecycle ----------

    def start(self) -> None:
        self._alive = True
        self._http = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler())
        self._http.daemon_threads = True
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        threading.Thread(target=self._serve_ssdp, daemon=True).start()
        print(f"  dial: {self.name} discoverable on {local_ip()}:{self.port}")

    def close(self) -> None:
        self._alive = False
        if self._http:
            self._http.shutdown()
        if self._ssdp:
            try:
                self._ssdp.close()
            except OSError:
                pass


def main() -> int:
    """Run the receiver alone and print what a phone asks of it.

    The point of a standalone mode: discovery either works or it does not, and finding that
    out should not require the television, the tuner, or a schedule.
    """
    import time

    def launched(app: str, params: dict) -> None:
        print(f"\n  LAUNCH {app}")
        for key, value in params.items():
            print(f"    {key} = {value}")
        print()

    service = DialService(on_launch=launched, on_stop=lambda app: print(f"  STOP {app}"))
    service.start()
    print("  waiting — open YouTube on a phone and press cast\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
