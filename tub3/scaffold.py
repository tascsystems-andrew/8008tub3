"""Create the commercial folder structure, with the sorting made optional.

Programme ratings arrive free — TMDB, TVDB and Plex all carry a content rating, so a channel
can be built by rating and daypart without anyone touching a file. **Commercials have no such
database.** Nobody publishes content ratings for a 1993 Sunny D spot, so the folder a file
sits in *is* its metadata, and every one of those decisions is hand work.

So the design goal here is not a tidy taxonomy. It is to require as little sorting as
possible before the thing works:

- **`Unsorted/` is the landing zone and it counts as `Late`.** Dump everything there and you
  have a working adult-rated channel immediately, with no sorting at all. The safe default is
  the restrictive one: an unsorted spot can never reach a kids channel by accident.
- **Sorting is incremental.** Move a handful into `Kids/` whenever you feel like it; the kids
  channel gets better each time and nothing breaks in between.
- **Three tiers, not nine.** Rating times decade would be a matrix nobody fills in. Decade can
  live in an optional subfolder for anyone who wants it, and is ignored otherwise.
"""

from __future__ import annotations

import argparse
from pathlib import Path

FOLDERS: tuple[tuple[str, str, str], ...] = (
    (
        "Kids",
        "kids",
        "Safe for a seven-year-old with nobody in the room.\n"
        "Cereal, toys, theme parks, fast food, kids' TV promos.\n"
        "\n"
        "This is the only folder that reaches a kids channel, so when in doubt leave a spot\n"
        "out. A missing commercial costs nothing; a beer ad on the cartoon channel is the\n"
        "thing this whole structure exists to prevent.\n",
    ),
    (
        "Family",
        "family",
        "Fine in the living room at dinner time, but not aimed at children.\n"
        "Cars, banks, insurance, airlines, household products, movie trailers.\n"
        "\n"
        "Family channels draw from Kids AND Family, so anything here is additional to the\n"
        "kids pool rather than instead of it.\n",
    ),
    (
        "Late",
        "late",
        "Everything else. Beer, trucks, late-night 1-900 numbers, anything you would not\n"
        "want on before bedtime.\n"
        "\n"
        "Late channels draw from all three tiers.\n",
    ),
    (
        "Unsorted",
        "late",
        "The landing zone. Drop new downloads here and stop thinking about it.\n"
        "\n"
        "Treated as Late, which means it is used by adult-rated channels and can never leak\n"
        "onto a kids channel. Nothing here needs sorting for the system to work - sorting\n"
        "only improves the kids and family channels.\n"
        "\n"
        "A good rhythm: dump downloads here, and whenever you happen to be watching, move\n"
        "anything obviously kid-safe into ../Kids.\n",
    ),
)

ROOT_README = """BoobTube - commercials
======================

Drop commercials in here. The folder a spot sits in decides which channels can air it.

    Unsorted/   everything lands here first. Counts as Late. No sorting required.
    Kids/       safe for young children. The ONLY folder a kids channel draws from.
    Family/     general audience, not aimed at kids.
    Late/       anything else.

Ratings are cumulative, so a Family channel airs Kids + Family, and a Late channel airs
all of them. Sorting is optional and incremental - the system works with everything sitting
in Unsorted, and gets better as you move things out.

Long recordings are fine. A ninety-minute block of ads off a VHS tape gets cut into
individual spots automatically; already-cut clips are used as they are. You do not have to
say which you have.

Optional: a year or decade subfolder (Kids/1993/) is preserved for reference and ignored by
the scheduler.

Not needed: any particular filename, any sidecar file, any tagging.
"""


def scaffold(root: Path, *, dry_run: bool = False) -> dict[str, str]:
    """Create the structure. Idempotent, and never touches folders that already exist."""
    created: dict[str, str] = {}
    root.mkdir(parents=True, exist_ok=True)

    for name, rating, blurb in FOLDERS:
        folder = root / name
        existed = folder.exists()
        if not existed and not dry_run:
            folder.mkdir(parents=True, exist_ok=True)
        created[name] = "exists" if existed else ("would create" if dry_run else "created")

        readme = folder / "WHAT GOES HERE.txt"
        if not dry_run and not readme.exists():
            readme.write_text(f"{name}  (rating: {rating})\n{'=' * (len(name) + 20)}\n\n{blurb}")

    root_readme = root / "README.txt"
    if not dry_run and not root_readme.exists():
        root_readme.write_text(ROOT_README)

    return created


def survey(root: Path) -> dict[str, int]:
    """Count what is actually in each folder — the answer to 'is it worth sorting yet'."""
    from .bootstrap import VIDEO_SUFFIXES, classify_rating

    counts: dict[str, int] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if classify_rating(child.name) is None:
            continue
        counts[child.name] = sum(
            1 for p in child.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
        )
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.scaffold", description=__doc__)
    ap.add_argument("root", type=Path, help="your commercials folder")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    result = scaffold(args.root, dry_run=args.dry_run)
    print(f"\n  {args.root}\n")
    for name, state in result.items():
        rating = next(r for n, r, _ in FOLDERS if n == name)
        print(f"    {name:<12} {state:<14} (rating: {rating})")

    from .bootstrap import classify_rating

    counts = survey(args.root)
    if counts:
        print("\n  current contents:")
        for name, count in counts.items():
            print(f"    {name:<24} {count:>5} spots  ({classify_rating(name)})")
        total = sum(counts.values())
        kids = sum(c for n, c in counts.items() if classify_rating(n) == "kids")
        print(f"\n    {total} total, {kids} reachable by a kids channel")
        if total and not kids:
            print("    nothing is kid-safe yet — a kids channel would have no ads to play")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
