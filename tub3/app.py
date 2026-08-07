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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3", description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--channel", type=int, default=None, help="start on this channel")
    ap.add_argument("--windowed", action="store_true", help="do not go fullscreen")
    ap.add_argument("--headless", action="store_true",
                    help="Linux appliance mode: also read input devices directly")
    ap.add_argument("--vo", default=None, help="mpv video output driver")
    ap.add_argument("--web-port", type=int, default=8008,
                    help="port shown on screen for the settings page")
    ap.add_argument("--no-web", action="store_true",
                    help="do not start the settings server")
    args = ap.parse_args(argv)

    channels = discover_channels(args.db)
    if not channels:
        print("No channels have a schedule yet.\n"
              "  Build one:  python3 -m tub3.bootstrap --programs DIR --ads DIR "
              "--media-root DIR --channel 3", file=sys.stderr)
        return 1

    lineup = Lineup([
        LiquidChannel(number, name, args.db, name)
        for number, name in channels
    ])

    print(f"\n  {len(channels)} channel(s)")
    for number, name in channels:
        print(f"    {number:>3}  {name}")

    player = MpvPlayer(fullscreen=not args.windowed, video_output=args.vo)
    try:
        player.start()
    except MpvUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.no_web:
        from .web import serve  # noqa: PLC0415
        threading.Thread(target=serve, args=("0.0.0.0", args.web_port),
                         daemon=True).start()
        print(f"  settings: http://{local_ip()}:{args.web_port}")

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
