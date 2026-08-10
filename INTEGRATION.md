# 8008TUB3 × FieldStation42 — Integration Plan

*Verified against `vendor/FieldStation42` @ `2baa022` (per `UPSTREAM.md`) and the 8008TUB3 source at the repo root on 2026-08-07. Every file:line below was opened, not inferred.*

---

## 1. Subsystem table

| Subsystem | Verdict | Deciding evidence |
|---|---|---|
| **Catalog** | **Read its data + drive its rebuild.** Write files to disk, trigger `ShowCatalog(conf, rebuild_catalog=True)`, read back with SQL. Keep an 8008TUB3-owned ad DB alongside. Do not write `catalog_entries` rows. | The whole ingest contract is a directory: `fs42/catalog.py:354-357` resolves a tag to `f"{content_dir}/{tag}"` and `fs42/media_processor.py:265` globs it. No sidecar, no manifest, no naming convention — exactly "point at commercials". But `CatalogEntry` (`fs42/catalog_entry.py:17-32`) has nowhere to put adsplice's real output: `Spot.fingerprint`, `confidence`, `bucket`, `duplicate_of` (`adsplice/pipeline.py:40-54`). So FS42 owns the index; we own the metadata. Writing rows directly is pointless — `CatalogAPI.set_entries` starts with `delete_catalog`. |
| **Scheduler** | **Read its data; drive generation out-of-process. Keep `tuner/schedule.py`'s interface. DELETE `TimetableChannel`. Demote `LoopChannel` to fallback.** | Importing it into the tuner is impossible: I ran `python3 -c "from fs42.liquid_manager import LiquidManager"` in the vendor checkout and the interpreter printed an ffmpeg banner and exited — `fs42/media_processor.py:23,34` calls `sys.exit(1)` at module scope. A tuner that must never die cannot take that dependency. But `plan_json` (`fs42/liquid_io.py:177`) is the entire reason to vendor FS42: a flat, ad-pod-resolved, seek-ready playlist of `{path, skip, duration, content_type}`. Read it with SQL; it needs no `fs42` import at all. **`TimetableChannel` (`tuner/schedule.py:122-147`) is now dead weight** — a FS42 standard schedule is a timetable that also carries commercials. `LoopChannel` survives because `(at - epoch) % total` cannot expire (`tuner/schedule.py:91`), which is exactly what you want when a FS42 schedule runs out. |
| **Ad pods** | **Import as a library and subclass.** `class Tub3Catalog(ShowCatalog)` overriding one method, plus a one-line module rebind. Zero FS42 files edited. | `find_commercial` is the sole chokepoint — called via `self.` from `fs42/catalog.py:681` (`make_reel_block`) and `:738` (`make_reel_fill`). `find_candidate` already implements a tested `exclusion_index`/`proposed_start` mechanism (`fs42/catalog.py:508,534-545`) that upstream never wires to commercials — `find_commercial` at `:633` calls it with three arguments. And `fs42/liquid_schedule.py:10` *from-imports* `ShowCatalog`, constructing it at `:35`, so rebinding the module attribute substitutes our subclass with no fork. I confirmed `fs42/liquid_schedule.py:35` is the **only** construction site on the ad-selection path (`fs42/liquid_manager.py:75` is sequence reset; the rest are catalog rebuilds). |
| **Player** | **Keep ours; discard FS42's entirely.** | `fs42/station_player.py:183` constructs mpv with `hr_seek="yes"` (precise seek), calls `mpv.play()` at `:406`, and only *then* seeks at `:432-433` — so mpv presents frames from position zero before jumping. That is a picture-correctness defect, not just latency. `tuner/player.py:195-197` passes `start=+{offset}` as a **loadfile option** with `--hr-seek=no` (`:81`) and times to `playback-restart` (`:207`). Add `~100ms` fixed from `short_change_effect` (`fs42/reception.py:19`) and a 50ms detection poll (`fs42/station_player.py:962`) and FS42's floor is ~250ms before decode. Ours is not importable-around: `StationPlayer` builds its own MPV in `__init__` and blocks in nested `while True: sleep(0.05)` loops in every method. |
| **Input** | **Keep ours; FS42 has nothing to take.** | `runtime/channel.socket`'s entire vocabulary is `direct`/`up`/`down` plus garbage-means-up (`field_player.py:224-282`). SELECT and BACK have nowhere to go. The richer verbs live only on an in-process `multiprocessing.Queue` created at `field_player.py:380` that no external process can put to. HDMI-CEC is absent: a case-insensitive grep for `\bcec\b|cec-ctl|/dev/cec|libcec` across the whole tree returns **zero hits**. `tuner/input.py:205-251` `CecDriver` is a pure 8008TUB3 contribution. |
| **Config / setup UI** | **Keep ours. Write `confs/*.json` directly with our own generator. Do NOT run `fs42_server`. Do NOT generate a form from `station_config_schema.json`.** | The schema is a validation schema, not a form schema: 62 properties, 2 required, exactly one `"default"` in the whole file (`metaHint.exclusive`), no `"title"`, no grouping, no conditionals on `network_type`. Real defaults live in Python (`fs42/station_io.py:22-31`). It also drifts both ways — the code reads `continued`, `start_clip`/`end_clip`, `off_air_image`, `sequence_strategy`; the schema declares dead `runtime_dir`/`catalog_path`/`schedule_path`. The properties that *matter* (seven weekday 24-hour grids, open-ended override maps, free-text paths) are precisely the ones a four-button control cannot render. And the shipped editor is Monaco + purecss from CDNs (`static/station_editor.html:7,84,164`) — needs a keyboard, a desktop, and the internet. Skipping `fs42_server` also removes an unauthenticated `0.0.0.0:4242` service with full station CRUD from a home appliance. |

