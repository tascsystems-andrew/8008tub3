# 8008TUB3

A 90s cable TV appliance for a Raspberry Pi. Point it at your media and a folder of
commercials, and you get **television** — channels that are already mid-episode when you tune
in, real ad breaks, station idents, a scrolling guide, and a set that answers your TV's own
remote.

Not a media player. There is no library grid, no "resume", no play button. You turn it on and
something is on. If you don't like it you change the channel.

```
   2  GUIDE          the scrolling guide, with music
   3  BOOBTUBE       all-day family channel, dayparted
   4  SATURDAY AM    cartoons, Saturday mornings only
   5  SUNNY DAYS     preschool, with interstitials
   6  THE PICTURES   films, 2-hour blocks
   7  MATINEE        afternoon films
   8  THE GOOD LIFE  lifestyle, 5-minute grid
   9  FAR AFIELD     travel and motoring
  10  AFTER DARK     evening drama
  11  LAST LAUGH     comedy
  12  THE ZONE       genre and cult
  13  AMBIANCE       a fireplace, a fish tank, a yule log
  14  BBC            the Boobtube Broadcasting Corporation
```

**This is a distro, not a rewrite.** Playout and schedule generation come from
[FieldStation42](https://github.com/shane-mason/FieldStation42) (MPL-2.0), pinned by SHA in
[`UPSTREAM.md`](UPSTREAM.md). **Zero upstream files are modified** — 8008TUB3 subclasses,
drives and reads it. What's added is everything between "a scheduling engine exists" and "the
television in the living room works": a tuner that survives, HDMI-CEC, casting, a watershed, a
commercial ingest pipeline, and a box that boots into a picture with nobody administering it.

---

## Status

**Running daily in a real living room.** Eleven scheduled channels plus a guide and an ambiance
channel, driven by a USB remote and the TV remote over CEC.

| | |
|---|---|
| Tuner, channel changes, guide, menu | works |
| HDMI-CEC — TV remote drives the box, box turns the TV on | works |
| Schedule generation, auto top-up every 6h | works |
| Watershed — nothing over 14A before 8pm | works |
| Commercial ingest (`adsplice`) | works, validated against synthetic fixtures |
| AirPlay mirroring | works |
| **Casting from the YouTube app** | **works** — as a screen, not a speaker |
| Flashable image | not built — provisioning is a script, not an image |
| Setup wizard | partial — the web UI does settings, the lineup is still JSON |

---

## Hardware

- Raspberry Pi 5 (a 4 will do; the 5 decodes 1080p h264 comfortably)
- Raspberry Pi OS **Lite**, Bookworm, 64-bit — no desktop, mpv renders straight to KMS
- An HDMI cable to a TV that speaks CEC (most do, under a vendor name: Anynet+, Bravia Sync,
  SimpLink, Viera Link)
- Storage for media: a NAS over SMB, or a USB disk
- Optionally a USB remote — anything that presents as a keyboard

---

## Quick start

### 1. Before the card ever boots

Flash Raspberry Pi OS Lite with the Imager, set the hostname, user and Wi-Fi in its advanced
options, then — while the boot partition is still mounted on your Mac:

```bash
bash packaging/check_sdcard.sh --fix
```

Worth the thirty seconds. It catches the two CEC traps and the headless traps, each of which
otherwise costs you a boot, a diagnosis and a reboot — and the headless ones present as a Pi
that boots perfectly and is simply unreachable.

### 2. Provision the Pi

```bash
sudo bash packaging/pi_setup.sh
```

Idempotent, so re-run it after any change. It installs mpv, ffmpeg, the CEC tools and Samba;
builds a **separate** build virtualenv; installs the three services and the top-up timer; and
sets up a drop share so you can copy media to the box from Finder.

The split virtualenv is load-bearing, not tidiness. FieldStation42 calls `sys.exit()` at module
scope when ffmpeg is missing — an *import* that can kill the process. So the tuner runs on
system Python with mpv and the standard library and nothing else, and a broken build
environment cannot stop the television starting.

### 3. Tell it where the media is

Copy the examples and edit:

```bash
cp settings.example.json settings.json
cp lineup.example.json   lineup.json
```

`settings.json` is the small stuff — where the programmes are, where the commercials are, ad
load, cooldown. Most of it is also editable from the web UI at `http://<box>:8008`.

**The ambiance channel** reads `ambiance_dir`, and inside it one folder per month — `09/`,
`12/`, or `December/`, whichever is easier to remember. Drop a video or two in for the month
and that is the entire configuration: no catalogue, no schedule, just a playlist on loop, which
is also why it keeps working when the NAS is busy and the rest of the dial is slow.

A month with nothing of its own borrows from the nearest month that has something. That
fallback exists because the alternative was worse: August ended, `09/` was empty, and the
channel simply stopped appearing on the dial with nothing anywhere saying why. An empty folder
should cost you a seasonal mismatch, not a missing channel.

**Time of day is read from the name.** The day is cut into four:

| | |
|---|---|
| morning | 05:00 – 11:00 |
| afternoon | 11:00 – 16:00 |
| evening | 16:00 – 21:00 |
| night | 21:00 – 05:00 |

`... Morning` or `... Sunrise` is offered in the morning. **The dark half pools**: `... Sunset`
and `... at Night` are both offered from 16:00 right through to 05:00, because a month usually
holds one dark clip and splitting dusk from night leaves whichever half missed out with nothing
seasonal on. The light half stays strict. A name that says nothing about the hour is offered
always, which is the common case and the safe default.

Only whole words count, and only strong ones. `Rainy Day` and `Late Summer Harvest` are about
weather and the season, not the clock, so they stay unpinned. Matching is by word, not
substring, so `Knightsbridge`, `fortnight` and `Nightingale` are not the night. A title naming
two different times — `Sunrise to Sunset` — spans the day and is treated as unpinned rather
than pinned to whichever word came first.

If the title lies, override it with a folder: `09/evening/whatever.mp4`. A folder is a
decision, a filename is our guess, and the decision wins. Group folders say a span in one
go — `daytime/` (or `day/`, `light/`) for morning and afternoon, `dark/` for evening and
night, `anytime/` for all of it. A clip that arrives as its own folder,
`09/Paris Balcony Jazz at Night/video.mp4`, is read from the folder name.

When nothing suits the current hour — a month holding one clip pinned to the morning — the
whole month plays anyway. A wrong hour is a blemish; an empty channel is a fault.

`lineup.json` is the dial. One entry per channel:

```json
{
  "number": 9,
  "name": "FAR AFIELD",
  "rating": "late",
  "increment": 5,
  "sources": {
    "shows-evening": ["/mnt/tub3/Media/TV/Some Travel Show"]
  }
}
```

- **`number`** — the channel. `2` is reserved for the guide, `13` for ambiance; the audit
  refuses a lineup that puts anything else there.
- **`rating`** — `kids`, `family` or `late`. Gates what may run in children's hours.
- **`increment`** — the schedule grid in minutes. A programme is padded to the smallest
  multiple that fits it, and the surplus becomes advertising. Use `5` for long-form content
  you want rolling continuously; `30` or `60` for a channel that should feel scheduled.
- **`sources`** — tag → folders. Tag names beginning `shows-` are matched by dayparts.
- **`commercials`** — optional, a per-channel ad pool. Omit it and the channel uses the
  general pool; set it and the channel gets only its own (this is how ch14 runs only British
  ads).
- **`interstitials`** — optional, short filler between programmes on channels for very small
  children, where an ad break is the wrong texture.

### The box owns the lineup

`lineup.json` lives on the box, at `~/8008tub3/lineup.json`, and nowhere else. There is no
copy on the Mac and there is none in git — it carries real media paths, which is why it is on
line 17 of `.gitignore`, and this repository is public.

That is a rule rather than an accident, and it was not always true. Two copies were kept in
sync by hand with `scp`, and `deploy_to_pi.sh` did not exclude the file, so a deploy pushed
the Mac's dial over the box's silently — with no version history to recover from and no
warning, because a gitignored file cannot appear in the script's uncommitted-changes check.
It cost nothing only because the two copies happened to be identical. The moment anything
edits the dial on the box, which is the whole point of the channel editor, a deploy would
have destroyed that edit.

So: edit it on the box. Snapshots are kept in `~/8008tub3/lineup-history/`.

### 4. Check it before it writes anything

```bash
.venv-build/bin/python -m tub3.lineup lineup.json \
    --media-root ~/8008tub3/media --ads /mnt/tub3/Media/mshare/Commercials \
    --bumpers ~/8008tub3/bumpers --dry-run
```

Three details in that command, each of which used to be wrong here and each of which fails
in its own way:

- **`.venv-build/bin/python`, not `python3`.** The dry run works under either, so the system
  interpreter looks fine right up until you drop `--dry-run` — and then it walks the whole
  library, writes the pools, and dies on `import PIL` while drawing the ident cards for the
  new channel. `tub3/web.py` carries the same warning about `sys.executable` for the same
  reason.
- **`--media-root ~/8008tub3/media`**, which is where the tag pools live. It is not the media
  share: that is mounted read-only on the box on purpose, so pointing this at
  `/mnt/tub3/Media` cannot write a pool at all.
- **`--ads /mnt/tub3/Media/mshare/Commercials`.** The rating folders are under `mshare`.

The audit prints two lists: **REFUSED**, things that make the lineup unsafe to apply, and
**VETTED**, content allowed into children's hours by explicit override. `apply` raises on the
first problem it meets, which is right for a guard and wrong for a dry run — so the dry run
always shows you the whole list.

Drop `--dry-run` to write the station configs.

### 5. Build the schedule and watch

```bash
.venv-build/bin/python -m tub3.durations
.venv-build/bin/python -m tub3.supervisor
sudo systemctl restart tub3-tuner
```

**Do not pass `--force` when you are adding a channel.** A new station has no blocks, so it
reports zero hours left and lands in `stale` by itself — the plain run builds exactly the new
channels and leaves the rest alone. `--force` rebuilds all of them, and since `schedules.py`
calls `_clear_blocks` before generating, it takes every existing channel dark for the length
of the run. `--force` is for when a schedule is wrong, not for when one is missing.

Building is slow — expect roughly a minute per channel over a network mount, because every
candidate file gets `realpath` and `stat` regardless of what else is cached. Run it once by
hand; after that the timer keeps it ahead of you.

---

## Running it

Four units. The tuner and the settings page are deliberately separate processes: the settings
page has to be reachable when the television is *not* working, which is exactly when you need
it, so it cannot be a thread inside the thing that is failing.

| Unit | What it is |
|---|---|
| `tub3-tuner` | The television. `Restart=always`, `StartLimitIntervalSec=0` — never gives up. |
| `tub3-web` | Settings page on `:8008`. |
| `tub3-build.timer` | Tops the schedule up 2 minutes after boot, then every 6 hours. |
| `tub3-airplay` | `uxplay`, advertising permanently so phones can see it. |

```bash
systemctl status tub3-tuner
journalctl -u tub3-tuner -f          # what the box is doing right now
.venv-build/bin/python -m tub3.supervisor --status    # how much schedule is left
```

**Rebuild early, not on empty.** A generated schedule is finite, and when it runs out the
viewer's experience is that the television broke. Upstream's panic path adds one day at a time
from a stale end time, so a box that has been off a week does seven sequential day-builds with
a standby card on screen throughout. Topping up while hours remain avoids ever meeting it.

### Running it somewhere other than a Pi

```bash
python3 -m tub3.app --windowed --channel 3
```

`--headless` skips the local input devices, `--vo` picks an mpv video output, `--no-web` drops
the embedded settings server.

---

## Using it

### The remote

Any USB remote that presents as a keyboard, the TV's own remote over CEC, or the keyboard.

```
WATCH    UP/DOWN change channel     SELECT opens the menu    BACK shows the bug
MENU     UP/DOWN move the cursor    SELECT activates         BACK goes up a level
```

Digits tune directly — `1`,`2` goes to channel 12, and a single digit settles after a second.
Channel feedback is drawn in under a millisecond and the file opens behind it, so the box feels
instant even when the media is on a NAS.

The menu opens resting on **EXIT TO TV**, so the menu button toggles: press to open, press
again to leave. Under `SETUP` are PICTURE, AUDIO, CHANNELS, NETWORK, POWER and ABOUT; `POWER`
offers Shut down, Restart, and Restart the tuner, so nobody has to pull the plug.

Long-pressing the menu button is wired to send the box to standby and turn the television off
over CEC. Verified against a replay of the captured byte stream, not yet on the sofa — CEC
itself is confirmed working, so if it misbehaves the modifier chord is the suspect, and both
halves log.

Find out what your own remote sends:

```bash
python3 -m tub3.remote watch          # press buttons, see the verbs
python3 -m tub3.cec check             # is CEC alive, and what address did we get
python3 -m tub3.cec on                # turn the television on
```

### Casting

Both work, and they are different mechanisms for different jobs.

**YouTube** — the box appears in the cast list *inside the YouTube app*, as a screen rather
than a speaker. The phone sends a video **id**, not pixels: the box fetches the video itself
and hands it to the player it already has. Nothing is mirrored, nothing is re-encoded, and the
phone can lock or leave the room. This is DIAL plus YouTube's lounge protocol, implemented in
`tub3/dial.py` and `tub3/lounge.py` with no dependencies beyond the standard library.

**AirPlay** — mirroring and audio, via `uxplay` as its own service. The answer for everything
that isn't YouTube. mpv holds DRM master until it exits, so the tuner releases the screen when
a mirroring session starts and takes it back on TEARDOWN. Any button ends a session, because a
phone that walks off the network never says goodbye.

### The web UI

`http://<box>:8008` — settings, the guide, and what's on now. It runs whether or not the tuner
does.

---

## Getting content in

### `adsplice` — a folder of anything into a folder of commercials

```bash
python3 -m adsplice.cli ingest ~/commercials --out /srv/tub3/staging/ads-cut --profile vhs
python3 -m adsplice.cli selftest
```

It never asks what you have — a folder of 400 thirty-second clips and a folder of twelve
ninety-minute tapes are the same command. And it never produces a to-do list: segments below
the confidence floor are discarded, not queued for review. Commercials are fungible and you
will have thousands; losing 15% to caution costs nothing, while an hour of human review per
hour of footage costs the project.

Four things here that no existing tool does:

- **Analog pre-crop.** VHS captures carry a head-switching noise band along the bottom edge
  that is never dark. Whole-frame black detection scored against those pixels doesn't degrade
  on analog sources — it fails silently and completely. On the same file, the `vhs` profile
  scores 1.00 precision and recall where `digital` scores 0.00.
- **The duration prior as evidence, not a filter.** Commercials are cut to :15, :30 and :60.
  Existing detectors use duration as a post-hoc reject rule at best; here it's a scoring
  signal weighted by how common each length actually is.
- **Fragment recombination.** A commercial with an internal fade trips `blackdetect` and gets
  cut in half; neither piece then lands near a canonical length, so both fail the floor and the
  whole spot is lost. Consecutive fragments that sum to a canonical length across weak
  boundaries are put back together — and refused across a strong one, because that is a real
  break between two commercials.
- **Perceptual dedupe**, which FieldStation42 lacks entirely: it rotates by lifetime play
  count, so two rips of the same spot are two catalog entries and can both land in one break.

### `normalize` — make it all sound the same

```bash
python3 -m normalize.cli run /srv/tub3/staging/ads-cut \
        --out /srv/tub3/Media/Commercials --target pi5 --no-chapters
```

Two-pass loudness, so an ad pod never jumps against a programme. Order matters: `adsplice`
cuts with `-c copy` and leaves loudness untouched, so levelling happens **after** cutting and
**before** anything is catalogued.

### Other tools

```bash
python3 -m tub3.inventory /mnt/tub3/Media/TV     # how much television do I actually have?
python3 -m tub3.manifest  /mnt/tub3/Media --tree # what is on the drive
python3 -m tub3.scaffold  --help                 # build the commercial sorting folders
python3 -m tub3.bootstrap --help                 # one folder of shows → one channel
```

---

## Working on it

Edit on a laptop, deploy over ssh:

```bash
bash packaging/deploy_to_pi.sh            # to `boobtube`
bash packaging/deploy_to_pi.sh other-host
```

Tar over ssh rather than rsync, because Raspberry Pi OS Lite doesn't ship rsync and a deploy
tool that needs installing before it can deploy is not a deploy tool. `vendor/` is excluded —
`pi_setup.sh` clones it at the pinned SHA, and copying it risks the box running a different
upstream commit than [`UPSTREAM.md`](UPSTREAM.md) claims. `.git` **is** included: it's under a
megabyte, and having history on the box means a bad change can be backed out at 9pm without a
laptop.

[`INTEGRATION.md`](INTEGRATION.md) is the file:line-level analysis of what is imported from
FieldStation42, what is driven, what is read, and what is ours — with the evidence for each
call. Read it before changing anything near the seam.

---

## Troubleshooting

**A channel shows off-air cards.** The schedule ran out. `tub3.supervisor --status`, then
`--force`.

**A channel has no blocks at all.** Usually a commercial-free station with no bumpers: those
fill gaps with a bare bump tag, and the resulting `NoFillerContentFound` is a *sibling* of
`MatchingContentNotFound`, so upstream's own handlers never catch it. Give the channel
bumpers, or a `commercials` pool.

**Wall-to-wall commercials.** A long programme in a much longer block. Padding to the nearest
`increment` is what fills the difference, so a 91-minute show on a 120-minute grid owes 29
minutes of advertising. Drop that channel's `increment` to `5`.

**Audio drops while surfing.** Should be fixed — mpv holds the HDMI device open with
`--audio-stream-silence` instead of reopening it per file. If it comes back, that's the
suspect.

**CEC does nothing.** `python3 -m tub3.cec check`. If it can't register a logical address the
cause is usually in config.txt, which is why `check_sdcard.sh --fix` exists.

**Casting connects and never plays.** Turn on tracing and watch the actual frames:

```
# /etc/systemd/system/tub3-tuner.service.d/trace.conf
[Service]
Environment=TUB3_LOUNGE_TRACE=1
```

Turn it off again afterwards — it writes session tokens and account identifiers to the journal
in full.

---

## Licence

MPL-2.0, matching FieldStation42 — file-level copyleft, so modified files stay open and
anything new stays yours.

**The software ships with zero content.** It ingests only user-supplied files. Vintage
commercials are copyrighted works; do not bundle, host, or redistribute them.
