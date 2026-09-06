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