**Net: one FS42 subsystem is imported (ad pods), one is driven (catalog+schedule generation), one is read (schedule data), three are ours. Zero FS42 files are modified — which under MPL-2.0's file-level copyleft means zero files we must carry, re-merge and republish forever.**

---

## 2. The exact wiring

### 2.1 On-disk layout (this is the contract)

```
/srv/tub3/
  incoming/ads/            user drops anything here
  incoming/shows/          user drops anything here
  staging/ads-cut/         adsplice --out          (intermediate, never catalogued)
  media/                   ← FS42 content_dir, MUST BE ABSOLUTE
      sitcoms/             a tag
      cartoons/            a tag
      commercial/          commercial_dir = "commercial"
      bump/                bump_dir = "bump"       (may be empty; MUST exist and be configured)
  ads.sqlite3              8008TUB3-owned ad metadata
  vendor/FieldStation42/
      confs/tub3_ch3.json
      runtime/fs42_fluid.db   ← relative to FS42 cwd; every FS42 process must chdir here
```

**`content_dir` must be an absolute path.** `fs42/catalog.py:354-357` builds `tag_dir = f"{content_dir}/{tag}"` and `fs42/media_processor.py:265` globs `f"{tag_dir}/*.{ext}"`, so `CatalogEntry.path` inherits absoluteness. `ReelCutter` copies `CatalogEntry.path` verbatim into `BlockPlanEntry` (`fs42/reel_cutter.py:15,27` and `fs42/liquid_blocks.py:356-360`) — **`realpath` never reaches `plan_json`**. Every shipped example uses a relative `content_dir` (`confs/examples/loop_channel.json:5` = `"catalog/loop"`), which would make `plan_json` paths unresolvable from our tuner's cwd. Absolute `content_dir` fixes this at the source with no code.

### 2.2 Pipeline order (order is load-bearing)

```bash
# 1. cut ads  →  staging (NOT the catalog: adsplice uses -c copy, loudness is untouched)
python3 -m adsplice.cli ingest /srv/tub3/incoming/ads \
        --out /srv/tub3/staging/ads-cut --profile vhs

# 2. level ads  →  the catalog. -18 LUFS, so pods never jump against programs.
python3 -m normalize.cli run /srv/tub3/staging/ads-cut \
        --out /srv/tub3/media/commercial --target pi5 --no-chapters

# 3. condition programs → the catalog. Chapters are injected HERE, before FS42 ever indexes.
python3 -m normalize.cli run /srv/tub3/incoming/shows \
        --out /srv/tub3/media --target pi5

# 4. join adsplice fingerprints to final paths  →  ads.sqlite3   (new; see 2.6)
python3 -m tub3.adjoin --report /srv/tub3/staging/ads-cut/adsplice-report.json \
        --normalized /srv/tub3/media/commercial --db /srv/tub3/ads.sqlite3

# 5. catalog + schedule, in ONE process with our subclass bound (see §3)
python3 -m tub3.build --station tub3_ch3 --catalog --add week
```

