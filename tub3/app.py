"""Turn the box on.

One entry point for both targets. The difference between a Mac and a Raspberry Pi here is
two lines of driver selection — everything above that is identical, which is the whole reason
the box is Python plus ffmpeg plus mpv rather than anything platform-specific.

Boots straight into a channel. No menu, no login, no "choose a source": the appliance rule is
that the first thing you see is television.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
from pathlib import Path

from tuner.box import Box
from tuner.input import CecDriver, Driver, EvdevDriver, MpvKeyDriver, discover_evdev_devices
from tuner.player import MpvPlayer, MpvUnavailable
from tuner.schedule import LiquidChannel, Lineup

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "FieldStation42"
DB = VENDOR / "runtime" / "fs42_fluid.db"


def discover_channels(db: Path) -> list[tuple[int, str]]:
    """Find every station that actually has a schedule, and its channel number.

    Read from the schedule rather than the configs: a station with no generated blocks is
    not a channel you can tune to, and showing it would give the viewer dead air.
    """
    if not db.exists():
        return []

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        stations = [row[0] for row in conn.execute(
            "SELECT DISTINCT station FROM liquid_blocks"
        )]
    except sqlite3.DatabaseError:
        # The file exists but the schedule table does not — which is the *normal* state
        # for several minutes during a build, because the catalog tables are written well
        # before liquid_blocks. Checking only for the file meant the tuner crashed on an
        # OperationalError every two seconds while the standby card polled for a schedule,
        # i.e. exactly while the screen said "Building your channel".
        #
        # Also covers a half-written or corrupt database: no channels is the correct
        # answer to "what can I tune to" in every one of those cases.
        return []
    finally:
        conn.close()

    numbers: dict[str, int] = {}
    for conf_path in sorted((VENDOR / "confs").glob("*.json")):
        try:
            conf = json.loads(conf_path.read_text()).get("station_conf", {})
        except (json.JSONDecodeError, OSError):
            continue
        name = conf.get("network_name")
        number = conf.get("channel_number")
        if name and isinstance(number, int):
            numbers[name] = number

    found = [(numbers.get(name, index + 2), name) for index, name in enumerate(sorted(stations))]
    return sorted(found)


def local_ip() -> str:
    """The address to type into a browser.

    Opening a UDP socket toward a public address picks the interface the OS would actually
    route through, without sending a packet. `gethostbyname(gethostname())` is the obvious
    alternative and is wrong on any box with more than one interface — it happily returns
    127.0.0.1, which is exactly the address that does not help someone holding a phone.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))   # TEST-NET-1: reserved, never routed
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def menu_state(db: Path, web_port: int) -> dict:
    """What the on-screen menu shows. Facts, not settings — this screen cannot type."""
    address = local_ip()
    state = {
        "net": "Connected" if address != "127.0.0.1" else "No network",
        "addr": f"{address}:{web_port}",
        "version": "0.1",
        "remote": "keys + clicker",
    }

    settings_path = Path(__file__).resolve().parent.parent / "settings.json"
    ads_dir = ""
    if settings_path.exists():
        try:
            ads_dir = json.loads(settings_path.read_text()).get("commercials_dir", "")
        except (json.JSONDecodeError, OSError):
            pass

    if ads_dir and Path(ads_dir).exists():
        from .bootstrap import VIDEO_SUFFIXES
        spots = sum(
            1 for p in Path(ads_dir).rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
        )
        state["ads"] = f"{spots:,} spots"
    else:
        state["ads"] = "none yet"

    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM liquid_blocks").fetchone()[0]
            state["library"] = f"{count:,} blocks scheduled"
        finally:
            conn.close()
    return state


def build_drivers(player: MpvPlayer, *, headless: bool) -> list[Driver]:
    """Every input that might exist, all live at once.

    The viewer should never have to pick. A CEC remote and a clicker both work with no
    configuration, and if CEC turns out to be broken on a given TV the keys keep working.
    """
    drivers: list[Driver] = [MpvKeyDriver(player.socket_path)]

    cec = CecDriver()
    if cec.available():
        drivers.append(cec)

    if headless:
        # Linux appliance: read the input devices directly, so the remote works with no X
        # server and no window focus.
        for path, name in discover_evdev_devices():
            try:
                drivers.append(EvdevDriver(path))
            except OSError:
                continue

    return drivers


