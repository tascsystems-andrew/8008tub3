# 8008TUB3

A 90s cable TV appliance. Point it at your media, point it at a folder of commercials, and
you get television — with real ad breaks, act-break mid-rolls, and a scrolling guide channel.

**This is a distro, not a media player.** Playout and scheduling come from
[FieldStation42](https://github.com/shane-mason/FieldStation42) (MPL-2.0), which already does
that work well. 8008TUB3 adds the parts nobody has built: a flashable image, a setup wizard
that doesn't involve editing JSON, a commercial ingest pipeline, and an HDMI-CEC bridge so the
TV's own remote drives the box.

Mint to Ubuntu. The engine exists and is free; what's missing is somebody making it boot.

See [`../8008tub3-plan.md`](../8008tub3-plan.md) for the architecture and
[`../8008tub3-market-research.md`](../8008tub3-market-research.md) for why this shape.

## Status

Early. `adsplice` — the ingest pipeline, and the one genuinely unoccupied piece — works
end to end and is validated against synthetic fixtures with ground truth. Nothing else is
built yet.

## adsplice

Turns a folder of *anything* into a folder of cut, deduplicated, tagged commercials.

```bash
python3 -m adsplice.cli ingest ~/commercials --out /media/ads --profile vhs
python3 -m adsplice.cli selftest
```

It never asks what you have. A folder of 400 thirty-second clips and a folder of twelve
ninety-minute tapes are the same command — classification is inferred per file from duration,
and mixed folders are handled item by item.

It never produces a to-do list. Segments below the confidence floor are discarded, not queued
for review. Commercials are fungible and you will have thousands; losing 15% to caution costs
nothing, while an hour of human review per hour of footage costs the project.

### Three things here that no existing tool does

**Analog pre-crop.** VHS captures carry a head-switching noise band along the bottom edge that
is never dark. Whole-frame black detection scored against those pixels doesn't degrade on
analog sources — it fails silently and completely. Cropping before measuring is the fix, and
it's decisive: on the same file, the `vhs` profile scores 1.00 precision and 1.00 recall where
the standard `digital` profile scores 0.00.

**The duration prior as evidence, not a filter.** Commercials are cut to :15, :30 and :60.
Every existing detector uses duration as a post-hoc reject rule at best. Here it's a scoring
signal, weighted by how common each length actually is — :20 and :45 exist but are rare, and
treating them as equally likely lets a fragment masquerade as a whole spot.

**Fragment recombination.** A commercial with an internal fade or a dark scene trips
`blackdetect` and gets cut in half; neither piece then lands near a canonical length, so both
fail the confidence floor and the whole spot is lost. So: when consecutive fragments sum to
very close to a canonical length *and* the boundaries between them were weak, put them back
together. Merging is refused across a strong boundary — black and silence in firm agreement —
because that's a real break between two commercials.

**Perceptual dedupe**, which FieldStation42 lacks entirely (it rotates by lifetime play count,
so two rips of the same spot are two independent catalog entries and can both land in one
break). A difference hash over sampled frames, no Python dependencies, so it runs on a Pi.
Clips only collapse if they're visually similar *and* close in length, which keeps the :60 and
:30 edits of one campaign as distinct assets.

### Validation

`tests/make_fixture.py` synthesises a commercial reel with known ground truth — distinct
generated "spots" at canonical lengths, separated by black-and-silent gaps — in a clean digital
variant and a VHS variant with grain, a head-switching band, a raised audio noise floor, and
black that is never truly black. `tests/score.py` scores detection against it.

This is why `selftest` exists: a freshly flashed box can prove its own ingest path works before
the user has supplied a single file.

Current results on a 9-spot VHS-variant reel with two repeated commercials:

| Metric | Result |
|---|---|
| Boundary precision / recall (VHS profile) | 1.00 / 1.00 |
| Same file, digital profile | 0.00 / 0.00 |
| Spots recovered | 9 / 9 |
| 60s spots reassembled from fragments | 2 / 2 |
| Duplicates caught | 2 / 2, no false positives |
| Mean boundary drift | 137 ms |

**These are synthetic fixtures.** They prove the algorithm and the pipeline, not real tape.
Actual VHS will be messier — tracking errors, dropouts, gaps of inconsistent length, and spots
that were never separated by clean black at all. Treat these numbers as a floor on a known
input, and re-measure on real footage before trusting them.

## Licence

MPL-2.0, matching FieldStation42 — file-level copyleft, so modified files stay open and
anything new stays yours.

**The software ships with zero content.** It ingests only user-supplied files. Vintage
commercials are copyrighted works; do not bundle, host, or redistribute them.