`adsplice/pipeline.py:384` writes with `-c copy` where possible, so step 2 is not optional — it is the only thing that makes ad breaks match program loudness. Step 3 before any catalog build is not optional either (see 2.4).

### 2.3 What adsplice output must satisfy for FS42 to index it

Every one of these is already true of `adsplice/pipeline.py:383` (`f"{stem}_{index:04d}_{spot.bucket}s.mkv"`), but they are the contract:

- **Extension in `VIDEO_FORMATS`** = `["mp4","mpg","mpeg","avi","mov","mkv","ts","m4v","webm","wmv"]` (`fs42/media_processor.py:56`). `.mkv` qualifies.
- **Filename must not start with `.`** — `fs42/media_processor.py:311,316` skips dotfiles and dot-dirs.
- **Duration ≥ 1.0s**, or the file is indexed but never selectable: `fs42/catalog.py:529` requires `seconds > candidate.duration >= 1`. adsplice's `MIN_SPOT = 8.0` (`pipeline.py:35`) clears it.
- **ffprobe-readable duration > 0**, else the file is silently dropped with only a log line (`fs42/media_processor.py:167-172`).
- **Files must live under `content_dir`**, or `FluidBuilder.scan_file_cache` (which walks only `content_dir`, `fs42/catalog.py:130`) never caches them and every rebuild re-ffprobes every ad.
- **normalize's `.mkv.tub3.json` sidecars are safely invisible** — `fs42/media_processor.py:288-322` filters by extension set; `.json` is not in it. Verified.
- **Never name a generated ad bucket** a month name, `q1`–`q4`, a weekday, or a daypart (`morning`/`daytime`/`prime`/`late`/`overnight`). `fs42/media_processor.py:327-345` turns any such path component into a silent temporal restriction. A bucket folder called `December` would only ever air in December.

### 2.4 How normalize's chapters reach FS42 — zero glue, but ordered

The chain is already complete:

`normalize/chapters.py:101` `to_ffmetadata()` → `normalize/encode.py:213-215` muxes it (`-i meta.ffmeta -map_metadata 1`) → real container chapters → `ffprobe -show_chapters` in `MediaProcessor.chapter_detect` (`fs42/media_processor.py:548-551`) → stored in `chapter_points` keyed by `os.path.realpath` (`fs42/fluid_builder.py:149` → `fs42/fluid_statements.py:187-194`) → read with **first priority** at `fs42/liquid_blocks.py:114`: `break_points = _fluid.get_chapters(self.content.realpath)`, falling back to black-detect only if empty.

The shapes already agree. `to_ffmetadata` emits contiguous spans tiling `0..duration`; `chapter_detect` produces exactly `{"chapter_start", "chapter_end", "segment_duration"}` and `ReelCutter` plays `chapter_end - chapter_start` from `chapter_start` (`fs42/reel_cutter.py:64`). Nothing to translate.

**Four hard conditions:**

1. **normalize must run before FS42's first catalog build of that file.** `fs42/fluid_builder.py:146-151` stores `chapters if chapters else []` and treats *row existence* as "already scanned" — a file indexed before chapters are injected is permanently chapterless. Escape hatch if you get this wrong: `FluidStatements.delete_chapter_points(conn, realpath)` per file, or `station_42.py --reset_chapters` for the whole table.
2. **`break_strategy` must be `"standard"`.** `fs42/reel_cutter.py:20` short-circuits and discards `break_points` entirely for `"end"` and `"center"`.
3. **Programs must be ≥ 300s.** `fs42/media_processor.py:540` returns `None` below `timings.MIN_5`. Conveniently `normalize/encode.py:35` uses the identical `PROGRAM_MIN_DURATION = 300.0` — the two already agree, so no special case is needed.
4. **The final chapter's `END` must reach the container duration.** `normalize/chapters.py:111` writes `END = next_start - 1ms`, and the tail chapter ends at `duration - 1ms`. That 1ms is fine; a larger shortfall would silently truncate the last act.

