"""What Plex holds, as the box's catalogue.

The box has always decided *what is on* and asked Plex only *how to play it*. That split is
right and this does not change it. What changes is where the box learns what it *has*.

Until now the lineup named folders on the share, and every question about a folder — what is
it rated, what genre, how long are its episodes — was answered by matching path *tails*
against Plex: the last two or three components, lowercased, with ambiguous keys deleted
rather than guessed. `tub3/plex.py` explains why, and it is honest about it: Plex runs in a
container and reports `/Media/TV/Seinfeld` while the Pi sees
`/mnt/tub3/Media/mshare/TV/Seinfeld`, so "the prefixes will never agree".

They do agree, though, under one substitution — and the box already knows both halves. Plex
reports its section roots, and `settings.json` lists the same libraries as the Pi sees them.
Pair them by name, and every path Plex gives becomes an exact local path. Derived, then
*verified* against a file that must exist: a catalogue built on an unproven prefix would be
worse than no catalogue, because everything downstream would look right and resolve to
nothing.

So this module makes Plex the catalogue and the path authority both, and the share goes back
to being where the bytes are. The scheduler still needs directories of files — that is
FieldStation42's shape and it is vendored — but those pools become something derived from
what Plex says a channel contains, rather than the thing the lineup has to name by hand.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "runtime" / "library.json"

# Every section Plex will describe. Commercials and ambiance come along because they are
# `movie` sections like any other, and they are worth having: an editor that can see the ad
# pools can say how many spots a channel has, and one that cannot has to guess. A channel does
# not *draw* from them the way it draws from a series, which is a question for `resolve`, not
# for what gets catalogued.
PROGRAMME_TYPES = ("show", "movie")


def derive_prefix(plex_roots: list[str], local_dirs: list[str]) -> tuple[str, str] | None:
    """The one substitution that turns a Plex path into a path on this box.

    Paired by the library's own folder name — `/Media/TV` against
    `/mnt/tub3/Media/mshare/TV` — then reduced to the longest common head on each side. Two
    libraries agreeing on the same pair is what makes it a prefix rather than a coincidence,
    so a single pairing is not accepted.
    """
    pairs: list[tuple[str, str]] = []
    by_name = {Path(d).name.strip().lower(): d for d in local_dirs}
    for root in plex_roots:
        local = by_name.get(Path(root).name.strip().lower())
        if local:
            pairs.append((root.rstrip("/"), local.rstrip("/")))
    if len(pairs) < 2:
        return None

    def head(paths: list[str]) -> str:
        parts = [p.split("/") for p in paths]
        out: list[str] = []
        for column in zip(*parts):
            if len(set(column)) != 1:
                break
            out.append(column[0])
        return "/".join(out)

    plex_head = head([p for p, _ in pairs])
    local_head = head([l for _, l in pairs])
    if not plex_head or not local_head:
        return None
    return plex_head, local_head


def localise(path: str, prefix: tuple[str, str]) -> str:
    plex_head, local_head = prefix
    if path.startswith(plex_head):
        return local_head + path[len(plex_head):]
    return path


def verify(paths: list[str], want: int = 5) -> list[str]:
    """Prove the substitution against the filesystem before anything is written against it.

    Takes paths that have already been localised and asks whether they are there. Returns the
    ones that are not; an empty list is the only acceptable answer, because a catalogue built
    on an unproven prefix looks correct everywhere and resolves to nothing.
    """
    missing: list[str] = []
    checked = 0
    for path in paths:
        if checked >= want:
            break
        if not path:
            continue
        checked += 1
        if not os.path.exists(path):
            missing.append(path)
    if checked == 0:
        return ["no paths to check the prefix against"]
    return missing


def build(*, verbose: bool = False) -> dict:
    """Walk every programme section and write the catalogue."""
    from .plex import from_config

    client = from_config()
    if client is None:
        raise RuntimeError("Plex is not configured on this box")

    sections = [s for s in client.sections() if s["type"] in PROGRAMME_TYPES]
    # The library roots are on the sections listing itself, one <Location> per Directory —
    # not on the per-section call, which returns the section's contents.
    listing = client._get("/library/sections")                      # noqa: SLF001
    wanted = {s["key"] for s in sections}
    roots: list[str] = []
    for node in listing.findall(".//Directory"):
        if node.get("key") in wanted:
            roots += [l.get("path") for l in node.findall(".//Location") if l.get("path")]

    from .web import load_settings  # noqa: PLC0415
    local_dirs = list(load_settings().get("programs_dirs") or [])

    prefix = derive_prefix(roots, local_dirs)
    if prefix is None:
        raise RuntimeError(
            "could not pair Plex's library roots with this box's programs_dirs — "
            f"plex says {roots}, the box says {local_dirs}"
        )

    titles: list[dict] = []
    for section in sections:
        for item in client.items(section["key"], section["title"] or ""):
            # The section listing truncates each title's genre list, so the full one costs a
            # request per title. Sixteen seconds for the whole library, once, against a
            # channel definition that would otherwise be built on two genres out of five.
            genres = _genres(client, item.rating_key) or item.genres
            local = [localise(p, prefix) for p in item.paths]
            titles.append({
                "title": item.title,
                "kind": item.kind,
                "section": section["title"],
                "section_key": section["key"],
                "rating": item.rating,
                "content_rating": item.content_rating,
                "year": item.year,
                "genres": genres,
                "episodes": item.episodes,
                "seconds": item.seconds,
                "rating_key": item.rating_key,
                # The folder a lineup would name: the common head of everything under it.
                "path": _folder_of(local),
                "files": len(local),
            })
        if verbose:
            print(f"  {section['title']}: {len([t for t in titles if t['section'] == section['title']])}")

    missing = verify([t["path"] for t in titles if t["path"]])
    if missing:
        raise RuntimeError(
            "the derived Plex-to-box path prefix does not resolve: "
            + ", ".join(missing[:3])
        )

    cache = {
        "prefix": {"plex": prefix[0], "box": prefix[1]},
        "sections": [{"key": s["key"], "title": s["title"], "type": s["type"]}
                     for s in sections],
        "titles": sorted(titles, key=lambda t: (t["section"], t["title"].lower())),
    }
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(CACHE)
    return cache


def _genres(client, rating_key: str) -> list[str]:
    if not rating_key:
        return []
    try:
        root = client._get(f"/library/metadata/{rating_key}")        # noqa: SLF001
    except Exception:                                                # noqa: BLE001
        return []
    node = root.find(".//Directory") if root.find(".//Directory") is not None else root.find(".//Video")
    if node is None:
        return []
    return [g.get("tag") for g in node.findall("Genre") if g.get("tag")]


def _folder_of(paths: list[str]) -> str:
    """The directory a lineup would name for this title.

    A series is its folder. A film is very often a bare file in the library root, and naming
    the root would sweep in every other film — so a film with no folder of its own keeps its
    file path, which `_iter_videos` already accepts.
    """
    if not paths:
        return ""
    if len(paths) == 1:
        parent = str(Path(paths[0]).parent)
        return paths[0] if Path(parent).name.lower() in ("movies", "kids movies") else parent
    parts = [p.split("/") for p in paths]
    out: list[str] = []
    for column in zip(*parts):
        if len(set(column)) != 1:
            break
        out.append(column[0])
    return "/".join(out)


def load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"prefix": {}, "sections": [], "titles": []}


def resolve(**clauses) -> list[dict]:
    """Titles matching every clause. `genre` and `section` may be given more than once."""
    titles = load().get("titles", [])
    out = []
    for t in titles:
        keep = True
        for field, want in clauses.items():
            if want is None:
                continue
            values = want if isinstance(want, (list, tuple, set)) else [want]
            if field == "genre":
                if not set(g.lower() for g in t["genres"]) & set(str(v).lower() for v in values):
                    keep = False
            elif field == "section":
                if str(t["section"]).lower() not in [str(v).lower() for v in values] \
                        and str(t["section_key"]) not in [str(v) for v in values]:
                    keep = False
            elif str(t.get(field, "")).lower() not in [str(v).lower() for v in values]:
                keep = False
            if not keep:
                break
        if keep:
            out.append(t)
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="tub3.library", description=__doc__)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--resolve", nargs="*", metavar="field=value")
    args = ap.parse_args(argv)

    if args.build:
        cache = build(verbose=True)
        print(f"\n  {len(cache['titles'])} titles across {len(cache['sections'])} sections")
        print(f"  prefix: {cache['prefix']['plex']}  ->  {cache['prefix']['box']}\n")
        return 0
    if args.resolve is not None:
        clauses: dict = {}
        for pair in args.resolve:
            field, _, value = pair.partition("=")
            clauses.setdefault(field, []).append(value)
        hits = resolve(**clauses)
        print(f"  {len(hits)} title(s)")
        for t in hits[:40]:
            flag = "  ⚠ unrated" if not t["content_rating"] else ""
            print(f"    {t['title'][:44]:<44} {str(t['content_rating'] or '—'):<10} "
                  f"{','.join(t['genres'][:3]):<28}{flag}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
