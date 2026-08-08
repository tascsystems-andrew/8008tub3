"""Proper programme names for the on-screen bug.

A filename is not a title. `kidstv__Bill.Nye.-..The.Science.Guy.S04E12.SDTV.Ocean.Life` is
the accumulated residue of a scene release, a pool prefix and a download client, and putting
it on a television undoes a lot of other work.

Plex already knows the real name, because it matched the library against the standard
databases. But the tuner must not ask Plex anything: a channel change is 17 ms and a network
round trip is 38 ms on this box, so a lookup in the tune path would more than triple it and
would fail entirely whenever the NAS was asleep.

So the resolution happens **once, at build time**, into a flat JSON map that the tuner reads
from local disk. Same reasoning as the seek index written into the front of each file: pay
for it when nobody is waiting.

The shape is deliberately conservative. Plex is asked only for the *show* title, which is
the part it is reliably right about. Season, episode and episode-title come from the
filename, which is where they actually live — Plex would need one HTTP call per episode to
answer that, several thousand of them, for a string that is already sitting in the name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TITLES = Path(__file__).resolve().parent.parent / "titles.json"

# S04E12, 4x12, and the bare 0412 some rippers use. Ordered most to least specific.
EPISODE_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})"),
    re.compile(r"(?<![\d])(\d{1,2})x(\d{2,3})(?![\d])"),
    re.compile(r"(?<![\d])(\d{1,2})(\d{2})(?![\d])\s*-\s"),
)

NOISE = ("WEBRip", "WEB-DL", "WEBDL", "BluRay", "HDTV", "DVDRip", "PDTV", "AMZN", "DSNP",
         "NF", "2160p", "1080p", "720p", "480p", "x264", "x265", "H.264", "H264", "HEVC",
         "XviD", "DivX", "AAC", "AC3", "EAC3", "DDP", "DD5", "SDTV", "REPACK", "PROPER",
         "INTERNAL", "REMUX", "10bit", "HDR", "DoVi")

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(TITLES.read_text())
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def parse_episode(name: str) -> tuple[int | None, int | None]:
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(name)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def clean(text: str) -> str:
    """Strip a release name down to words a person would recognise."""
    for noise in NOISE:
        text = re.sub(rf"\b{re.escape(noise)}\b", " ", text, flags=re.I)
    text = text.replace(".", " ").replace("_", " ").replace("-", " ")
    # Trailing release-group tags: anything after the last multi-capital token.
    text = re.sub(r"\b[A-Z]{2,}[0-9]*\b\s*$", "", text)
    return " ".join(text.split())


def episode_title(stem: str) -> str:
    """Whatever follows the SxxExx marker, which is where rippers put the episode name."""
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(stem)
        if match:
            return clean(stem[match.end():])
    return ""


def describe(path: str | Path) -> tuple[str, str]:
    """(headline, subtitle) for the bug. Falls back to a tidied filename, never to nothing.

    Returning a pair rather than one string lets the bug show the series large and the
    episode small, which is how a real channel bug read.
    """
    path = Path(path)
    entry = _load().get(str(path)) or _load().get(str(Path(path).resolve()) if path.exists()
                                                  else str(path))
    stem = path.stem
    if "__" in stem:
        stem = stem.split("__", 1)[1]

    season, number = parse_episode(stem)
    detail = episode_title(stem)

    if entry:
        show = entry.get("show") or clean(stem)
    else:
        # No Plex match: take everything before the SxxExx as the series name.
        show = ""
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(stem)
            if match:
                show = clean(stem[: match.start()])
                break
        show = show or clean(stem)

    if season is not None:
        marker = f"S{season:02d}E{number:02d}"
        subtitle = f"{marker}  {detail}" if detail else marker
    else:
        subtitle = detail

    return show[:44] or "—", subtitle[:52]


def build(pool_root: Path, plex_client=None) -> dict:
    """Resolve every pooled file to a show name, once, at schedule-build time."""
    from tub3.plex import lookup as plex_lookup, path_index

    index = {}
    if plex_client is not None:
        try:
            index = path_index(plex_client.library())
        except Exception:  # noqa: BLE001 - a title map is a nicety, never a blocker
            index = {}

    out: dict[str, dict] = {}
    for pool in sorted(Path(pool_root).iterdir()):
        if not pool.is_dir():
            continue
        for link in pool.iterdir():
            try:
                real = link.resolve()
            except OSError:
                continue
            # Walk up until something matches. An episode lives at
            # <show>/Season 4/<file>, so its own tail is "Show/Season 4" while Plex's show
            # path tail is "Kids TV/Show" — they can never meet. Films sit directly in the
            # library folder, so their parent matches first. Four levels covers both without
            # climbing out of the library.
            item = None
            if index:
                candidate = real
                for _ in range(4):
                    item = plex_lookup(index, candidate)
                    if item is not None:
                        break
                    if candidate.parent == candidate:
                        break
                    candidate = candidate.parent
            if item is None:
                continue
            out[str(real)] = {"show": item.title, "rating": item.content_rating,
                              "year": item.year}
    return out
