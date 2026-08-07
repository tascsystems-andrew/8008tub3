"""The ingest pipeline: a folder of anything -> a folder of tagged, deduped spots.

Design rules, from the project charter:

- **Never ask the user what they have.** A folder of 400 thirty-second files and a folder
  of twelve ninety-minute tapes are the same command. Classification is inferred from
  duration, and mixed folders are handled item by item.
- **Never produce a to-do list.** Segments below the confidence floor are discarded, not
  queued. Ads are fungible and there are thousands; losing 15% to caution costs nothing,
  while an hour of human review per hour of footage costs the project.
- **Every decision is recorded.** The run report is how you debug a bad-looking channel
  without re-running anything.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from . import phash
from .detect import Boundary, Profile, PROFILES, duration_prior, find_boundaries
from .ffmpeg import MediaInfo, probe, run

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".ts", ".webm", ".wmv", ".vob"}

# Classification thresholds, in seconds.
SPOT_MAX = 75.0        # at or under this, treat the file as an already-cut spot
BLOCK_MIN = 300.0      # over this, definitely a block that needs segmenting
                       # between the two: segment, then sanity-check the result

# Acceptance limits for a cut segment.
MIN_SPOT = 8.0
MAX_SPOT = 125.0
DEFAULT_CONFIDENCE_FLOOR = 0.55


@dataclass
class Spot:
    source: str
    start: float
    end: float
    duration: float
    confidence: float
    canonical: float | None
    bucket: int
    agreement_in: float
    agreement_out: float
    notes: list[str] = field(default_factory=list)
    output: str | None = None
    fingerprint: list[int] = field(default_factory=list)
    duplicate_of: str | None = None


@dataclass
class Report:
    started: str
    profile: str
    inputs: int = 0
    already_spots: int = 0
    blocks_segmented: int = 0
    segments_found: int = 0
    fragments_merged: int = 0
    accepted: int = 0
    rejected_confidence: int = 0
    rejected_length: int = 0
    duplicates_dropped: int = 0
    written: int = 0
    errors: list[str] = field(default_factory=list)
    spots: list[Spot] = field(default_factory=list)

    def to_json(self) -> str:
        payload = asdict(self)
        payload["spots"] = [asdict(s) if not isinstance(s, dict) else s for s in self.spots]
        return json.dumps(payload, indent=2)


def bucket_for(duration: float) -> int:
    """Nearest broadcast duration class, used for pod assembly downstream."""
    for edge, label in ((12.5, 10), (22.5, 15), (45.0, 30), (90.0, 60), (1e9, 120)):
        if duration < edge:
            return label
    return 120


def classify(info: MediaInfo) -> str:
    if info.duration <= 0:
        return "unreadable"
    if info.duration <= SPOT_MAX:
        return "spot"
    if info.duration >= BLOCK_MIN:
        return "block"
    return "ambiguous"


def _segments_from_boundaries(
    info: MediaInfo,
    boundaries: list[Boundary],
) -> list[Spot]:
    """Turn cut points into segments, scoring each by signal agreement and duration prior."""
    edges: list[tuple[float, Boundary | None]] = [(0.0, None)]
    edges += [(b.time, b) for b in boundaries]
    edges.append((info.duration, None))

    spots: list[Spot] = []
    for i in range(len(edges) - 1):
        start, b_in = edges[i]
        end, b_out = edges[i + 1]
        duration = end - start
        if duration <= 0:
            continue

        prior, canonical = duration_prior(duration)
        agree_in = b_in.agreement if b_in else 0.5   # file start is a real edge, not a guess
        agree_out = b_out.agreement if b_out else 0.5

        # Confidence blends how well the bracketing cuts were evidenced with how closely
        # the resulting length matches a real spot. Either alone is unreliable; together
        # they are the whole trick.
        confidence = 0.55 * ((agree_in + agree_out) / 2.0) + 0.45 * prior

        notes: list[str] = []
        if b_in:
            notes += [f"in: {n}" for n in b_in.notes]
        if b_out:
            notes += [f"out: {n}" for n in b_out.notes]

        spots.append(Spot(
            source=str(info.path),
            start=start,
            end=end,
            duration=duration,
            confidence=round(confidence, 4),
            canonical=canonical,
            bucket=bucket_for(duration),
            agreement_in=round(agree_in, 3),
            agreement_out=round(agree_out, 3),
            notes=notes,
        ))
    return spots


def _merge_oversegmented(
    spots: list[Spot],
    *,
    max_span: int = 4,
    strong_boundary: float = 0.85,
    margin: float = 0.15,
) -> tuple[list[Spot], int]:
    """Reassemble fragments that a dark scene inside a commercial split apart.

    A spot that fades to black mid-way, or simply has a dark shot, trips blackdetect and
    gets cut in two. Neither fragment lands near a canonical length, so both then fail the
    confidence floor and the whole commercial is lost.

    The duration prior fixes this in the other direction: if several consecutive fragments
    sum to something very close to 15/30/60 seconds, and the boundaries *between* them were
    weak, the simplest explanation is one spot that was cut up — so put it back together.
    Merging is refused across a strong boundary (black and silence in firm agreement),
    because that is a real break between two commercials.

    This is the piece no existing detector has: everything else treats duration as a
    post-hoc reject rule, never as evidence for recombination.
    """
    if len(spots) < 2:
        return spots, 0

    merged: list[Spot] = []
    i = 0
    merge_count = 0

    while i < len(spots):
        best_span = 1
        best_gain = 0.0
        solo_prior, _ = duration_prior(spots[i].duration)

        for span in range(2, min(max_span, len(spots) - i) + 1):
            group = spots[i:i + span]
            # Never merge across a boundary that both signals confirmed.
            interior = [g.agreement_out for g in group[:-1]]
            if max(interior) >= strong_boundary:
                break

            total = sum(g.duration for g in group)
            if total > MAX_SPOT:
                break

            combined_prior, _ = duration_prior(total)
            # Compare against the best any single fragment manages on its own.
            solo_best = max(duration_prior(g.duration)[0] for g in group)
            gain = combined_prior - max(solo_best, solo_prior)

            if gain > margin and gain > best_gain:
                best_gain, best_span = gain, span

        if best_span == 1:
            merged.append(spots[i])
            i += 1
            continue

        group = spots[i:i + best_span]
        total = group[-1].end - group[0].start
        prior, canonical = duration_prior(total)
        head, tail = group[0], group[-1]
        combined = Spot(
            source=head.source,
            start=head.start,
            end=tail.end,
            duration=total,
            confidence=round(0.55 * ((head.agreement_in + tail.agreement_out) / 2.0) + 0.45 * prior, 4),
            canonical=canonical,
            bucket=bucket_for(total),
            agreement_in=head.agreement_in,
            agreement_out=tail.agreement_out,
            notes=[f"merged {best_span} fragments split by an interior dark scene"],
        )
        merged.append(combined)
        merge_count += best_span - 1
        i += best_span

    return merged, merge_count


def _cut(src: Path, spot: Spot, dest: Path) -> tuple[bool, str | None]:
    """Extract a segment. Re-encode only when a stream copy would land off-keyframe.

    The temp name keeps `dest`'s real suffix — ffmpeg picks its muxer from the extension,
    so a plain `.part` suffix makes every write fail with "no suitable output format".
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")

    # Accurate seek (input-side -ss with -accurate_seek) plus stream copy is fast and
    # usually frame-accurate enough; if the copy produces nothing usable we re-encode.
    copy_cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{spot.start:.3f}", "-to", f"{spot.end:.3f}",
        "-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero",
        str(tmp),
    ]
    last_error: str | None = None
    try:
        run(copy_cmd)
    except RuntimeError as exc:
        last_error = str(exc)
        tmp.unlink(missing_ok=True)

    if not tmp.exists() or tmp.stat().st_size < 4096:
        tmp.unlink(missing_ok=True)
        encode_cmd = [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{spot.start:.3f}", "-to", f"{spot.end:.3f}",
            "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            str(tmp),
        ]
        try:
            run(encode_cmd)
        except RuntimeError as exc:
            last_error = str(exc)
            tmp.unlink(missing_ok=True)
            return False, last_error

    if not tmp.exists() or tmp.stat().st_size < 4096:
        tmp.unlink(missing_ok=True)
        return False, last_error or "output was empty or too small"

    tmp.replace(dest)  # atomic, so an interrupted run never leaves a half file in the catalog
    return True, None


