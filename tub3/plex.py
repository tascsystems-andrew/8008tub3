"""Read the library from Plex, because Plex already did the hard part.

Classifying content from folder names is guesswork with a safety consequence. Plex has
already matched every item against TMDB/TVDB and holds the thing that actually matters —
`contentRating`, the broadcast rating a standards body assigned — plus genres, year and
duration. Where that exists it is strictly better than any heuristic, and it is the
difference between "this folder is called Kids TV" and "this programme is rated TV-Y".

It also solves the other half. Plex reports each item's **file path**, so its metadata joins
onto the folders on disk with no matching step: the lineup still schedules directories, and
Plex just tells us what is in them.

Two rules govern how this is used, and both exist because the failure here is asymmetric.

**Plex wins when it has an answer; the folder heuristic wins when it does not.** A missing
or unrecognised rating is not permission — it falls back to the path rules, which default
to adult.

**Any adult signal from either source wins.** The two are combined by taking the stricter,
never the more permissive. A disagreement between Plex and the folder layout is exactly the
case where being conservative costs a cartoon and being wrong costs something else.

Talks to Plex over its HTTP API with `X-Plex-Token`. The token is a credential: written by
the settings page mode 0600, excluded from deploys, never placed on a command line, and
never returned to any caller.
"""

from __future__ import annotations

import json
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Beside settings.json rather than under /etc. The token is the user's own credential on
# their own box, and the process that writes it and the process that reads it are the same
# unprivileged user — root ownership would buy nothing and cost a privileged helper.
# Mode 0600, and excluded from deploys so it is never copied off the box.
TOKEN_FILE = Path(__file__).resolve().parent.parent / "plex.json"
TIMEOUT = 20.0

# Plex/standards-body content ratings onto the three-tier ladder the scheduler uses.
# Anything not listed is adult — the list is an allowlist, not a lookup table, so a rating
# nobody anticipated ("TV-MA-S", a regional code, a typo) is treated as unsafe rather than
# as unknown-therefore-fine.
RATING_MAP = {
    # Small children.
    "tv-y": "kids", "tv-y7": "kids", "tv-y7-fv": "kids", "tv-g": "kids",
    "g": "kids", "u": "kids", "e": "kids", "ec": "kids", "0+": "kids", "3+": "kids",
    "6+": "kids", "7+": "kids", "all": "kids", "approved": "kids",
    # Fine with a parent in the room.
    "tv-pg": "family", "pg": "family", "pg-9": "family", "9+": "family", "10+": "family",
    # Everything else, explicitly.
    "pg-13": "adult", "tv-14": "adult", "12": "adult", "12a": "adult", "13+": "adult",
    "14+": "adult", "15": "adult", "16+": "adult", "17+": "adult", "18": "adult",
    "r": "adult", "tv-ma": "adult", "nc-17": "adult", "x": "adult", "nr": "adult",
    "unrated": "adult", "not rated": "adult",
}


@dataclass
class PlexItem:
    title: str
    kind: str                       # "show" | "movie"
    rating: str                     # kids | family | adult
    content_rating: str | None      # what Plex actually said, for the audit trail
    year: int | None
    genres: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    episodes: int = 0
    minutes: float | None = None
    section: str = ""
    # Plex's own id for the item, needed to ask it for the episodes underneath a show.
    rating_key: str = ""


@dataclass
class PlexEpisode:
    """One episode as Plex has it, with the file it lives in."""

    show: str
    title: str
    season: int | None
    episode: int | None
    path: str = ""
    # Seconds, as Plex measured them when it scanned the file. Carried here because it is
    # free — the same response already contains it — and because probing for it later is the
    # most expensive single thing a schedule build does.
    seconds: float = 0.0


class PlexError(RuntimeError):
    pass


