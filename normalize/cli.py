"""normalize command line.

    normalize run <source-dir> --out <normalized-dir> [--target pi5]
    normalize inspect <file>

Most files leave with their video stream copied untouched — only the audio is rebuilt, to
match loudness. Re-encoding happens only where the device genuinely cannot play the source,
or where the picture is interlaced or has bars baked in.

Safe to interrupt: progress is per file, and a killed run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mediakit.ffmpeg import FFmpegMissing, probe, require_tools
from .analyze import detect_crop, detect_interlacing, measure_loudness
from .encode import is_current, make_plan, normalize_one
from .profile import DEFAULT, TARGETS

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".ts", ".webm", ".wmv", ".vob"}


def _cmd_inspect(args: argparse.Namespace) -> int:
    info = probe(args.file)
    target = TARGETS[args.target]
    interlace = detect_interlacing(info)
    crop = detect_crop(info)
    loudness = measure_loudness(info, target.target_lufs, target.true_peak, target.loudness_range)

    print(f"\n  {info.path.name}")
    print(f"  {info.width}x{info.height} @ {info.fps:.3f}fps  {info.video_codec}  {info.duration:.1f}s")
    print(f"  interlacing   {interlace.mode} (confidence {interlace.confidence:.2f}; "
          f"TFF={interlace.tff} BFF={interlace.bff} prog={interlace.progressive})")
    print(f"  active area   {crop.filter_string}{'' if crop.trims else '  (no bars)'}")
    plan = make_plan(info, target)
    print(f"  plan          {'RE-ENCODE' if plan.reencode_video else 'copy video'} — {plan.summary}")
    if loudness:
        delta = target.target_lufs - loudness.input_i
        print(f"  loudness      {loudness.input_i:.1f} LUFS -> {target.target_lufs:.1f} "
              f"({delta:+.1f} dB), true peak {loudness.input_tp:.1f} dBTP")
    else:
        print("  loudness      not measurable")
    print()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    target = TARGETS[args.target]
    files = sorted(
        p for p in args.source.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
    )
    if not files:
        print(f"no media found under {args.source}", file=sys.stderr)
        return 1

    print(f"\n  {len(files)} file(s), target {target.name} "
          f"(max {target.max_frame[0]}x{target.max_frame[1]}, "
          f"{target.target_lufs:.0f} LUFS)\n")

    encoded = skipped = failed = 0
    results = []

    for index, source in enumerate(files, 1):
        rel = source.relative_to(args.source)
        dest = (args.out / rel).with_suffix(".mkv")

        if not args.force and is_current(source, dest, target):
            skipped += 1
            print(f"  [{index}/{len(files)}] {rel}  — current, skipped")
            continue

        result = normalize_one(
            source, dest, target,
            inject_chapters=not args.no_chapters,
            force=args.force,
        )
        results.append(result)

        if result.output and not result.skipped:
            encoded += 1
            bits = [f"{result.encode_seconds:.0f}s"]
            if result.interlacing and result.interlacing != "progressive":
                bits.append(f"deinterlaced {result.interlacing}")
            bits.append(result.video_action)
            if result.cropped:
                bits.append("cropped")
            if result.scaled_to:
                bits.append(f"scaled to {result.scaled_to}")
            if result.loudness_in is not None:
                bits.append(f"{result.loudness_in:.1f}->{target.target_lufs:.0f} LUFS")
            if result.chapters:
                bits.append(f"{len(result.chapters)} act breaks")
            print(f"  [{index}/{len(files)}] {rel}  — {', '.join(bits)}")
        elif result.skipped:
            skipped += 1
        else:
            failed += 1
            print(f"  [{index}/{len(files)}] {rel}  — FAILED: {result.reason}")

    print(f"\n  encoded {encoded}   skipped {skipped}   failed {failed}\n")

    if args.report:
        args.report.write_text(json.dumps([r.__dict__ for r in results], indent=2))
        print(f"  report written to {args.report}\n")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="normalize", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="condition a library for a playback device")
    p_run.add_argument("source", type=Path)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--target", choices=list(TARGETS), default=DEFAULT.name)
    p_run.add_argument("--no-chapters", action="store_true", help="skip act-break detection")
    p_run.add_argument("--force", action="store_true", help="re-encode even if current")
    p_run.add_argument("--report", type=Path)
    p_run.set_defaults(func=_cmd_run)

    p_ins = sub.add_parser("inspect", help="show what a file would need, without encoding")
    p_ins.add_argument("file", type=Path)
    p_ins.add_argument("--target", choices=list(TARGETS), default=DEFAULT.name)
    p_ins.set_defaults(func=_cmd_inspect)

    args = parser.parse_args(argv)
    try:
        require_tools()
    except FFmpegMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