def wait_for_channels(player: MpvPlayer, db: Path, web_port: int,
                      poll: float = 0.25) -> list[tuple[int, str]] | None:
    """Hold the standby card until a schedule exists, then return the channels.

    Returns None if the player goes away — the user quit, or mpv died — so the caller can
    exit cleanly rather than spinning against a dead socket.

    The screen redraws four times a second because it carries a moving sweep. That is the
    entire point of it: building a schedule across a network share takes minutes with no
    natural progress signal, and a still screen is indistinguishable from a hung one. The
    cost is an IPC message per redraw, which is nothing next to a user power-cycling the
    box halfway through a build because they thought it had died.
    """
    import time

    from tuner.standby import Standby

    card = Standby(web_url=f"http://{local_ip()}:{web_port}")
    print("  no channels yet — showing the standby card")

    building_since = 0.0
    last_check = 0.0
    while True:
        now = time.time()

        # Checking the database is cheap; checking for a running build is a process scan,
        # so do both on a slower clock than the redraw.
        if now - last_check >= 2.0:
            last_check = now
            channels = discover_channels(db)
            if channels:
                player.hide_overlay(2)
                print(f"  schedule appeared — {len(channels)} channel(s)")
                return channels

            building = _rebuild_running()
            if building and not building_since:
                building_since = now
            elif not building:
                building_since = 0.0

            card.building = bool(building_since)
            if building_since:
                done, total = _build_progress(db)
                card.headline = "Building your channel"
                if total:
                    card.progress = min(1.0, done / total)
                    left = ""
                    elapsed = now - building_since
                    if done > 20 and elapsed > 30:
                        remaining = (total - done) / (done / elapsed)
                        left = f" — about {max(1, int(remaining // 60))} min left"
                    card.detail = f"Read {done:,} of {total:,} files{left}"
                else:
                    card.progress = None
                    minutes = int((now - building_since) // 60)
                    card.detail = ("Reading your library over the network"
                                   + (f" — {minutes} min so far" if minutes else ""))
            else:
                card.headline = "No channels yet"
                card.detail = "Open the settings page to point this at your shows."

        try:
            player.show_overlay(card.render_ass(now), overlay_id=2)
        except (OSError, BrokenPipeError, RuntimeError):
            return None
        time.sleep(poll)


MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"


def _build_progress(db: Path) -> tuple[int, int]:
    """(files catalogued, files to catalogue) — a real number for the screen.

    The total comes free: the symlink pools are built *before* cataloguing starts, and they
    live on the local disk, so counting them costs nothing. The done count is rows in the
    catalog table. No coordination between the two processes, no progress file to go stale.

    Worth having because the honest rate over a network share is about one file a second —
    twenty-plus minutes for a modest library — and "reading your library" with no number
    attached is indistinguishable from a hang for the entire duration.
    """
    total = 0
    try:
        for pool in MEDIA_ROOT.iterdir():
            if pool.is_dir():
                total += sum(1 for _ in pool.iterdir())
    except OSError:
        return 0, 0

    done = 0
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                done = conn.execute("SELECT COUNT(*) FROM file_meta").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            done = 0
    return done, total


def _rebuild_running() -> bool:
    """Is a schedule build in flight? Used only to change what the screen says."""
    import subprocess

    try:
        result = subprocess.run(["pgrep", "-f", "tub3[.]bootstrap"],
                                capture_output=True, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3", description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--channel", type=int, default=None, help="start on this channel")
    ap.add_argument("--fullscreen", action="store_true",
                    help="take over the whole screen (default: windowed)")
    ap.add_argument("--windowed", action="store_true",
                    help="force windowed even if the setting says otherwise")
    ap.add_argument("--headless", action="store_true",
                    help="Linux appliance mode: also read input devices directly")
    ap.add_argument("--vo", default=None, help="mpv video output driver")
    ap.add_argument("--web-port", type=int, default=8008,
                    help="port shown on screen for the settings page")
    ap.add_argument("--no-web", action="store_true",
                    help="do not start the settings server")
    args = ap.parse_args(argv)

    channels = discover_channels(args.db)
    print(f"\n  {len(channels)} channel(s)")
    for number, name in channels:
        print(f"    {number:>3}  {name}")

    # Windowed by default. An appliance wants the whole screen; a laptop you are also
    # working on does not, and a program that seizes the display on launch is a
    # program you stop opening. The Pi image ships with this turned on.
    from .web import load_settings  # noqa: PLC0415
    wants_fullscreen = bool(load_settings().get('fullscreen', False))
    if args.fullscreen:
        wants_fullscreen = True
    if args.windowed:
        wants_fullscreen = False
    player = MpvPlayer(fullscreen=wants_fullscreen, video_output=args.vo)
    try:
        player.start()
    except MpvUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.no_web:
        from .web import serve  # noqa: PLC0415

        def _serve() -> None:
            # A settings page that cannot bind is an inconvenience. A television that
            # will not start because a settings page could not bind is a fault. The
            # commonest cause is a previous instance still holding the port, which is
            # exactly when you least want the box to refuse to come up.
            try:
                serve("0.0.0.0", args.web_port)
            except OSError as exc:
                print(f"  settings server unavailable: {exc}")

        threading.Thread(target=_serve, daemon=True).start()
        print(f"  settings: http://{local_ip()}:{args.web_port}")

    # Nothing configured yet: hold the standby card until a schedule appears, rather than
    # exiting. Exiting meant systemd restarted the process every three seconds and the TV
    # showed a Linux login prompt — the state every first-time user starts in.
    if not channels:
        channels = wait_for_channels(player, args.db, args.web_port)
        if channels is None:
            return 0

    lineup = Lineup([
        LiquidChannel(number, name, args.db, name)
        for number, name in channels
    ])

    drivers = build_drivers(player, headless=args.headless)
    print(f"  input: {', '.join(d.name for d in drivers)}")
    print("\n  UP/DOWN change channel · ENTER opens the menu · ESC closes it · Q quits\n")

    box = Box(lineup, player, start_channel=args.channel or channels[0][0],
              state=menu_state(args.db, args.web_port))
    try:
        box.run(drivers)
    except KeyboardInterrupt:
        pass
    finally:
        box.running = False
        for driver in drivers:
            driver.close()
        player.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
