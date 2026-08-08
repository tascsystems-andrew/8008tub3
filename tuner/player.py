"""mpv, driven over its JSON IPC socket.

One long-lived mpv process owns the screen for the life of the appliance. Tuning is a
`loadfile` on the existing process, not a new one — process startup is the single largest
avoidable cost in a channel change, and paying it once at boot rather than on every press is
the difference between a television and a computer.

Two settings do most of the work:

- ``hr-seek=no`` — seek to the nearest keyframe instead of decoding forward to an exact
  frame. Measured flat at ~24ms regardless of GOP length, where accurate seek scales with
  keyframe distance (80ms at a 10-second GOP, and far worse on slower hardware). It lands up
  to half a GOP off the true schedule position, which is invisible: nobody knows what frame
  was supposed to be showing.
- ``keep-open=no`` — when a program ends, mpv reports it and the tuner advances, rather than
  freezing on a last frame.

Latency is measured from sending the command to mpv's ``playback-restart`` event, which is
the moment picture actually appears — not the moment the file was accepted.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class MpvUnavailable(RuntimeError):
    pass


@dataclass
class TuneResult:
    ok: bool
    latency_ms: float
    error: str | None = None


def _known_options(binary: str) -> set[str]:
    """Which --options this mpv build actually accepts.

    mpv treats an unknown option as a *fatal* error: it prints one line and exits before
    creating the IPC socket, so the caller sees "socket never appeared" and none of the
    three words that would explain why. That is what --auto-window-resize did here — added
    in a later mpv than Debian Bookworm ships, present on the Mac's Homebrew build, so the
    box worked in development and died on the appliance.

    Probing once and filtering is cheaper than pinning a version, and it degrades the right
    way: a missing cosmetic option costs the cosmetic behaviour, not the television.
    """
    try:
        result = subprocess.run([binary, "--list-options"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()

    names = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("--"):
            names.add(line.split()[0].split("=")[0])
    return names


class MpvPlayer:
    def __init__(
        self,
        *,
        socket_path: str | None = None,
        video_output: str | None = None,
        fullscreen: bool = True,
        extra_args: list[str] | None = None,
        mpv_binary: str | None = None,
    ):
        self.socket_path = socket_path or os.path.join(
            tempfile.gettempdir(), f"tub3-mpv-{os.getpid()}.sock"
        )
        self.video_output = video_output
        self.fullscreen = fullscreen
        self.extra_args = extra_args or []
        # macOS attributes a window's dock icon to the bundle containing the running
        # binary. Launching Homebrew's mpv means the dock shows mpv's identity, not
        # ours — so the app bundle ships a link to mpv inside its own MacOS folder
        # and points here at that path instead.
        self.mpv_binary = mpv_binary or os.environ.get("TUB3_MPV") or "mpv"
        self.proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._request_id = 0
        self._fullscreen_applied = False
        self.alive = True

    # ---------- lifecycle ----------

    def start(self, timeout: float = 10.0) -> None:
        import shutil

        if not os.path.exists(self.mpv_binary) and shutil.which(self.mpv_binary) is None:
            raise MpvUnavailable(f"mpv not found ({self.mpv_binary})")

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        args = [
            self.mpv_binary,
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=no",
            "--hr-seek=no",           # keyframe seeking: flat latency regardless of GOP
            "--osd-level=0",
            # mpv's on-screen controller draws its own idle screen when no file is loaded:
            # a coloured field, the mpv logo, and "Drop files or URLs to play here." All
            # three appeared *underneath* the standby card. There is no seek bar on a
            # television anyway.
            "--osc=no",
            "--no-input-default-bindings",
            "--cache=yes",
            "--demuxer-max-bytes=64MiB",
            f"--input-ipc-server={self.socket_path}",
        ]

        # A television does not change size when the programme does. mpv resizes its window
        # to each new file by default, so a 640x480 commercial inside a 720p show makes the
        # window jump on every ad break.
        #
        # Windowed only. These are window-geometry options and there is no window on a DRM
        # appliance — but mpv still honoured --autofit by sizing an internal surface, which
        # DRM then upscaled to the panel's 3840x2160 while the OSD drew at native. The
        # result on a 4K TV was every element of the standby card rendered twice, once
        # crisp and once blurred and offset.
        if not self.fullscreen:
            args += ["--auto-window-resize=no", "--autofit=1280x720",
                     "--keepaspect-window=no"]

        # Drop anything this build does not know. An unknown option is fatal to mpv, and
        # every option above that could be missing is cosmetic — losing one should cost a
        # nicety, never the picture.
        known = _known_options(self.mpv_binary)
        if known:
            def supported(arg: str) -> bool:
                name = arg.split("=")[0]
                # mpv lists a flag as --foo and accepts --no-foo as its negation, so the
                # negated spelling never appears in --list-options. Checking only the
                # literal name would drop --no-input-default-bindings and quietly hand the
                # viewer mpv's entire default keymap.
                return name in known or ("--" + name[5:]) in known and name.startswith("--no-")

            dropped = [a for a in args if a.startswith("--") and not supported(a)]
            if dropped:
                print(f"  mpv {self.mpv_binary} does not support: "
                      f"{', '.join(sorted(set(a.split('=')[0] for a in dropped)))}")
                args = [a for a in args if a not in dropped]
        if self.fullscreen:
            args.append("--fullscreen=yes")
        if self.video_output:
            args.append(f"--vo={self.video_output}")
        args += self.extra_args

        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.socket_path):
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(self.socket_path)
                    sock.settimeout(0.05)
                    self._sock = sock
                    self._assert_appliance_state()
                    return
                except OSError:
                    pass
            time.sleep(0.02)
        raise MpvUnavailable(f"mpv IPC socket never appeared at {self.socket_path}")

    def _assert_appliance_state(self) -> None:
        """Force the state a television is always in, rather than trusting launch flags.

        Escape hatches are bound unconditionally. Default key bindings are disabled so the
        four-verb model owns the keyboard, but that leaves a fullscreen window with no way
        out — fine on an appliance with a remote, a trap on a desktop.
        """
        for key in ("q", "Q", "Ctrl+c"):
            self._command(["keybind", key, "quit"], wait=False)
        for key in ("f", "F"):
            self._command(["keybind", key, "cycle fullscreen"], wait=False)

        if self.fullscreen:
            self._nudge_fullscreen()

    def _nudge_fullscreen(self) -> None:
        """Re-assert fullscreen on a delay, off the hot path.

        `--fullscreen=yes` is evaluated while mpv is still creating its window and is
        silently dropped on macOS. Setting it at connect time is equally early. Tying it to
        the first presented frame worked in isolation but raced in the real app, so it gets
        its own thread with a couple of retries — it costs nothing and it is the difference
        between an appliance and a window.
        """
        import threading

        def run() -> None:
            for delay in (0.6, 1.5, 3.0):
                time.sleep(delay)
                try:
                    self._command(["set_property", "fullscreen", True], wait=False)
                except Exception:  # noqa: BLE001 - the socket may be gone; never fatal
                    return

        threading.Thread(target=run, daemon=True).start()

    def close(self) -> None:
        try:
            if self._sock:
                self._command(["quit"], wait=False)
                self._sock.close()
        except (OSError, MpvUnavailable):
            # Shutting down a player that has already gone is the normal path, not an error.
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    # ---------- IPC ----------

    def _send(self, payload: dict) -> None:
        if not self._sock or not self.alive:
            raise MpvUnavailable("not connected")
        try:
            self._sock.sendall((json.dumps(payload) + "\n").encode())
        except OSError as exc:
            # mpv has gone. That is a normal way for this to end — the viewer pressed quit —
            # so it must unwind cleanly rather than surfacing a broken pipe traceback.
            self.alive = False
            raise MpvUnavailable("mpv exited") from exc

    def _read_messages(self, timeout: float) -> list[dict]:
        """Drain whatever mpv has sent, up to `timeout`."""
        if not self._sock:
            return []
        messages: list[dict] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            self._buffer += chunk
            while b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if messages:
                break
        return messages

    def _command(self, command: list, *, wait: bool = True, timeout: float = 2.0):
        self._request_id += 1
        request_id = self._request_id
        self._send({"command": command, "request_id": request_id})
        if not wait:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self._read_messages(0.05):
                if message.get("request_id") == request_id:
                    return message
        return None

    def _wait_for_event(self, name: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self._read_messages(0.05):
                if message.get("event") == name:
                    return True
        return False

    # ---------- tuning ----------

    # Below this, a keyframe-rounded seek is a serious error rather than an invisible one.
    # A commercial is 10-30 seconds and may carry one keyframe every few seconds, so rounding
    # can land past the end — mpv reports the file finished, the box advances, and tuning in
    # during a break appears to skip to the next advert. In a 23-minute programme the same
    # rounding is unnoticeable, which is why hr-seek=no is right there and wrong here.
    EXACT_SEEK_UNDER = 90.0

    def tune(self, path: Path, offset: float, *, timeout: float = 8.0,
             duration: float | None = None) -> TuneResult:
        """Punch into a file at `offset`. Returns time to actual picture."""
        if not self.alive:
            return TuneResult(False, 0.0, "mpv exited")
        self._read_messages(0.0)  # discard anything stale before we start timing
        start = time.perf_counter()

        # Exact seeking for short items, keyframe seeking for long ones. Set per load rather
        # than at launch: the cost of an exact seek scales with how far into the file it is,
        # and on something this short it is a few frames of decoding.
        want_exact = duration is not None and duration <= self.EXACT_SEEK_UNDER and offset > 0.5
        self._command(["set_property", "hr-seek", "yes" if want_exact else "no"], wait=False)

        # Setting start= as a load option lets mpv seek during open, rather than opening,
        # decoding from zero, and then seeking.
        options = f"start=+{max(0.0, offset):.3f}"
        response = self._command(
            ["loadfile", str(path), "replace", 0, options], timeout=timeout
        )
        if response is None or response.get("error") not in (None, "success"):
            # Older mpv builds take options in the third argument position instead.
            response = self._command(["loadfile", str(path), "replace", options], timeout=timeout)

        if response is not None and response.get("error") not in (None, "success"):
            return TuneResult(False, 0.0, str(response.get("error")))

        # A television is never paused. mpv can come up paused after a load-with-seek, and a
        # box that boots into a frozen frame reads as broken rather than as "press play" —
        # there is no play button on a remote with four buttons.
        self._command(["set_property", "pause", False], wait=False)

        # playback-restart fires when frames actually start presenting.
        started = self._wait_for_event("playback-restart", timeout)
        latency_ms = (time.perf_counter() - start) * 1000.0
        if not started:
            return TuneResult(False, latency_ms, "no playback-restart event")

        # Fullscreen only sticks once a window genuinely exists. Setting it at launch or at
        # connect time is silently ignored on macOS, because mpv is still bringing the window
        # up. The first presented frame is the earliest moment it can be trusted.
        if self.fullscreen and not self._fullscreen_applied:
            self._command(["set_property", "fullscreen", True], wait=False)
            self._fullscreen_applied = True

        return TuneResult(True, latency_ms)

    # The coordinate space overlay ASS is authored in. Passing 0,0 here lets mpv choose,
    # which means the same menu renders at wildly different sizes depending on the display —
    # enormous on a retina panel, unreadable on a CRT. Declaring the space explicitly makes
    # mpv scale it to fit, so the menu occupies the same fraction of the screen everywhere.
    OVERLAY_RES = (1920, 1080)

    def show_overlay(self, ass_text: str, overlay_id: int = 1) -> None:
        if not self.alive:
            return
        width, height = self.OVERLAY_RES
        self._command([
            "osd-overlay", overlay_id, "ass-events", ass_text,
            width, height, 0, "no",
        ], wait=False)

    def hide_overlay(self, overlay_id: int = 1) -> None:
        if not self.alive:
            return
        self._command(["osd-overlay", overlay_id, "none", ""], wait=False)

    def nudge_volume(self, delta: int) -> None:
        if not self.alive:
            return
        self._command(["add", "volume", delta], wait=False)

    def toggle_mute(self) -> None:
        if not self.alive:
            return
        self._command(["cycle", "mute"], wait=False)

    def get_property(self, name: str):
        try:
            response = self._command(["get_property", name])
        except MpvUnavailable:
            return None
        return response.get("data") if response else None