*Optional optimisation, not required for v1:* pre-seed the `chapter_points` row directly (`REPLACE INTO chapter_points VALUES(path, json, now)`, `fs42/fluid_statements.py:191`) to skip FS42's per-file ffprobe pass at catalog time. Key on `os.path.realpath(final_path)`. Note `FluidBuilder(db_path=...)` is broken — `fs42/fluid_builder.py:13-15` never assigns `self.db_path` when you pass one, so line 18 raises `AttributeError`. Use plain `sqlite3` against `vendor/FieldStation42/runtime/fs42_fluid.db`.

### 2.5 The minimal station config that actually works

Not what the schema says. Three keys are required by code and absent from both the schema's `required` list and `OVERWATCH_DEFAULTS`:

- **All seven weekday keys.** `network_type` defaults to `"standard"` (`fs42/station_io.py:22`), and `SlotReader.smooth_tags` does a bare `conf[day_index]` subscript (`fs42/slot_reader.py:164`) outside any try/except at `fs42/station_manager.py:55-58`. I executed it against `{network_name, channel_number, network_type, content_dir}`: `KeyError: 'monday'`, escaping the constructor. Use the `day_templates` + 7 references idiom from `confs/examples/movie_channel.json`.
- **`commercial_dir`** (or `commercial_free: true`). `fs42/catalog.py:629` is a bare `self.config["commercial_dir"]`.
- **`bump_dir` — this one is not in any prior analysis.** `fs42/catalog.py:567-568`: `if not bump_tag: bump_tag = self.config["bump_dir"]`, a bare subscript. Catalog build survives its absence (`fs42/catalog.py:278` guards with `if "bump_dir" in self.config`), but `make_reel_block` always calls `find_bump` with `bumpers=True` (`fs42/catalog.py:681`, `make_reel_fill` default `use_bumpers=True`), so **schedule build KeyErrors**. The directory may be empty — `_scan_directory` creates the empty key first — but the config key must exist.

Also set `be_right_back_media` (no default; without it `fs42/catalog.py:753` silently zeroes the leftover gap) and `standby_image`.

Emit configs from an 8008TUB3-owned template writing `vendor/FieldStation42/confs/<name>.json`. Do not POST `static/templates/blank.json` — it writes a poisoned config to disk *then* 500s, bricking every subsequent boot.

### 2.6 The read seams

**Schedule → tuner. Pure SQL, no `fs42` import:**

```sql
SELECT start_time, end_time, title, plan_json
  FROM liquid_blocks
 WHERE station = ? AND start_time < ? AND end_time > ?
 ORDER BY start_time;
```
(`vendor/FieldStation42/runtime/fs42_fluid.db`; schema at `fs42/liquid_io.py:40-52`.) `plan_json` decodes to a list of `{path, skip, duration, is_stream, content_type, media_type}`.

**Bind `datetime` objects, never `.isoformat()` strings.** Stored values are `'YYYY-MM-DD HH:MM:SS'` (space separator) compared as TEXT (`fs42/liquid_io.py:100-103`); `'T'` (0x54) sorts above `' '` (0x20), so ISO strings silently return the wrong rows.

**`LiquidChannel` — new third `Channel` subclass in `tuner/schedule.py`.** Mirror `LiquidManager.get_play_point`'s arithmetic (`fs42/liquid_manager.py:191-199`) over a lazily loaded window (now−1h → now+6h) with prefix sums, so tuning stays a bisect and does no SQL. Copy the maths, **not** the bugs:

- FS42's block test is doubly-inclusive (`when >= start and when <= end`, `fs42/liquid_manager.py:164`). Use half-open `start <= t < end`.
- `get_programming_block` falls through and returns `None` on a schedule *gap*; `get_play_point` then evaluates `_block.plan` on `None` at `:192` → `AttributeError`, which `field_player` does not catch. Gaps are producible (`schedule_offset` is re-applied on every `_increment`, `fs42/liquid_schedule.py:482-484`). Return an explicit off-air `Airing` — `tuner/box.py:91-97` already draws the OFF AIR card correctly.
- Convert to/from epoch floats at the boundary and compare in epoch seconds (DST — see §5).

