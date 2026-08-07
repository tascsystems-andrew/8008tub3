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


class MpvPlayer:
    def __init__(
        self,
        *,
        socket_path: str | None = None,
        video_output: str | None = None,
        fullscreen: bool = True,
        extra_args: list[str] | None = None,
    ):
        self.socket_path = socket_path or os.path.join(
            tempfile.gettempdir(), f"tub3-mpv-{os.getpid()}.sock"
        )
        self.video_output = video_output
        self.fullscreen = fullscreen
        self.extra_args = extra_args or []
        self.proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._request_id = 0

    # ---------- lifecycle ----------

    def start(self, timeout: float = 10.0) -> None:
        import shutil

        if shutil.which("mpv") is None:
            raise MpvUnavailable("mpv not found on PATH")

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        args = [
            "mpv",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=no",
            "--hr-seek=no",           # keyframe seeking: flat latency regardless of GOP
            "--osd-level=0",
            "--no-input-default-bindings",
            "--cache=yes",
            "--demuxer-max-bytes=64MiB",
            f"--input-ipc-server={self.socket_path}",
        ]
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
                    return
                except OSError:
                    pass
            time.sleep(0.02)
        raise MpvUnavailable(f"mpv IPC socket never appeared at {self.socket_path}")

    def close(self) -> None:
        try:
            if self._sock:
                self._command(["quit"], wait=False)
                self._sock.close()
        except OSError:
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
        if not self._sock:
            raise MpvUnavailable("not connected")
        self._sock.sendall((json.dumps(payload) + "\n").encode())

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

    def tune(self, path: Path, offset: float, *, timeout: float = 8.0) -> TuneResult:
        """Punch into a file at `offset`. Returns time to actual picture."""
        self._read_messages(0.0)  # discard anything stale before we start timing
        start = time.perf_counter()

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

        # playback-restart fires when frames actually start presenting.
        started = self._wait_for_event("playback-restart", timeout)
        latency_ms = (time.perf_counter() - start) * 1000.0
        if not started:
            return TuneResult(False, latency_ms, "no playback-restart event")
        return TuneResult(True, latency_ms)

    def show_overlay(self, ass_text: str, overlay_id: int = 1) -> None:
        self._command([
            "osd-overlay", overlay_id, "ass-events", ass_text,
            0, 0, 0, "no",
        ], wait=False)

    def hide_overlay(self, overlay_id: int = 1) -> None:
        self._command(["osd-overlay", overlay_id, "none", ""], wait=False)

    def get_property(self, name: str):
        response = self._command(["get_property", name])
        return response.get("data") if response else None
