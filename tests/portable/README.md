# Is the scheduler portable?

Harnesses for the question in `STATUS.md` — *could the scheduler run somewhere other than the
Pi, talking only to Plex?* None of these are unit tests; they measure the live library and
answer with numbers.

| script | question |
|---|---|
| `durdiff.py`  | do Plex's durations agree with the catalogue's? |
| `memdiff.py`  | does Plex list the same files a folder walk finds? |
| `catdiff.py`  | would a Plex-backed catalogue hand the scheduler the same entries? |
| `plexdur.py`  | run a real build with durations from Plex and nothing else changed |
| `blockdiff.py`| diff two builds block for block |
| `versions.py` | does the map address the right *version* of a film? |
| `identity.py` | does Plex serve the version the map named? checked by id, not by a 200 |
| `tripwire.py` | does the staleness check survive a film with two versions, and a Plex that is down? |

## Running a build without touching the live one

`tub3/schedules.py` does `os.chdir(VENDOR)` before building, so a scratch *working directory*
does not isolate anything — the first attempt wrote to the live database. Isolation needs a
scratch `VENDOR`, because `VENDOR` is derived from the module's own location:

    /tmp/parallel/repo/
        tub3/ tuner/ adsplice/ ... brand.py       copies, so __file__ resolves here
        vendor/FieldStation42/
            fs42 -> real          symlink, read only
            confs -> real         symlink, read only
            runtime/              a real directory, private to the run

Pre-flight it before running anything:

    python -c "from tub3.schedules import VENDOR; print(VENDOR)"

and keep an `md5sum` of the live database either side. Build `nice -n 19`; the box is a
television and someone is probably watching it.

## Why `identity.py` checks an id and not a status code

Plex's transcode endpoints do not validate `mediaIndex` the way you would hope. An
out-of-range value is a 400, but a missing, negative or non-numeric one answers **200 and
quietly serves version 0** — which is indistinguishable from a correct request for a
single-version film. So a green request proves nothing. The check that discriminates is to
resolve a known second-version file, ask Plex to decide with the index the map gave, and
assert the `Media id` that comes back is the one the map named. Ids are stable; the index is
positional and Plex orders versions by resolution, so importing a better copy re-seats index
0 onto a different file and every stored index shifts.
