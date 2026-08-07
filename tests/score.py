"""Score the detector against a fixture's ground truth.

This is the number that decides whether the project's differentiator is real. The research
set the bar: existing black-frame detection benchmarks around 90% recall on *clean digital*
broadcast, and nobody has published anything for VHS. If the vhs variant lands near the
clean variant here, the analog pre-crop and threshold work is doing its job.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adsplice.detect import PROFILES, find_boundaries  # noqa: E402
from adsplice.ffmpeg import probe  # noqa: E402


def score(truth_path: Path, profile_name: str, tolerance: float = 1.0) -> dict:
    truth = json.loads(truth_path.read_text())
    block = Path(truth["block"])
    info = probe(block)
    boundaries = find_boundaries(info, PROFILES[profile_name])
    found = [b.time for b in boundaries]

    # Ground truth is one cut point per *gap*, not two per spot. Consecutive spots are
    # separated by a single black interstitial, so the only physically detectable boundary
    # is its midpoint — scoring against both the outgoing spot's end and the incoming
    # spot's start caps recall at 50% no matter how good detection is.
    spots = sorted(truth["spots"], key=lambda s: s["start"])
    expected = [
        round((a["end"] + b["start"]) / 2.0, 3)
        for a, b in zip(spots, spots[1:])
    ]

    matched: list[tuple[float, float]] = []
    unmatched_expected: list[float] = []
    used: set[int] = set()

    for want in expected:
        best_i, best_d = None, tolerance + 1
        for i, got in enumerate(found):
            if i in used:
                continue
            d = abs(got - want)
            if d < best_d:
                best_i, best_d = i, d
        if best_i is not None and best_d <= tolerance:
            used.add(best_i)
            matched.append((want, found[best_i]))
        else:
            unmatched_expected.append(want)

    # Detections in the reel's own lead-in and tail are correct, not spurious — a tape
    # starts and ends on black. Downstream they produce head/tail fragments that the
    # length filter discards, so they cost nothing and shouldn't count against precision.
    body_start, body_end = spots[0]["start"], spots[-1]["end"]
    false_positives = [
        t for i, t in enumerate(found)
        if i not in used and body_start + 0.1 < t < body_end - 0.1
    ]

    tp, fn, fp = len(matched), len(unmatched_expected), len(false_positives)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    drift = [abs(w - g) for w, g in matched]

    return {
        "fixture": truth_path.name,
        "profile": profile_name,
        "expected": len(expected),
        "found": len(found),
        "true_positives": tp,
        "false_negatives": fn,
        "false_positives": fp,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "max_drift_s": round(max(drift), 3) if drift else None,
        "mean_drift_s": round(sum(drift) / len(drift), 3) if drift else None,
        "missed_at": [round(t, 2) for t in unmatched_expected],
        "spurious_at": [round(t, 2) for t in false_positives],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("truth", type=Path, nargs="+")
    ap.add_argument("--profile", default="vhs", choices=list(PROFILES))
    ap.add_argument("--tolerance", type=float, default=1.0)
    args = ap.parse_args()

    rows = [score(t, args.profile, args.tolerance) for t in args.truth]
    print(json.dumps(rows, indent=2))

    print("\n" + "=" * 68)
    print(f"{'fixture':<28} {'prof':<8} {'P':>6} {'R':>6} {'F1':>6} {'drift':>7}")
    print("-" * 68)
    for r in rows:
        drift = f"{r['mean_drift_s']}s" if r["mean_drift_s"] is not None else "—"
        print(f"{r['fixture']:<28} {r['profile']:<8} {r['precision']:>6.2f} "
              f"{r['recall']:>6.2f} {r['f1']:>6.2f} {drift:>7}")
    print("=" * 68)


if __name__ == "__main__":
    main()
