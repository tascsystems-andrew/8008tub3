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
_cache_stamp: tuple[float, int] | None = None


def _load() -> dict:
    """The map, held in memory but reloaded when the file underneath it changes.

    Held because it is read on every tune and every guide redraw, and re-parsing a few
    thousand entries at that rate would be absurd. Reloaded because the map is *built while
    the box is running* — the walk crosses the VPN and takes the better part of an hour — and
    a cache that never looked again meant new titles appeared only after a restart, which
    nobody would connect back to the build that had just finished.

    `stat` is a syscall against local disk. Checking costs far less than parsing.
    """
    global _cache, _cache_stamp
    try:
        info = TITLES.stat()
        stamp = (info.st_mtime, info.st_size)
    except OSError:
        stamp = None

    if _cache is not None and stamp == _cache_stamp:
        return _cache

    _cache_stamp = stamp
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
    # Audio and codec tags carry their own version numbers — AAC2.0, DDP5.1, DTS-HD — so a
    # plain word list never matches them. Strip the family plus whatever digits follow.
    text = re.sub(r"\b(AAC|DDP|DD|DTS|EAC3|AC3|TrueHD|Atmos|FLAC|MP3|Opus)[\d.\-]*\b",
                  " ", text, flags=re.I)
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

    # Plex knows the real episode title, because it matched the file against TVDB. Filename
    # parsing is the fallback and only the fallback: a release name frequently has no episode
    # title in it at all — `PAW.Patrol.S01E01.1080p.WEB.x264-CRiMSON-postbot` carries a
    # resolution, a codec, a group and a bot, and nothing a viewer wants — and any scraper
    # confident enough to find a title in that will find one in noise too.
    if entry and entry.get("episode"):
        return (entry.get("show") or clean(stem))[:44] or "—", str(entry["episode"])[:52]

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

    # No SxxExx. Nobody watching television needs the episode number, and a broadcast bug
    # never carried one — it is a filing detail that leaked out of the filename.
    #
    # The episode title is only shown when it looks like a title. Rippers put all sorts
    # after the marker — quality tags, group names, dates, the same words again — and a
    # confident-looking line of noise is worse than a blank one, because the viewer reads
    # it as the name of what they are watching.
    return show[:44] or "—", detail[:52] if _plausible(detail) else ""


# Words that only ever appear in a release name. Not an exhaustive list of scene groups —
# that is unwinnable — but the shapes that survive `clean` and then read as a title.
# "Obfuscated", "postbot" and "proper" are real English words, which is exactly why the
# generic word-shape tests let them through.
RELEASE_WORDS = frozenset({
    "web", "webrip", "webdl", "bluray", "brrip", "bdrip", "hdrip", "dvdrip", "hdtv", "pdtv",
    "remux", "upscaled", "obfuscated", "postbot", "proper", "repack", "internal", "extended",
    "remastered", "signature", "edition", "commentary", "comm", "subs", "dual", "audio",
    "bit", "ch", "hevc", "avc", "sdtv", "dvd", "bd", "ip", "amzn", "dsnp", "hmax", "nf",
    "pcok", "iso", "rarbg", "yify", "yts", "galaxyrg", "tgx", "crimson", "megusta", "psa",
    "pof", "ivy", "lama", "evo", "flux", "swtyblz", "markii", "predikat", "anoxmous",
    "nikt0", "x0r", "d3g", "amiable", "kingdom", "playnow", "cbfm", "rng", "creed", "hbd",
    "salt", "esq", "srs", "ngp", "gaz", "deceit", "thc", "megatron", "animerg", "jrr", "v2",
})


def _plausible(text: str) -> bool:
    """Is this an episode title, or is it debris left over from a release name?

    Biased towards saying no. A blank second line on the bug is unremarkable; a confident
    line reading "Web 2 crimson obfuscated" is read as the name of what you are watching,
    and is worse than showing nothing at all.
    """
    if len(text) < 3 or len(text) > 60:
        return False
    words = text.split()
    if not words:
        return False
    # Mostly digits, or a lone token in block capitals: a group tag or a date, not a title.
    letters = sum(character.isalpha() for character in text)
    if letters < len(text.replace(" ", "")) * 0.6:
        return False
    if len(words) == 1 and text.isupper():
        return False
    # A real title has at least one proper word in it. "AAC2 0" and "x264 SRS" do not.
    if not any(len(word) >= 4 and word.isalpha() for word in words):
        return False

    # Any release vocabulary at all disqualifies the whole string. An episode really called
    # "Dual Audio" does not exist; a release tagged that way is most of this library.
    lowered = [word.strip("()[[]{}").lower() for word in words]
    if any(word in RELEASE_WORDS for word in lowered):
        return False
    # Two real words minimum. Single survivors are almost always a group name that happened
    # to look like English.
    if sum(1 for word in lowered if len(word) >= 3 and word.isalpha()) < 2:
        return False
    return True


def build(pool_root: Path, plex_client=None) -> dict:
    """Resolve every pooled file to a show and episode name, once, at build time."""
    from tub3.plex import (episode_index, fill_show_paths, lookup as plex_lookup,
                           lookup_episode, path_index)

    index = {}
    episodes: dict = {}
    if plex_client is not None:
        try:
            library = plex_client.library()
            # Shows arrive with no path of their own; derive it from their episodes, or the
            # show-level fallback below can never match a series folder.
            fill_show_paths(plex_client, library)
            index = path_index(library)
            # One request per show, not per episode: `/allLeaves` returns the whole series.
            episodes = episode_index(plex_client, library)
        except Exception:  # noqa: BLE001 - a title map is a nicety, never a blocker
            index, episodes = {}, {}

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
            # The episode is the precise answer and is keyed on the file itself, so it is
            # tried first. The show lookup below is the fallback for films and for anything
            # Plex has not matched.
            episode = lookup_episode(episodes, real) if episodes else None

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

            if episode is None and item is None:
                continue

            entry: dict = {}
            if item is not None:
                entry.update({"show": item.title, "rating": item.content_rating,
                              "year": item.year})
            if episode is not None:
                entry["show"] = episode.show or entry.get("show", "")
                entry["episode"] = episode.title
                entry["season"] = episode.season
                entry["number"] = episode.episode
            out[str(real)] = entry
    return out
