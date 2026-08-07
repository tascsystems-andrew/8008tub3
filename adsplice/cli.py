"""adsplice command line.

    adsplice ingest <folder> --out <commercials-dir>
    adsplice selftest

`selftest` matters more than it looks: it generates its own fixture and scores the
detector, so a freshly flashed box can prove its own ingest path works before the user has
supplied a single file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .detect import PROFILES
from .ffmpeg import FFmpegMissing, require_tools
from .pipeline import DEFAULT_CONFIDENCE_FLOOR, ingest


def _cmd_ingest(args: argparse.Namespace) -> int:
    report = ingest(
        args.source,
        args.out,
        profile_name=args.profile,
        confidence_floor=args.confidence,
        dedupe_threshold=args.dedupe,
        dry_run=args.dry_run,
    )

    print(f"\n  profile            {report.profile}")
    print(f"  files seen         {report.inputs}")
    print(f"    already spots    {report.already_spots}")
    print(f"    blocks cut       {report.blocks_segmented}")
    print(f"  segments found     {report.segments_found}")
    print(f"    fragments merged {report.fragments_merged}")
    print(f"  accepted           {report.accepted}")
    print(f"    dropped: short/long  {report.rejected_length}")
    print(f"    dropped: low confidence {report.rejected_confidence}")
    if not args.dry_run:
        print(f"  duplicates dropped {report.duplicates_dropped}")
        print(f"  written            {report.written}")
    if report.errors:
        print(f"\n  {len(report.errors)} problem(s):")
        for err in report.errors[:10]:
            print(f"    - {err}")
    print()
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests.make_fixture import build  # noqa: PLC0415
    from tests.score import score  # noqa: PLC0415

    work = args.workdir
    print(f"building fixtures in {work} …")
    failures = 0
    for variant, profile in (("clean", "digital"), ("vhs", "vhs")):
        build(work, variant)
        result = score(work / f"truth_{variant}.json", profile)
        ok = result["recall"] >= 0.9 and result["precision"] >= 0.9
        failures += 0 if ok else 1
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {variant:<6} profile={profile:<8} "
              f"P={result['precision']:.2f} R={result['recall']:.2f} "
              f"drift={result['mean_drift_s']}s")
    print()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adsplice", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_in = sub.add_parser("ingest", help="turn a folder of anything into cut, deduped spots")
    p_in.add_argument("source", type=Path)
    p_in.add_argument("--out", type=Path, required=True)
    p_in.add_argument("--profile", choices=list(PROFILES), default="vhs",
                      help="vhs (default) loosens thresholds and crops head-switching noise")
    p_in.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE_FLOOR)
    p_in.add_argument("--dedupe", type=float, default=0.90)
    p_in.add_argument("--dry-run", action="store_true")
    p_in.set_defaults(func=_cmd_ingest)

    p_st = sub.add_parser("selftest", help="prove the detector works, with no user content")
    p_st.add_argument("--workdir", type=Path, default=Path("/tmp/adsplice-selftest"))
    p_st.set_defaults(func=_cmd_selftest)

    args = parser.parse_args(argv)
    try:
        require_tools()
    except FFmpegMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