def load_config() -> dict:
    """Server address and token, written by the settings page. Never logged."""
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(url: str, token: str) -> None:
    """Write with 0600 from the start.

    Created with the mode rather than chmod'd after: between open and chmod the file is
    briefly world-readable, and a Plex token grants access to the whole library.
    """
    import os as _os

    fd = _os.open(TOKEN_FILE, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    with _os.fdopen(fd, "w") as handle:
        json.dump({"url": url.rstrip("/"), "token": token}, handle)


def classify_rating(content_rating: str | None) -> tuple[str, str]:
    """A Plex content rating onto the ladder, with the reason.

    Returns adult for anything unrecognised. That is the point: an allowlist fails closed,
    and the ratings this will meet in the wild include regional codes and blanks.
    """
    if not content_rating:
        return "adult", "Plex has no content rating for it"
    key = content_rating.strip().lower()
    # Plex prefixes some ratings with a country, e.g. "gb/12A" or "us/TV-14".
    if "/" in key:
        key = key.split("/", 1)[1]
    mapped = RATING_MAP.get(key)
    if mapped:
        return mapped, f"Plex rates it {content_rating}"
    return "adult", f"Plex rates it {content_rating!r}, which is not a rating we recognise"


class Plex:
    def __init__(self, base_url: str, token: str = "", *, verify_tls: bool = False):
        self.base_url = base_url.rstrip("/")
        self._token = token
        # Plex Media Server ships a certificate for *.plex.direct that will not validate
        # against a bare LAN address. This connection is to a box on the user's own
        # network, carrying a library listing.
        self._ctx = None if verify_tls else ssl._create_unverified_context()

    def _get(self, path: str, **params) -> ET.Element:
        # The token is optional. Plex servers commonly allow unauthenticated access from
        # the local network, and this one does — so the box asks for nothing it does not
        # need. A credential you never collect is a credential you cannot mishandle.
        if self._token:
            params["X-Plex-Token"] = self._token
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/xml"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT,
                                        context=self._ctx) as response:
                return ET.fromstring(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                # Distinguish the two 401s. Whether a server answers local clients without
                # a token depends on its Settings > Network > "allowed without auth" list,
                # which is empty on a default install and populated on many NAS packages.
                # So both cases are normal, and telling someone their token was rejected
                # when they never supplied one sends them hunting for the wrong problem.
                if self._token:
                    raise PlexError(
                        "Plex rejected that token. Check it was copied whole."
                    ) from None
                raise PlexError(
                    "This Plex server needs a token: it does not allow clients on the "
                    "local network without one. Open any item in Plex, then the ... menu "
                    "> Get Info > View XML, and copy X-Plex-Token from the address bar."
                ) from None
            if exc.code == 403:
                raise PlexError(
                    "Plex refused the request. The token may belong to an account without "
                    "access to this server's libraries."
                ) from None
            raise PlexError(f"Plex returned HTTP {exc.code}.") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PlexError(f"Could not reach Plex at {self.base_url}: {exc}") from None
        except ET.ParseError:
            raise PlexError("Plex returned something that was not XML.") from None

    def sections(self) -> list[dict]:
        root = self._get("/library/sections")
        return [{"key": d.get("key"), "title": d.get("title"), "type": d.get("type")}
                for d in root.findall(".//Directory")
                if d.get("type") in ("movie", "show")]

    def items(self, section_key: str, section_title: str) -> list[PlexItem]:
        # includeGuids is cheap and makes the result far easier to reconcile later; the
        # heavy per-episode data is deliberately not requested.
        root = self._get(f"/library/sections/{section_key}/all", includeGuids=1)
        out: list[PlexItem] = []

        for node in list(root.findall("Video")) + list(root.findall("Directory")):
            title = node.get("title") or ""
            if not title:
                continue
            content_rating = node.get("contentRating")
            rating, _why = classify_rating(content_rating)
            duration = node.get("duration")
            minutes = round(int(duration) / 60000.0, 1) if duration else None

            paths = [part.get("file") for media in node.findall("Media")
                     for part in media.findall("Part") if part.get("file")]
            # A show's own element carries no Part; its location is a directory instead.
            paths += [loc.get("path") for loc in node.findall("Location") if loc.get("path")]

            out.append(PlexItem(
                title=title,
                kind="movie" if node.tag == "Video" else "show",
                rating=rating,
                content_rating=content_rating,
                year=int(node.get("year")) if (node.get("year") or "").isdigit() else None,
                genres=[g.get("tag") for g in node.findall("Genre") if g.get("tag")],
                paths=[p for p in paths if p],
                episodes=int(node.get("leafCount") or 0) or (1 if node.tag == "Video" else 0),
                minutes=minutes,
                section=section_title,
                rating_key=node.get("ratingKey") or "",
            ))
        return out

    def episodes_of(self, item: PlexItem) -> list[PlexEpisode]:
        """Every episode under one show, with season, number, title and file.

        `/allLeaves` returns the whole series in a single response — season, episode number,
        title and the file each one lives in. That matters: the obvious alternative is a
        request per season or per episode, which for this library is thousands of round trips
        and is why the episode title was being scraped out of the filename instead.

        Failures are swallowed to an empty list on purpose. A missing episode title costs a
        line on the bug; it must never be able to fail a schedule build.
        """
        if not item.rating_key:
            return []
        try:
            root = self._get(f"/library/metadata/{item.rating_key}/allLeaves")
        except PlexError:
            return []

        out: list[PlexEpisode] = []
        for node in root.findall("Video"):
            title = node.get("title") or ""
            season = node.get("parentIndex")
            number = node.get("index")
            # Milliseconds on the Video node, which is where Plex puts the authoritative
            # length; the Media node repeats it but is absent on some items.
            millis = node.get("duration")
            seconds = float(millis) / 1000.0 if (millis or "").isdigit() else 0.0
            for media in node.findall("Media"):
                for part in media.findall("Part"):
                    path = part.get("file")
                    if not path:
                        continue
                    out.append(PlexEpisode(
                        show=item.title,
                        title=title,
                        season=int(season) if (season or "").isdigit() else None,
                        episode=int(number) if (number or "").isdigit() else None,
                        path=path,
                        seconds=seconds,
                    ))
        return out

    def library(self) -> list[PlexItem]:
        items: list[PlexItem] = []
        for section in self.sections():
            items += self.items(section["key"], section["title"] or "")
        return items


def from_config() -> Plex | None:
    config = load_config()
    if not config.get("url"):
        return None
    return Plex(config["url"], config.get("token", ""))


# How many trailing path components to match on. Plex almost always runs in a container, so
# it reports the paths *it* sees — /media/TV/Seinfeld — while the Pi sees the same files at
# /mnt/tub3/Media/mshare/TV/Seinfeld. The prefixes will never agree and asking the user to
# describe their Docker bind mounts is exactly the kind of question this project refuses to
# ask. The tails do agree, so match on those.
SUFFIX_DEPTHS = (3, 2)


def _norm(text: str) -> str:
    """One spelling of a name, so two systems can agree it is the same name.

    The share hands back NFD — `Poke` plus a combining acute — and Plex hands back NFC, where
    the same accent is a single codepoint. The strings look identical in any terminal, compare
    unequal in Python, and so a dict keyed on one never matches a lookup with the other.
    `Pokémon` simply had no Plex match and nobody could see why.

    This matters beyond a missing episode title: `path_index` feeds the *safety audit*, whose
    whole job is to take Plex's rating as a second opinion over a guess from the folder name.
    An accented show silently lost that second opinion, and losing it quietly is precisely the
    failure mode the audit exists to prevent.
    """
    return unicodedata.normalize("NFC", text).lower()


def _suffixes(path: Path) -> list[str]:
    parts = [p for p in path.parts if p not in ("/", "")]
    return [_norm("/".join(parts[-depth:]))
            for depth in SUFFIX_DEPTHS if len(parts) >= depth]


def path_index(items: list[PlexItem]) -> dict[str, PlexItem]:
    """Map path *tails* to items, so a folder can be looked up across a container boundary.

    Keyed by the last two and three components rather than the whole path. A one-component
    key is deliberately not used: folder names like "Season 1" or "Movies" collide across
    libraries, and a wrong match here means an item inherits the wrong rating.

    Ambiguity is resolved by removal, not by picking. If two different items claim the same
    tail, the key is deleted and callers fall back to the path heuristic — which defaults to
    adult. Guessing between two candidates is exactly the wrong instinct in a safety check.
    """
    index: dict[str, PlexItem] = {}
    ambiguous: set[str] = set()

    def add(key: str, item: PlexItem) -> None:
        existing = index.get(key)
        if existing is not None and existing is not item:
            ambiguous.add(key)
            return
        index[key] = item

    for item in items:
        for raw in item.paths:
            path = Path(raw)
            for key in _suffixes(path):
                add(key, item)
            if item.kind == "movie":
                for key in _suffixes(path.parent):
                    add(key, item)

    for key in ambiguous:
        index.pop(key, None)
    return index


def lookup(index: dict[str, PlexItem], path: Path) -> PlexItem | None:
    """Longest tail first, so the most specific match wins."""
    for key in _suffixes(path):
        item = index.get(key)
        if item is not None:
            return item
    return None


# An episode is a *file*, and unlike a folder its basename is already close to unique — a
# release name carries the show, season and episode in it. So one component is allowed here
# where `path_index` refuses it: the failure that rule guards against is a folder called
# "Season 1" matching the wrong series, and no such collision exists between two files called
# `PAW.Patrol.S01E01.1080p.WEB.x264-CRiMSON-postbot.mkv`. Ambiguity is still resolved by
# removal, so the rare genuine clash simply falls through to the filename parser.
EPISODE_DEPTHS = (3, 2, 1)


def _episode_keys(path: Path) -> list[str]:
    """Case-folded, because Plex and the filesystem disagree about capitalisation.

    Observed directly: Plex reports `/Media/Kids TV/PAW Patrol/...` for files that live in
    `Kids TV/Paw Patrol/` on the share. The basename matches either way, so the deepest key
    would have rescued it by luck — folding makes the folder keys work too, and costs nothing
    on a library where two files differing only in case would be a problem in itself.
    """
    parts = [p for p in path.parts if p not in ("/", "")]
    return [_norm("/".join(parts[-depth:]))
            for depth in EPISODE_DEPTHS if len(parts) >= depth]


def fill_show_paths(plex: "Plex", items: list[PlexItem]) -> int:
    """Give every show the folder it actually lives in. Returns how many were filled.

    A show's own element in `/library/sections/{key}/all` carries no `Location` on this
    server — `Mr. Bean` comes back with 19 episodes and an empty path list — so `path_index`
    had nothing to key a series on and `lookup` returned None for every TV folder ever asked
    about. The audit went on working, quietly, on the folder-name rule alone: the Plex second
    opinion it is built around was absent for all nine television channels and said so
    nowhere. That is the exact shape of failure the audit exists to prevent, hiding inside
    the audit.

    The episodes do carry real paths, so the show's folder is their common parent. Derived
    rather than requested because there is no endpoint that will simply answer the question.
    """
    filled = 0
    for item in items:
        if item.kind != "show" or item.paths:
            continue
        folders = {str(Path(episode.path).parent) for episode in plex.episodes_of(item)
                   if episode.path}
        if not folders:
            continue
        # Episodes usually sit in per-season subfolders, so the season directories are the
        # common parent, and the show directory is one above that. Keep both: the deeper
        # keys are harmless and the shallower one is what a lineup source names.
        item.paths = sorted(folders | {str(Path(f).parent) for f in folders})
        filled += 1
    return filled


def episode_index(plex: "Plex", items: list[PlexItem] | None = None) -> dict[str, PlexEpisode]:
    """Map path tails to episodes, one request per show.

    Same container-boundary problem as `path_index` and the same answer: Plex reports the
    paths it sees inside Docker, we see them on a CIFS mount, and only the tails agree.
    """
    if items is None:
        items = plex.library()

    index: dict[str, PlexEpisode] = {}
    ambiguous: set[str] = set()

    for item in items:
        if item.kind != "show":
            continue
        for episode in plex.episodes_of(item):
            for key in _episode_keys(Path(episode.path)):
                existing = index.get(key)
                if existing is not None and existing.path != episode.path:
                    ambiguous.add(key)
                    continue
                index[key] = episode

    for key in ambiguous:
        index.pop(key, None)
    return index


def lookup_episode(index: dict[str, PlexEpisode], path: Path) -> PlexEpisode | None:
    for key in _episode_keys(path):
        episode = index.get(key)
        if episode is not None:
            return episode
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="tub3.plex", description=__doc__)
    ap.add_argument("--url", help="e.g. http://10.0.1.12:32400 (default: stored config)")
    ap.add_argument("--summary", action="store_true", help="counts by rating, not a listing")
    args = ap.parse_args(argv)

    if args.url:
        plex = Plex(args.url, load_config().get("token", ""))
    else:
        plex = from_config()
        if plex is None:
            print("\n  Plex is not configured. Add the server and token on the "
                  "settings page.\n")
            return 1

    try:
        items = plex.library()
    except PlexError as exc:
        print(f"\n  {exc}\n")
        return 1

    print(f"\n  {len(items)} item(s) from Plex\n")
    if args.summary:
        by_rating: dict[str, int] = {}
        unknown: list[str] = []
        for item in items:
            by_rating[item.rating] = by_rating.get(item.rating, 0) + 1
            if item.rating == "adult" and not item.content_rating:
                unknown.append(item.title)
        for rating, count in sorted(by_rating.items()):
            print(f"    {rating:<8} {count:>5}")
        if unknown:
            print(f"\n    {len(unknown)} item(s) have no Plex rating and are therefore "
                  f"treated as adult:")
            for title in unknown[:15]:
                print(f"      {title[:60]}")
        print()
        return 0

    for item in sorted(items, key=lambda i: (i.section, i.title.lower())):
        length = f"{item.minutes:>6.0f}m" if item.minutes else "      "
        print(f"    {item.section[:14]:<15} {item.title[:38]:<40} "
              f"{item.content_rating or '-':<8} {item.rating:<7} {length} "
              f"{','.join(item.genres[:3])}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