def ingest(
    source_dir: Path,
    out_dir: Path,
    *,
    profile_name: str = "vhs",
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    dedupe_threshold: float = 0.90,
    dry_run: bool = False,
) -> Report:
    profile: Profile = PROFILES[profile_name]
    report = Report(
        started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        profile=profile_name,
    )

    files = sorted(
        p for p in source_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
    )
    report.inputs = len(files)

    candidates: list[Spot] = []

    for path in files:
        try:
            info = probe(path)
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop the run
            report.errors.append(f"{path.name}: probe failed: {exc}")
            continue

        kind = classify(info)
        if kind == "unreadable":
            report.errors.append(f"{path.name}: no readable duration")
            continue

        if kind == "spot":
            # Already a cut spot. Take it whole; no detection, no risk of chopping it up.
            report.already_spots += 1
            prior, canonical = duration_prior(info.duration)
            candidates.append(Spot(
                source=str(path),
                start=0.0,
                end=info.duration,
                duration=info.duration,
                confidence=round(0.7 + 0.3 * prior, 4),
                canonical=canonical,
                bucket=bucket_for(info.duration),
                agreement_in=1.0,
                agreement_out=1.0,
                notes=["pre-cut spot, passed through"],
            ))
            continue

        report.blocks_segmented += 1
        try:
            boundaries = find_boundaries(info, profile)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{path.name}: detection failed: {exc}")
            continue

        segments = _segments_from_boundaries(info, boundaries)
        report.segments_found += len(segments)

        segments, merged = _merge_oversegmented(segments)
        report.fragments_merged += merged

        if kind == "ambiguous" and len(segments) <= 1:
            # Between 75s and 5min with no internal boundaries — it was one long spot or a
            # short bumper reel. Keep it whole rather than discarding it.
            prior, canonical = duration_prior(info.duration)
            candidates.append(Spot(
                source=str(path),
                start=0.0,
                end=info.duration,
                duration=info.duration,
                confidence=round(0.6 + 0.3 * prior, 4),
                canonical=canonical,
                bucket=bucket_for(info.duration),
                agreement_in=1.0,
                agreement_out=1.0,
                notes=["ambiguous length, no boundaries found, kept whole"],
            ))
            continue

        candidates.extend(segments)

    # --- filter: length, then confidence. Discarded, never queued. ---
    survivors: list[Spot] = []
    for spot in candidates:
        if not (MIN_SPOT <= spot.duration <= MAX_SPOT):
            report.rejected_length += 1
            continue
        if spot.confidence < confidence_floor:
            report.rejected_confidence += 1
            continue
        survivors.append(spot)
    report.accepted = len(survivors)

    if dry_run:
        report.spots = survivors
        return report

    # --- cut ---
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Spot] = []
    for index, spot in enumerate(survivors):
        stem = Path(spot.source).stem[:40].replace(" ", "_")
        dest = out_dir / f"{stem}_{index:04d}_{spot.bucket}s.mkv"
        ok, err = _cut(Path(spot.source), spot, dest)
        if ok:
            spot.output = str(dest)
            written.append(spot)
        else:
            report.errors.append(
                f"{Path(spot.source).name} @{spot.start:.1f}s: cut failed — {err or 'unknown'}"
            )

    # --- dedupe, after cutting so we hash exactly what will air ---
    for spot in written:
        spot.fingerprint = phash.fingerprint(Path(spot.output), spot.duration)

    result = phash.dedupe(
        [s.fingerprint for s in written],
        [s.duration for s in written],
        threshold=dedupe_threshold,
    )
    for dup_index, original_index in result.dropped.items():
        dup = written[dup_index]
        dup.duplicate_of = written[original_index].output
        Path(dup.output).unlink(missing_ok=True)
        dup.output = None
    report.duplicates_dropped = len(result.dropped)
    report.written = len(result.kept)
    report.spots = written

    (out_dir / "adsplice-report.json").write_text(report.to_json())
    return report