**`Airing` must gain two fields** (`tuner/schedule.py:43-56`). Today it carries one file and one offset; a `PlayPoint` is an *index into a plan list*, and the plan is where the commercials are. Add:
- `skip: float` — the real punch-in is `entry.skip + play_point.offset` (`fs42/station_player.py:852`). Without it, every non-zero-skip entry starts at the wrong place.
- `plan: list[PlanEntry]` and `index: int` — so `tuner/box.py:189` `_advance_if_ended` walks to `plan[index+1]` with `skip` applied and `offset=0`, instead of re-querying the clock on every entry boundary (a pod is 4–8 entries; re-tuning from scratch each time is wasteful and races the clock).

**Catalog → wizard.** `SELECT path, duration, tag FROM catalog_entries WHERE station=? AND content_type='commercial'` answers "how many spots, what lengths" for `tuner/menu.py:228`'s `Commercials` INFO row, which is currently hardcoded `"1,284 spots"`.

**Staleness → supervisor.** `SELECT MIN(start_time), MAX(end_time) FROM liquid_blocks WHERE station=?` on our own wall-clock timer. **Never** let FS42's `schedule_panic` path run: `fs42/station_player.py:800-814` calls `add_days(1)`, and `_increment` resumes from the *stale* `_end_time()` (`fs42/liquid_schedule.py:471-481`), so a week offline costs ~7 full day-builds at one per retry, with a standby image on screen throughout.

### 2.7 8008TUB3's own ad DB (`ads.sqlite3`)

FS42 has nowhere to put fingerprints, so we keep them. `adsplice-report.json` (`adsplice/pipeline.py:411`) records `output` = the **staging** path; `normalize/encode.py:266` writes `source_path` into each `<final>.mkv.tub3.json`. Join on those two to map fingerprint → final path:

```sql
CREATE TABLE ads (
  realpath   TEXT PRIMARY KEY,   -- os.path.realpath of the file in media/commercial
  cluster_id INTEGER NOT NULL,   -- perceptual cluster (see §3)
  duration   REAL NOT NULL,
  bucket     INTEGER,            -- 10/15/30/60/120, adsplice/pipeline.py:80
  confidence REAL,
  fingerprint TEXT,              -- json list[int], adsplice/phash.py:49
  source     TEXT
);
CREATE TABLE ad_airings (realpath TEXT, aired_at TIMESTAMP);
```

`cluster_id` comes from re-running `phash.dedupe` (`adsplice/phash.py:95`) across the *whole* commercial folder rather than one ingest run — today dedupe only spans a single `ingest()` call, so a spot ingested last month and the same spot ingested today are two independent entries.

---

## 3. The dedupe hook

**Forking is not necessary.** One subclass, one method, one module rebind. Zero FS42 files touched.

### Why this hook and not another

`find_commercial` (`fs42/catalog.py:628`) is the *only* place a commercial is chosen — both callers (`fs42/catalog.py:681` and `:738`) reach it through `self.`, so overriding it intercepts 100% of ad selection without touching `reel_cutter.py`, `liquid_blocks.py` or `make_reel_block`. And `find_candidate` already has the filter built and unit-tested (`test/test_exclusion.py:179-300`); upstream simply never passes it from the commercial path.

The exclusion contract, read off `fs42/catalog.py:534-545`: `{realpath: [(start_dt, end_dt), ...]}`, with a half-open overlap test against `proposed_start .. proposed_start + candidate.duration`. **Both** `exclusion_index` and `proposed_start` are required — omitting the latter silently disables the check.

A recency cooldown is one window `(T, T + COOLDOWN)`. **Perceptual dedupe is the same mechanism applied to every `realpath` in the cluster** — when a spot airs, register the window on all its siblings. That is the entire trick; FS42 needs no change.

### The code

