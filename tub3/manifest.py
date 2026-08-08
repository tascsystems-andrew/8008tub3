"""What is actually on the drive, in a form something else can reason about.

Building a dial is a judgement problem, not a computation. Which four sitcoms belong in a
dinner strip, whether a channel of nature documentaries wants an hour or a half-hour block,
what to call it — none of that follows from the file names by rule. So the box enumerates,
and something with taste decides.

This is the enumeration half. Three things make it usable:

**Series, not files.** A raw listing of three thousand paths is both too long to reason over
and the wrong shape — the unit of scheduling is a series, and the fact that one has 180
episodes and another has 6 is the single most important input to a lineup. Episodes are
counted, not listed.

**Durations sampled, never measured.** Probing every file across a network share takes
hours. Three per series, multiplied out, is accurate to a few percent and takes seconds.
Anything derived from it is marked as an estimate.

**A rating that defaults to restrictive.** Every series carries a rating derived from where
it sits on disk, and anything not positively identified as kid-safe is `adult`. That is the
whole safety model in one sentence, and it is deliberately the boring half: the interesting
half decides what makes a good channel, this half decides what is allowed to be considered.

The output is JSON — for a model, a script, or a person reading it. `--tree` prints the same
thing for human eyes.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .bootstrap import VIDEO_SUFFIXES

# Folder names that positively mark content as safe for children. Everything else is adult
# until a human says otherwise — the default has to be the safe one, because the cost of
# the two mistakes is not symmetric. A cartoon missing from a kids channel is a
# disappointment; the reverse is the thing this project must never do.
KIDS_MARKERS = ("kids", "kid", "children", "child", "family", "cartoon", "cartoons",
                "preschool", "juvenile", "toddler", "littles", "saturday morning")

# The subset safe to match against a *folder's own name* when that folder is the thing being
# scheduled. "family" is deliberately absent, and that omission is the whole point: it makes
# "Kids Movies" kid-safe while leaving "Family Guy" adult. A title is not a rating, and the
# words most likely to appear in an adult show's title are exactly the reassuring ones.
LEAF_MARKERS = ("kids", "children", "preschool", "toddler", "cartoon", "cartoons",
                "juvenile", "littles")

# Names that mark content as definitely NOT for a kids channel even if it sits in a folder
# that otherwise looks safe.
ADULT_MARKERS = ("unrated", "uncut", "extended", "nc-17", "nc17")

YEAR_BRACKETED = re.compile(r"[(\[](19|20)\d{2}[)\]]")
YEAR_TOKEN = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")

# Release years only. The 2049 in "Blade Runner 2049" is part of a title, and a ceiling is
# what tells the two apart; next year rather than this one leaves room for a title named
# ahead of its release.
_YEAR_CEILING = date.today().year + 1


def year_of(name: str) -> int | None:
    """The release year a folder or file name is claiming, or None.

    This library writes years three ways — `Ace Ventura (1994)`, `Goosebumps 2023`, and
    scene style `Airplane.1980.2160p` — and reading only the bracketed form meant the year
    was very often not read at all: **no** folder in the TV library brackets it. That is the
    mechanism behind "a scheduler cannot tell them apart from the folder name". The 2023
    `Goosebumps` does say 2023 right there in the folder name; nothing was looking.

    Two guards keep a number in a title from being read as a year. A bracketed year always
    wins, so `Blade Runner 2049 (2017)` is 2017. Failing that, a bare token counts only if it
    is no later than next year and is not the entire name — which is what separates `Blade
    Runner 2049` and `1917` from a genuine release year.
    """
    bracketed = YEAR_BRACKETED.search(name)
    if bracketed:
        return int(bracketed.group(0)[1:-1])

    stripped = name.strip()
    for match in YEAR_TOKEN.finditer(stripped):
        if match.group(0) == stripped:
            continue  # the title *is* a year — "1917"
        value = int(match.group(0))
        if value <= _YEAR_CEILING:
            return value
    return None


@dataclass
class Series:
    name: str
    path: str
    top_folder: str
    episodes: int
    minutes_median: float | None
    sampled: int
    rating: str                 # "kids" | "adult"
    rating_reason: str
    kind: str                   # "series" | "film" | "short"
    year: int | None = None

    @property
    def hours(self) -> float:
        return (self.episodes * (self.minutes_median or 0)) / 60.0


@dataclass
class Manifest:
    roots: list[str]
    series: list[Series] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        payload = {
            "roots": self.roots,
            "counts": {
                "series": len(self.series),
                "episodes": sum(s.episodes for s in self.series),
                "kids_rated": sum(1 for s in self.series if s.rating == "kids"),
            },
            "rating_rule": (
                "Content is 'kids' only when its folder positively says so. Everything "
                "else is 'adult'. A lineup that places an 'adult' source on a channel or "
                "daypart rated for children is refused by tub3.lineup, not warned about."
            ),
            "series": [asdict(s) for s in self.series],
            "notes": self.notes,
        }
        return json.dumps(payload, indent=2)


def classify(top_folder: str, name: str, path: Path) -> tuple[str, str]:
    """Rating for one series or folder, and the reason in plain language.

    The reason is carried through because a refused lineup has to explain itself in terms
    someone can act on — "Kids TV is marked for children, TV is not" is actionable,
    "rating: adult" is not.

    Two different tests, and the asymmetry is deliberate:

    - **Ancestor folders** are matched against the full marker list. Someone who files a
      show under `Kids TV` has made a statement about it.
    - **The item's own name** is matched only against `LEAF_MARKERS`, which excludes
      "family". A title is not a rating. `Kids Movies` should be kid-safe, and `Family Guy`
      very much should not, and only leaving "family" out of the leaf test gets both right.

    Anything not positively identified is adult. The two errors are not symmetric: a cartoon
    wrongly withheld is a disappointment, and the reverse is the one outcome this project
    cannot have.
    """
    lowered = name.lower()
    for marker in ADULT_MARKERS:
        if marker in lowered:
            return "adult", f"name contains {marker!r}"

    ancestors = " ".join(part.lower() for part in path.parts[:-1])
    if top_folder:
        ancestors += " " + top_folder.lower()
    for marker in KIDS_MARKERS:
        if marker in ancestors:
            return "kids", f"it sits under a folder marked for children ({marker!r})"

    for marker in LEAF_MARKERS:
        if marker in lowered:
            return "kids", f"the folder {name!r} is itself marked for children"

    return "adult", f"nothing in the path {str(path)!r} marks it as for children"


def _episodes(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
            and not p.name.startswith(".")]


def _median_minutes(files: list[Path], samples: int) -> tuple[float | None, int]:
    from mediakit.ffmpeg import probe

    if not files:
        return None, 0
    step = max(1, len(files) // (samples + 1))
    picks = files[step::step][:samples] or files[:1]

    durations = []
    for path in picks:
        try:
            info = probe(path)
        except Exception:  # noqa: BLE001 - one unreadable file must not sink the estimate
            continue
        if info.duration > 0:
            durations.append(info.duration / 60.0)
    if not durations:
        return None, 0
    return round(statistics.median(durations), 1), len(durations)


def _kind(minutes: float | None, episodes: int) -> str:
    if minutes is None:
        return "series" if episodes > 1 else "film"
    if minutes >= 65:
        return "film"
    if minutes < 8:
        return "short"
    return "series"


def scan(roots: list[Path], *, samples: int = 3, probe_durations: bool = True) -> Manifest:
    manifest = Manifest(roots=[str(r) for r in roots])

    for root in roots:
        if not root.exists():
            manifest.notes.append(f"missing: {root}")
            continue
        top = root.name

        children = sorted((c for c in root.iterdir()
                           if c.is_dir() and not c.name.startswith(".")),
                          key=lambda p: p.name.lower())

        # A folder of loose files (a films folder, typically) rather than one of series.
        loose = [p for p in root.iterdir()
                 if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
                 and not p.name.startswith(".")]

        for child in children:
            files = _episodes(child)
            if not files:
                continue
            minutes, sampled = (_median_minutes(files, samples) if probe_durations
                                else (None, 0))
            rating, why = classify(top, child.name, child)
            manifest.series.append(Series(
                name=child.name, path=str(child), top_folder=top,
                episodes=len(files), minutes_median=minutes, sampled=sampled,
                rating=rating, rating_reason=why,
                kind=_kind(minutes, len(files)),
                year=year_of(child.name),
            ))

        for film in sorted(loose, key=lambda p: p.name.lower())[:400]:
            rating, why = classify(top, film.stem, film)
            manifest.series.append(Series(
                name=film.stem, path=str(film), top_folder=top,
                episodes=1, minutes_median=None, sampled=0,
                rating=rating, rating_reason=why, kind="film",
                year=year_of(film.name),
            ))

    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.manifest", description=__doc__)
    ap.add_argument("roots", nargs="+", type=Path, help="library folders to enumerate")
    ap.add_argument("--out", type=Path, help="write JSON here instead of stdout")
    ap.add_argument("--tree", action="store_true", help="human-readable instead of JSON")
    ap.add_argument("--samples", type=int, default=3,
                    help="files probed per series for the duration estimate")
    ap.add_argument("--no-durations", action="store_true",
                    help="skip probing entirely — instant, but no block-length guidance")
    args = ap.parse_args(argv)

    manifest = scan(args.roots, samples=args.samples,
                    probe_durations=not args.no_durations)

    if args.tree:
        by_folder: dict[str, list[Series]] = {}
        for series in manifest.series:
            by_folder.setdefault(series.top_folder, []).append(series)
        print()
        for folder, items in by_folder.items():
            kids = sum(1 for s in items if s.rating == "kids")
            mark = "kid-safe" if kids == len(items) else (
                "adult" if kids == 0 else f"MIXED ({kids}/{len(items)} kid-safe)")
            print(f"  {folder}  —  {len(items)} entries, {mark}")
            for series in items[:40]:
                length = f"{series.minutes_median:>5.1f}m" if series.minutes_median else "    ?"
                print(f"      {series.name[:46]:<48} {series.episodes:>4} eps {length}"
                      f"  {series.rating}")
            if len(items) > 40:
                print(f"      ... and {len(items) - 40} more")
            print()
        return 0

    text = manifest.to_json()
    if args.out:
        args.out.write_text(text)
        print(f"\n  {len(manifest.series)} entries written to {args.out}"
              f"  ({len(text) / 1024:.0f} KB)\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
