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
import threading
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
        drm_mode: str | None = None,
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
        self.drm_mode = drm_mode
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
        # Writes only, never the wait. Since the tune moved off the box's lock, two threads
        # reach this socket: the run loop opening a file, and input pushing an overlay the
        # instant a button is pressed. `sendall` can loop on a partial write, so without this
        # a long overlay and a loadfile can interleave into one corrupt line.
        #
        # Deliberately not held across the reply wait. `tune` waits up to eight seconds for
        # playback-restart, and blocking the button behind that would restore precisely the
        # drag this arrangement exists to remove. It is safe because every call made from the
        # input thread is fire-and-forget: only the run loop ever reads.
        self._write_lock = threading.Lock()
        self._dropping = False
        self._fullscreen_applied = False
        self._backdrop = False
        self._splash_dismissed = False
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
            # `video-sync` is left at mpv's default of `audio`, and that is a decision.
            #
            # `display-resample` looks like the right answer to judder and was measurably
            # much worse here: it renders one output frame per *display refresh* rather than
            # per source frame, so a 4K file being shown at 1080p pays for a 3840x2160 ->
            # 1920x1080 scale sixty times a second instead of twenty-four. Measured on the
            # box: the filter chain kept perfect pace at 23.976 fps while the video output
            # managed 18.6 — decode idle, presentation drowning. Raising the refresh rate to
            # fix cadence therefore made it dramatically worse, because it doubled the work.
            #
            # The cadence problem it was meant to solve is better fixed by the display mode,
            # which costs nothing per frame: at 59.94 Hz, 29.97 fps content lands on exactly
            # two refreshes and 23.976 on the standard 3:2 film pattern.
            "--cache=yes",
            # Sized for a bad minute on the NAS, not a good one. Measured while Plex was
            # running intro detection over the same share: 3.5 MB/s, against the 5-7 MB/s a
            # 4K HEVC film wants. The old 64 MiB was about ten seconds of one of those, so
            # any sustained dip emptied it and the picture stuttered — which reads as a
            # decode problem and is a supply problem. 512 MiB is a couple of minutes of
            # headroom and a fraction of this box's memory.
            "--demuxer-max-bytes=512MiB",
            "--demuxer-max-back-bytes=64MiB",
            # Seconds, not bytes. A byte cap alone buffers least where it is needed most: the
            # same 64 MiB is minutes of a DVD rip and seconds of a remux.
            "--demuxer-readahead-secs=60",
            f"--input-ipc-server={self.socket_path}",
        ]

        # Casting from a phone hands this player a YouTube URL, and mpv resolves those with
        # yt-dlp — but only if it can find one. Debian's packaged yt-dlp is old enough to
        # break against YouTube regularly, so the build virtualenv's copy is preferred and
        # PATH is the fallback.
        #
        # The format cap is the same lesson the library learned the hard way: this box drops
        # roughly half the frames of a 4K HEVC file, and YouTube will happily serve 2160p
        # VP9 to anything that asks. Pinning to AVC at 1080p keeps casting inside what the
        # hardware decoder can actually do.
        ytdl = self._find_ytdl()
        if ytdl:
            args.append(f"--script-opts=ytdl_hook-ytdl_path={ytdl}")
            args.append(
                "--ytdl-format=bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]"
                "/best[height<=1080]"
            )

        # Which display mode to drive, when rendering straight to KMS.
        #
        # mpv picks its own mode through its DRM context, from whatever the connector calls
        # preferred — here 3840x2160@30. `video=` on the kernel command line does not change
        # that; it sets the console framebuffer, which is why setting it looked like it should
        # work, reached the kernel intact, and changed nothing on screen.
        #
        # The mode matters more than the resolution. 30 Hz divides into neither 23.976 nor
        # 29.97, so every film and every television episode is shown with some frames held
        # for one refresh and some for two. Nothing drops and nothing starves; it simply
        # judders, which is why it reads as a decode or buffering fault and is neither.
        if self.drm_mode:
            args.append(f"--drm-mode={self.drm_mode}")

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

    @staticmethod
    def _find_ytdl() -> str | None:
        """Where yt-dlp lives, if anywhere.

        Ordered deliberately. The virtualenv copy is installed from PyPI and can be updated
        the day YouTube changes something; a distribution package cannot, and a stale yt-dlp
        does not degrade — it stops resolving videos entirely.
        """
        import shutil

        candidate = Path(__file__).resolve().parent.parent / ".venv-build" / "bin" / "yt-dlp"
        if candidate.exists():
            return str(candidate)
        return shutil.which("yt-dlp")

    def release(self) -> None:
        """Quit mpv and give up the display, leaving this object reusable.

        The only way to hand DRM master to another program. mpv holds it for the life of its
        video output and offers no way to drop it — there is no property, no command, and no
        signal. So the player goes away entirely and comes back afterwards.

        That costs a relaunch on the return, a second or two, which is the correct trade
        against a session measured in minutes and no trade at all against the alternative,
        which is that AirPlay cannot exist on a box with no compositor.
        """
        self.close()
        self.alive = False
        self.proc = None
        self._sock = None

    def resume(self, timeout: float = 10.0) -> None:
        """Come back after `release`, as a genuinely fresh process.

        Every field reset here is per-process state that would otherwise be a lie about the
        new mpv: a request counter it never issued, a half-read reply from the old socket, a
        fullscreen flag claiming a window that no longer exists.

        `_splash_dismissed` is the exception and is set the other way on purpose. The boot
        splash was taken down hours ago; a fresh player must not go looking for plymouth
        again on its first frame.
        """
        self._buffer = b""
        self._request_id = 0
        self._fullscreen_applied = False
        self._splash_dismissed = True
        self._backdrop = False
        self._dropping = False
        self.alive = True
        self.start(timeout=timeout)

    # ---------- IPC ----------

    def _send(self, payload: dict) -> None:
        if not self._sock or not self.alive:
            raise MpvUnavailable("not connected")
        try:
            self._sock.sendall((json.dumps(payload) + "\n").encode())
        except TimeoutError:
            # A full socket buffer, which means mpv is *busy*, not gone. The socket carries a
            # 50ms timeout, and treating that as death is what actually took the tuner down
            # when the listings were pushing five kilobytes four times a second: `alive` went
            # false, the run loop saw a dead player and shut down, and systemd restarted the
            # television — seven times in half an hour.
            #
            # Raised rather than swallowed so `_command` can drop the one command; the caller
            # loses an overlay for a moment, which is the correct price.
            raise
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
        try:
            with self._write_lock:
                self._request_id += 1
                request_id = self._request_id
                self._send({"command": command, "request_id": request_id})
        except (OSError, TimeoutError) as exc:
            # A write that cannot complete must not reach the run loop. mpv's IPC socket has
            # a finite buffer, and a caller that pushes faster than mpv drains — the listings
            # overlay was sending five kilobytes four times a second — fills it, blocks the
            # write, and times out. That exception propagated out of the tick and killed the
            # tuner, so tuning to the guide took the television down and systemd restarted it.
            #
            # A dropped command is a missing overlay for a moment. A raised one is a black
            # screen, so this is never the right thing to be strict about.
            #
            # Logged on the transition only. A blocked socket drops every command until it
            # clears, and a line each would put hundreds of identical entries in the journal
            # — burying the one that says when it started.
            if not self._dropping:
                self._dropping = True
                print(f"  mpv is not draining; dropping commands "
                      f"({command[0] if command else '?'}: {exc})")
            return None
        if self._dropping:
            self._dropping = False
            print("  mpv is draining again")
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
        if started:
            self._pace_to_source()
            self._dismiss_splash()
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

    # A synthetic video track, for channels whose content is audio only.
    #
    # An ASS overlay is composited onto the video surface, so with an audio-only file there is
    # nothing to draw on and the listings simply do not appear — the guide came up playing its
    # music with an empty screen, and every counter said healthy because nothing had failed.
    # `--force-window` is not enough: it makes a window, not a surface for the OSD.
    #
    # `lavfi-complex` synthesises one from a colour source at 12 fps, which costs nothing to
    # "decode" and gives libass something to render into. The colour is the house ink, so the
    # frame behind the grid matches the grid's own background exactly.
    BACKDROP = "[aid1]anull[ao];color=c=0x0E0C14:s=1920x1080:r=12[vo]"

    def play_loop(self, paths: list[Path], *, backdrop: bool = False,
                  timeout: float = 8.0) -> TuneResult:
        """Loop a playlist forever — the guide's music bed, or an ambiance channel's video.

        Deliberately not `tune`. That one punches into a single file at an offset because a
        scheduled programme is somewhere in the middle of itself when you arrive; the guide's
        music is not on a timetable and nobody can tune in late to it. Handing mpv the whole
        playlist with `loop-playlist` also means track durations never have to be probed —
        which matters, because probing a folder of music over the NAS is exactly the kind of
        wait that would sit between pressing 2 and seeing anything.

        `keep-open` is off so the playlist advances by itself, and `force-window` is on
        because an audio file presents no video and mpv would otherwise tear the window down
        and take the listings overlay with it.
        """
        if not self.alive:
            return TuneResult(False, 0.0, "mpv exited")
        if not paths:
            return TuneResult(False, 0.0, "no music")

        start = time.perf_counter()
        self._command(["set_property", "force-window", "yes"], wait=False)
        self._command(["set_property", "keep-open", "no"], wait=False)
        self._command(["set_property", "loop-playlist", "inf"], wait=False)

        response = self._command(["loadfile", str(paths[0]), "replace"], timeout=timeout)
        if response is not None and response.get("error") not in (None, "success"):
            return TuneResult(False, 0.0, str(response.get("error")))
        for extra in paths[1:]:
            self._command(["loadfile", str(extra), "append"], wait=False)

        # After the load: setting it before is discarded when the file is replaced.
        if backdrop:
            self._command(["set_property", "lavfi-complex", self.BACKDROP], wait=False)
            self._backdrop = True

        self._command(["set_property", "pause", False], wait=False)
        started = self._wait_for_event("playback-restart", timeout)
        if started:
            self._dismiss_splash()
        latency_ms = (time.perf_counter() - start) * 1000.0
        return TuneResult(started, latency_ms, None if started else "no playback-restart")

    def show_backdrop(self, *, timeout: float = 8.0) -> TuneResult:
        """A picture with nothing in it, so an overlay has somewhere to live.

        The music path gets its surface from `lavfi-complex` layered over the audio. With no
        music there is no file at all, and mpv sitting idle presents nothing — so the guide
        would draw its listings into a void and the screen would keep showing whatever
        channel you came from.

        `av://lavfi:` plays a filter graph as though it were a file, so the colour source
        becomes the thing being played rather than something layered onto it. Infinite by
        nature, which is why it loops on `loop-file` rather than `loop-playlist`.
        """
        if not self.alive:
            return TuneResult(False, 0.0, "mpv exited")
        start = time.perf_counter()
        self._command(["set_property", "force-window", "yes"], wait=False)
        self._command(["set_property", "keep-open", "no"], wait=False)
        self._command(["set_property", "loop-file", "inf"], wait=False)
        response = self._command(
            ["loadfile", "av://lavfi:color=c=0x0E0C14:s=1920x1080:r=12", "replace"],
            timeout=timeout,
        )
        if response is not None and response.get("error") not in (None, "success"):
            return TuneResult(False, 0.0, str(response.get("error")))
        self._backdrop = True
        self._command(["set_property", "pause", False], wait=False)
        started = self._wait_for_event("playback-restart", timeout)
        if started:
            self._dismiss_splash()
        return TuneResult(started, (time.perf_counter() - start) * 1000.0,
                          None if started else "no playback-restart")

    def clear_loop(self) -> None:
        """Undo `play_loop`, so a scheduled channel does not inherit its looping or backdrop.

        The backdrop especially: a synthetic video track left in place would sit in front of
        the next channel's actual picture, which is a far louder failure than the blank guide
        it was added to fix.
        """
        if not self.alive:
            return
        self._command(["set_property", "loop-playlist", "no"], wait=False)
        if self._backdrop:
            self._command(["set_property", "lavfi-complex", ""], wait=False)
            self._command(["set_property", "loop-file", "no"], wait=False)
            self._backdrop = False

    # The coordinate space overlay ASS is authored in. Passing 0,0 here lets mpv choose,
    # which means the same menu renders at wildly different sizes depending on the display —
    # enormous on a retina panel, unreadable on a CRT. Declaring the space explicitly makes
    # mpv scale it to fit, so the menu occupies the same fraction of the screen everywhere.
    OVERLAY_RES = (1920, 1080)

    # Above this width, retiming to the display costs more than it buys.
    RESAMPLE_MAX_WIDTH = 2048

    def _dismiss_splash(self) -> None:
        """Take the boot splash down, now that there is television behind it.

        Plymouth normally quits when boot finishes, which on this box is eleven seconds —
        and a Samsung Frame spends several of those negotiating HDMI. So the mark was drawn
        correctly to a screen that was not yet watching, and what the viewer saw was black,
        then a login prompt, then a programme. Verified by hand: `plymouth show-splash` on a
        running system puts the mark up perfectly.

        Holding it until the first frame presents is also just the right chain — power, mark,
        television, with nothing in between. Best effort in every direction: no plymouth, no
        splash running, or no permission all mean there is simply nothing to take down.
        """
        if self._splash_dismissed:
            return
        self._splash_dismissed = True
        try:
            subprocess.Popen(["plymouth", "quit"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, ValueError):
            pass

    def _pace_to_source(self) -> None:
        """Pick a frame-pacing strategy for the file that just started.

        This library has three frame rates — 23.976 film, 25.0 PAL, 29.97 NTSC — and a fixed
        refresh divides evenly into at most one of them. At 59.94, NTSC lands on exactly two
        refreshes; film gets the familiar 3:2 pattern; and PAL gets 2.398, which is the worst
        of the three and is most of the British children's programming on this dial.

        `display-resample` fixes that by retiming playback to the display's own clock instead
        of the audio clock, and resampling audio to match. Measured here on 1080p: a full
        59.96 fps presented, zero drops.

        It is not free, and the cost scales with picture size, because it renders once per
        *refresh* rather than once per source frame. On a 4K file that means a
        3840x2160 -> 1920x1080 scale sixty times a second, and the same measurement collapses
        to 18.6 fps presented — visibly worse than the judder it was meant to cure. Hence the
        width test rather than a global setting: almost the whole library is at or below
        1080p and gets the smooth path; the handful of 4K films keep the cheap one.
        """
        width = self.get_property("width")
        try:
            wide = int(width) > self.RESAMPLE_MAX_WIDTH
        except (TypeError, ValueError):
            wide = False          # unknown: assume it is fine, and prefer smooth
        self._command(["set_property", "video-sync",
                       "audio" if wide else "display-resample"], wait=False)

    def stop(self) -> None:
        """Stop playback and leave mpv idle — still owning the screen, showing nothing.

        Deliberately not `quit`. This process holds the display for the life of the box and
        restarting it is the single largest cost in a channel change; standby must not pay
        that on the way back. Deliberately not `pause` either: a paused television is a frozen
        frame, which reads as a fault rather than as off.
        """
        if not self.alive:
            return
        self._command(["stop"], wait=False)

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

    def set_subtitles(self, on: bool) -> None:
        """Show or hide subtitles, now and for everything tuned afterwards.

        Two properties, because one is not enough. `sub-visibility` governs the current
        file; `sid` governs which track gets selected when the *next* file loads, and mpv
        resets visibility per file. Setting only the first turns subtitles on for the
        programme you are watching and off again at the next advert.
        """
        self._command(["set_property", "sub-visibility", "yes" if on else "no"], wait=False)
        self._command(["set_property", "sid", "auto" if on else "no"], wait=False)

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