```python
# tub3/adcatalog.py — 8008TUB3-owned. No FS42 file is modified.
import datetime, json, sqlite3
from fs42.catalog import ShowCatalog, MatchingContentNotFound, NoFillerContentFound

COOLDOWN = datetime.timedelta(minutes=45)

class Tub3Catalog(ShowCatalog):
    def __init__(self, config, ads_db="/srv/tub3/ads.sqlite3", **kw):
        self._members = _load_clusters(ads_db)   # {realpath: [sibling realpaths]}
        self._blocked = {}                       # {realpath: [(start, end)]}
        self._cursor = None                      # synthetic intra-block clock
        super().__init__(config, **kw)

    def seed(self, station, since):
        """Carry cooldowns across builds: FS42 never persists commercial counts."""
        for realpath, aired in _recent_airings(station, since):
            self._register(realpath, aired)

    # --- the one override -------------------------------------------------
    def find_commercial(self, seconds, when, commercial_dir):
        tag = commercial_dir or self.config.get("commercial_dir")
        if not tag or not self.clip_index.get(tag):
            raise NoFillerContentFound(f"No commercials indexed under {tag!r}")

        # Every commercial in a block is offered the SAME `when` — liquid_blocks.py:170
        # passes self.start_time to make_reel_fill. Advance our own monotonic cursor by
        # each pick's duration so two copies of one spot cannot land in the same block.
        at = max(self._cursor, when) if self._cursor else when

        for index, start in ((self._blocked, at), (None, None)):   # 2nd pass = give up
            try:
                pick = self.find_candidate(tag, seconds, when,
                                           exclusion_index=index, proposed_start=start)
            except MatchingContentNotFound:
                continue
            if pick is None:            # find_candidate falls off the end, catalog.py:517
                continue
            self._cursor = at + datetime.timedelta(seconds=pick.duration)
            self._register(pick.realpath, at)
            return pick
        raise NoFillerContentFound(f"Nothing under {seconds}s in {tag!r}")

    def _register(self, realpath, at):
        if not realpath:
            return
        window = (at, at + COOLDOWN)
        for sibling in self._members.get(realpath, [realpath]):
            self._blocked.setdefault(sibling, []).append(window)
```

```python
# tub3/build.py — substitution point
import os; os.chdir("/srv/tub3/vendor/FieldStation42")     # FS42 is cwd-relative throughout
import fs42.liquid_schedule as ls
from tub3.adcatalog import Tub3Catalog

ls.ShowCatalog = Tub3Catalog          # LiquidSchedule.__init__ resolves this global at
                                      # call time — fs42/liquid_schedule.py:10 imports the
                                      # NAME, :35 constructs it. Verified: this is the only
                                      # construction site on the ad-selection path.
sched = ls.LiquidSchedule(conf)
assert isinstance(sched.catalog, Tub3Catalog)   # smoke test: fails loudly on upstream drift
sched.add_amount("week")
```

### Five things that make this correct rather than merely plausible

1. **The two-pass retry is mandatory.** `make_reel_fill`'s handlers (`fs42/catalog.py:711,742`) catch only `MatchingContentNotFound`. `NoFillerContentFound` is a sibling of `Exception`, **not a subclass** (`fs42/catalog_entry.py:7,11`), so it aborts the whole schedule build. Never let the cooldown starve the pool — fall back to no index rather than raise.
2. **`when` has no intra-block resolution.** `fs42/liquid_blocks.py:170` passes `self.start_time` to `make_reel_fill`, so every commercial in a 30-minute block shares one timestamp. Without the synthetic `_cursor`, two copies of the same spot can still land in the same pod. This is the non-obvious part.
3. **Seed from the existing schedule.** `result.count += 1` at `fs42/catalog.py:553` is in-memory only — `CatalogAPI.set_play_count` on the next line is commented out, and `fs42/liquid_schedule.py:456-461` only persists `block.content` (features). So every `add_week` restarts commercial rotation from zero. `seed()` reading the trailing `COOLDOWN` of `plan_json` where `content_type='commercial'` fixes exactly the "append another week" case.
4. **`realpath` is populated for commercials.** Standard stations get a `FluidBuilder` (`fs42/catalog.py:120-136`), and `process_one` sets `result.realpath = os.path.realpath(fname)` only when one is present (`fs42/media_processor.py:149,178`). Loop channels get the literal `False`. Another reason ad channels must be `network_type: "standard"`.
5. **`station_42.py -w` will NOT use the subclass** — it constructs its own `LiquidSchedule` in a process where the rebind never happened. All schedule generation must go through `tub3/build.py`. Catalog rebuild (`-r`) is safe either way, since catalog build selects no commercials.

---

## 4. What to build next, in order

**Step 1 — the walking skeleton (`tub3/bootstrap.py`). This is the smallest thing that produces a watchable channel with real ad pods.**

