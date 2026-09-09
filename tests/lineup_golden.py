"""Build the golden artefacts for `test_lineup.py`, and rebuild them to compare.

Hermetic on purpose. The real dial lives on the box and carries the household's media paths,
so the fixture beside this file is the same *shape* with the mount point stripped, and the
media tree it names is synthesised into a temporary directory at run time. Nothing here
touches the share, Plex, or the box.

Two artefacts, because one is not enough:

  confs   what `compile_station` emits per channel. This is what upstream actually reads.
  pools   what `_link_pool` would link, per tag. This matters because `compile_station` never
          looks inside `sources` — so a change that drops an `exclude` list, or reorders a
          source list, produces byte-identical configs and a different channel.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from tub3 import lineup as L                                    # noqa: E402

FIXTURE = HERE / "fixtures" / "lineup.json"
SOURCES = HERE / "fixtures" / "sources.txt"
GOLDEN = HERE / "golden"
PLACEHOLDER = "TUB3_FIXTURE_MEDIA"


def plant(root: Path) -> Path:
    """Synthesise the media tree the fixture names.

    Two ordinary videos per folder, plus one per `exclude` token that the real lineup uses.
    The decoys are the point: without a file whose name a rule actually matches, the pool
    artefact would be identical whether the exclude lists were honoured or silently dropped,
    which is exactly the regression it exists to catch.
    """
    media = root / "share"
    fixture = json.loads(FIXTURE.read_text())
    decoys = {}
    for channel in fixture.get("channels", []):
        for tag, tokens in (channel.get("exclude") or {}).items():
            for folder in (channel.get("sources") or {}).get(tag, []):
                decoys.setdefault(folder, set()).update(tokens)

    for line in SOURCES.read_text().splitlines():
        if not line.strip():
            continue
        folder = media / line.replace(PLACEHOLDER + "/", "")
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("aa-ep01.mp4", "bb-ep02.mp4"):
            (folder / name).touch()
        for token in sorted(decoys.get(line, ())):
            safe = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in token)
            (folder / f"zz {safe} decoy.mp4").touch()
    return media


def load(media: Path):
    """The fixture, with its placeholder pointed at the planted tree."""
    raw = FIXTURE.read_text().replace(PLACEHOLDER, str(media))
    tmp = media.parent / "lineup.json"
    tmp.write_text(raw)
    return L.load(tmp)


def build(root: Path) -> dict:
    """Everything the golden test compares, from a clean temporary root."""
    media = plant(root)
    channels = load(media)
    pool_root = root / "pools"
    pool_root.mkdir(parents=True, exist_ok=True)

    pools = {}
    for channel in channels:
        for tag, folders in channel.sources.items():
            if tag in pools:
                continue
            L._link_pool(pool_root, tag, folders, channel.exclude.get(tag))
            linked = sorted(p.name for p in (pool_root / tag).iterdir() if p.is_symlink())
            pools[tag] = linked

    confs = {}
    templates = {}
    for channel in channels:
        if channel.kind == "guide":
            continue
        conf = L.compile_station(channel, pool_root,
                                 pools={"kids": "ads-kids", "family": "ads-family",
                                        "late": "ads-late"})
        # The pool root is a temp path; it must not reach the golden.
        confs[str(channel.number)] = json.loads(
            json.dumps(conf).replace(str(pool_root), "<POOLS>"))
        names, byday = L._day_templates(channel)
        templates[str(channel.number)] = {"templates": sorted(names), "days": byday}

    return {"confs": confs, "pools": pools, "templates": templates}


def write(root: Path) -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    data = build(root)
    for name, payload in data.items():
        (GOLDEN / f"{name}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        write(Path(tmp))
    for f in sorted(GOLDEN.glob("*.json")):
        print(f"  wrote {f.relative_to(HERE.parent)}  ({f.stat().st_size} bytes)")