One script: `--programs DIR --ads DIR --channel 3`. It (a) writes `vendor/FieldStation42/confs/tub3_ch3.json` from our template — absolute `content_dir`, `commercial_dir`, `bump_dir`, `be_right_back_media`, `break_strategy: "standard"`, `day_templates` + all seven weekday keys; (b) `os.chdir` to the FS42 root; (c) `ShowCatalog(conf, rebuild_catalog=True)`; (d) `LiquidSchedule(conf).add_days(1)`.

No adsplice, no normalize, no dedupe. Point it at a folder of already-cut ads and a folder of shows. You get real ad pods immediately — with no chapters, `fs42/reel_cutter.py:36-43` still inserts mid-rolls at `duration / break_count`, just mid-scene. **The channel must be `network_type: "standard"`** — I checked `LiquidLoopBlock.make_plan` (`fs42/liquid_blocks.py:304-330`) and it emits back-to-back content with **no reel blocks at all**. A loop channel can never have commercials.

**Step 2 — `LiquidChannel` + `Airing.skip`/`.plan`.** ~120 lines in `tuner/schedule.py`. Window-loading SQL reader, prefix sums, half-open bounds, explicit off-air on gaps. Delete `TimetableChannel` and `Slot` in the same commit. Update `tuner/box.py:100` to `player.tune(entry.path, entry.skip + airing.offset)` and `_advance_if_ended` to walk the plan. **Now you can watch it**, and the ad pods play.

**Step 3 — `tub3/build.py` + `Tub3Catalog`.** The rebind, the subclass, the smoke test. Cooldown only (no clustering yet — one row per file, `cluster_id = rowid`). This alone stops the same file recurring within 45 minutes, which FS42 cannot do at all.

**Step 4 — normalize into the loop.** Run `normalize.cli run` over programs *before* the first catalog build, wire it into `bootstrap.py`. Mid-rolls move from arbitrary thirds to real act breaks, and every ad break stops jumping in volume. Biggest single quality jump per line of code, and it needs no FS42 change.

**Step 5 — adsplice + `tub3/adjoin.py` + real clusters.** Whole-folder `phash.dedupe`, populate `ads.sqlite3`, switch `_members` to real clusters. Now two rips of one spot cannot share a break.

**Step 6 — the supervisor.** `MIN/MAX(start_time)` poll on a wall-clock timer, `add_amount("week")` in a subprocess with generous headroom, `LiquidChannel` window invalidation on completion, and a `LoopChannel` fallback if a schedule is ever exhausted anyway. Wire `tuner/menu.py:220` "Rebuild schedule" to it — it's currently a lambda returning a string.

**Step 7 — the browser wizard.** Our own, writing `confs/*.json` via the Step 1 generator. `tuner/menu.py` stays status-only, per its own rule at `menu.py:11-14`.

**Step 8 — the flashable image.** systemd: a fat `tub3-build.service` (ffmpeg-python, moviepy, FS42) and a thin `tub3-tuner.service` (mpv + stdlib only). That split is what makes the "import can `sys.exit`" hazard structurally harmless.

---

## 5. Risks and unknowns

**Blockers, ordered by how likely they are to bite:**

1. **Chapter markers cache permanently on first scan.** `fs42/fluid_builder.py:146-151` writes `[]` for a chapterless file and treats row existence as "scanned". If normalize runs after FS42 indexes a program, FS42 never sees the act breaks. Mitigation: enforce order in `bootstrap.py`, and have `normalize` emit a list of re-encoded realpaths that the build step feeds to `FluidStatements.delete_chapter_points`.
2. **`bump_dir` KeyErrors schedule build when absent** (`fs42/catalog.py:568`) even though catalog build tolerates it. Not in the schema's `required`, not in `OVERWATCH_DEFAULTS`. Our template must always emit it.
3. **Regenerating an overlapping range silently doubles the schedule.** `fs42/liquid_io.py:182-198` says `INSERT OR REPLACE`, but the only unique constraint is `id AUTOINCREMENT` and all three indexes are non-UNIQUE — the conflict clause can never fire. Any "regenerate this channel" button must `delete_liquid_blocks` first.
4. **DST, twice a year.** FS42 stores naive local datetimes and marches forward with pure `timedelta` (`fs42/liquid_schedule.py:68-71`). On fall-back, `get_programming_block`'s linear scan returns the *earlier* of two identical local hours — an hour of already-aired programming. On spring-forward, an hour of scheduled content silently never plays. Mitigation: `LiquidChannel` compares epoch seconds, converting only for display and only at the SQL boundary.
5. **Keyframe seek vs. short entries.** `tuner/player.py:81` runs `--hr-seek=no`, and `normalize/encode.py:226` sets `-g` at `gop_seconds=2.0` **only when re-encoding** — stream-copied files keep their source GOP. Tuning into a 15-second commercial in a 10-second-GOP file can land ~5s off, a third of the spot. Programs are unaffected (invisible), and pods play with `skip=0` so only the entry you tune *into* is at risk. Mitigation: if the entry we'd punch into is a `content_type == "commercial"` with under ~5s remaining, advance to the next plan entry instead of seeking. Cheap, and it also avoids a one-frame ad.
6. **`glob` metacharacters.** `fs42/media_processor.py:265` globs `f"{tag_dir}/*.{ext}"`. A `[` or `?` anywhere in `content_dir` silently yields zero files. Reject them in the wizard.
7. **Everything FS42 is cwd-relative** — `db_path` default `"runtime/fs42_fluid.db"` (`fs42/station_manager.py:42`), `confs/` and the schema path (`fs42/station_io.py:43-44`). Every FS42-touching process must `chdir` to the vendor root. `FluidBuilder(db_path=...)` cannot override it (`fs42/fluid_builder.py:13-15` leaves `self.db_path` unassigned → `AttributeError`).
8. **`StationManager` calls `exit(-1)` on config errors** (`fs42/station_manager.py:72,192,214`) and is a borg singleton. Never embed it in a long-lived process; `tub3/build.py` must be short-lived and supervised.
9. **`ShowCatalog._fluid_cache_scanned` is a mutable class attribute** (`fs42/catalog.py:50`). Any process rebuilding twice must call `ShowCatalog.clear_fluid_cache()` first, or the second rebuild skips every directory walk and misses newly added ads.
10. **`fs42/catalog.py:669` uses `prebump` for `end_candidate`** in the autobump `"start"` strategy, and `:675` dereferences a possibly-`None` `start_candidate`. Never emit the `autobump` key from our generator.
11. **`_flood` can make zero progress** for loop channels: `fs42/liquid_schedule.py:68` iterates `range(diff.days)` (integer days) while `timings.next_week` returns hour 6. With `schedule_offset` configured, the end time can drift onto a Sunday after 06:00 and every subsequent `add_week` becomes a no-op forever. Never emit `schedule_offset`.

**Unknowns the source cannot settle:**

- **Real channel-change latency of `LiquidChannel` on a Pi.** Everything above is code reading. There is no populated FS42 install in the checkout — `confs/` contains only `examples/` and `runtime/` does not exist — so nothing was measured end to end. In particular the "`plan_json` paths are absolute when `content_dir` is absolute" conclusion is traced through `catalog.py:354` → `media_processor.py:265` → `reel_cutter.py:15`, but never executed.
- **Our own latency numbers are self-reported and internally inconsistent**: `tuner/box.py:12-13` says "~17ms median and 50ms worst"; `tuner/schedule.py:5-6` says "measured at ~25ms". `tests/bench_mpv_tune.py` is the harness but no result set is committed. Re-measure on real hardware before treating either as a budget.
- **adsplice on real tape.** `README.md` says so itself: 1.00/1.00 precision and recall are against synthetic fixtures with a clean head-switching band and consistent gap lengths. Real VHS has tracking errors, dropouts, and spots never separated by clean black.
- **Whether `phash`'s duration-bucket guard survives real ad libraries.** `adsplice/phash.py:15-17` flags the known limitation honestly: two cutdowns of one campaign share footage. The `duration_tolerance=2.0` guard keeps :30 and :60 apart, but two :30 variants of the same campaign will collapse — and arguably should.
- **Upstream churn.** FieldStation42 has no tags, no releases, and no test covering `PlayPoint`, `BlockPlanEntry`, `find_commercial` or the `liquid_blocks` schema (`test/` holds 8 files, none of them scheduler contracts). The pin in `UPSTREAM.md` plus the `isinstance` smoke test in `tub3/build.py` plus a golden-row test over `plan_json`'s field names are the only defences.
