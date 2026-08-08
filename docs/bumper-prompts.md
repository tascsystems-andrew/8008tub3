# Channel bumper prompts

Generation prompts for the per-channel station idents. Written for Grok Imagine but they'll
work in any short-form video model.

---

## The premise, so the set hangs together

BoobTube is a national public broadcaster staffed entirely by boobs. Not a scrappy pirate
station — a *self-important* one, with a budget, a mandate, and forty years of institutional
pride, run by people who are not good at this and have never once suspected it.

That gives every ident the same recipe:

> **95% straight-faced network polish. 5% visible failure. The failure is never acknowledged.**

The camera doesn't flinch. The music doesn't stop. Whatever went wrong just stays in frame,
rotating with total confidence, until the card holds. That's the whole joke, and it's what
makes nine clips read as one network instead of nine unrelated stock videos.

Per-channel, the specific wrongness is called out below as **The tell**. Don't drop it — it's
the only thing distinguishing this from a corporate sizzle reel.

---

## House style — paste this in front of every prompt

```
1990s North American cable television station identification bumper. Shot on Betacam and
mastered to VHS: interlaced motion, slight chroma bleed on saturated reds, gentle vertical
smear on highlights, visible video noise in the shadows, mild edge halo. Colour palette
anchored on deep violet and warm gold against near-black. Confident, expensive-looking
broadcast production. 16:9, locked or slow deliberate camera move. Absolutely no lettering,
no text, no words, no logos, no numbers, no captions or subtitles anywhere in the frame.
Ends holding on an uncluttered composition with clear empty space in the middle third.
```

Three notes on why it reads that way:

- **No text, stated three ways.** Video models hallucinate garbled lettering, and garbled
  lettering on a station ident is instantly fatal — it's the one element a viewer reads
  closely. The channel name and mark get composited on afterwards in the tuner's own
  overlay, where they're sharp and correct. Ask the model for a *stage*, not a *sign*.
- **Empty space in the middle third** is that landing zone. If the model fills the centre
  with detail there's nowhere to put the mark.
- **Palette.** Violet `#9644EC`, soft violet `#622CA2`, gold `#FFC83C`, ink `#0E0C14` —
  same values as the icon and the on-screen bug, so the bumpers match the furniture.

**Settings:** 5–6 seconds, 16:9, 1080p. Generate **two or three per channel** — a single
bumper on a channel you watch for an hour becomes wallpaper by the third airing.

---

## CH 3 — BOOBTUBE

The flagship. Preschool at 6am through *Always Sunny* at midnight. This is the network
identifying *itself*, so it's the most pompous of the nine — orbital, ceremonial, scored
like a moon landing.

**The tell:** the emblem assembles out of order. One segment arrives late, rotates in
upside down, and locks into place wrong. Nothing corrects it.

```
A monumental emblem assembling itself in black space from floating segments of polished
chrome and violet glass, each piece drifting in on its own arc and locking together with a
soft flare of gold light. Slow majestic orbit of the camera around the forming shape,
lens flares raking across it. Volumetric light beams, drifting particles, deep starfield
behind. One final segment arrives late and seats itself upside down and slightly proud of
the surface; the emblem keeps rotating in serene triumph regardless. Grand, ceremonial,
expensive. Holds on the completed shape centred in frame.
```

---

## CH 4 — SATURDAY AM

Cartoons, 6am to noon, sugar-forward. 1993 kids-block energy: splatter paint, skateboards,
primaries screaming over the house violet.

**The tell:** a mascot in a full costume head is visibly a bored adult. They wander through
frame, check their watch, and keep going.

```
Explosive Saturday morning kids television opener: gobs of violet and gold paint splattering
in slow motion against a bright cyan wall, a skateboard spinning end over end through frame,
confetti bursts, rubber balls bouncing, everything oversaturated and hyperactive. Whip pans
and snap zooms, jump cuts on the beat. A person in a bulky cartoon animal mascot costume
strolls through the background mid-shot, pauses to look at their wristwatch, and ambles out
of frame, entirely uninterested. Chaotic, loud, joyful. Holds on a clean painted wall with
open space at the centre.
```

---

## CH 5 — SUNNY DAYS

Mister Rogers, Daniel Tiger, Dragon Tales, Bill Nye. Commercial-free — the one channel where
the boobs, entirely by accident, made something lovely. So this ident is *sincere*. No irony.

**The tell:** a human hand reaches in, adjusts a piece of the set, and withdraws. Warm rather
than incompetent — the tell here is that it's handmade, and you can see the hands.

```
Handmade stop-motion in a sunlit kitchen: a sun cut from yellow construction paper rising
over rolling hills of green felt, cotton-wool clouds nudging across a pale blue paper sky,
tiny knitted flowers opening in the foreground. Soft morning light through a window, warm
dust motes in the air, shallow focus, visible paper fibre and fabric texture. Gentle stop
motion judder. Partway through, a real adult human hand enters frame from above, carefully
straightens one of the felt hills, and withdraws. Tender, unhurried, sincere, no irony.
Holds on the sky with clear open space.
```

---

## CH 6 — THE PICTURES

Films. Full cinema treatment — velvet, gilt, a projector beam thick with dust.

**The tell:** the curtain snags. It stops dead halfway, hangs there a beat too long, then
jerks the rest of the way open.

```
Heavy deep-violet velvet cinema curtains parting slowly on a gilded art deco proscenium
arch, a projector beam cutting through thick swirling dust above the empty seats, warm gold
light spilling across carved plasterwork. Slow push in toward the opening screen. Rich
shadows, anamorphic flares, film grain, cigarette haze in the beam. The curtain catches
partway and hangs motionless for a long beat, then jerks open the rest of the way in one
graceless movement. Opulent and faded. Holds on the bright empty screen.
```

---

## CH 7 — MATINEE

Kids' films, afternoon. Same cinema, four hours earlier and much cheaper — sunlight through
lobby glass instead of velvet dark.

**The tell:** the popcorn machine is overflowing onto the floor and nobody is dealing with it.

```
Afternoon sunlight streaming in golden shafts through the tall glass doors of a 1990s cinema
lobby, popcorn tumbling in slow motion through the light, worn patterned carpet, cardboard
standees leaning slightly, a brass rope stand. Warm dusty haze, lens flare, soft nostalgic
palette of gold and violet shadow. In the background a popcorn machine has overflowed and is
steadily spilling onto the floor in a growing pile; nobody attends to it. Gentle drifting
camera. Holds on the sunlit lobby with open space at the centre.
```

---

## CH 8 — THE GOOD LIFE

*Baking Show*, *Top Gear*, *Selling Sunset*, *Love is Blind*. Aspirational lifestyle
television, buttery and profoundly smug.

**The tell:** in the corner of a flawless infinity pool, one inflatable flamingo is slowly,
terminally deflating.

```
Luxury lifestyle television opener: buttery soft-focus glide across a marble kitchen island,
champagne rising in a coupe glass, cream linen drifting in a breeze, then a slow aerial glide
out over a mirror-still infinity pool at golden hour above a city. Everything gleaming,
shallow depth of field, warm bloom on the highlights, gold and violet sunset. Insufferably
tasteful. In one corner of the pool a single inflatable pink flamingo is slowly deflating and
folding over on itself throughout the shot. Serene, smug, unhurried. Holds on the pool and
sky with clear open space.
```

---

## CH 9 — FAR AFIELD

Bourdain, *Long Way Round*, *Departures*, *Grand Tour*, F1. Travel and engines.

**The tell:** the route line drawn across the map runs out into open ocean and simply stops.

```
Travel documentary opener: a battered passport flipping open to stamped pages, a mechanical
airport departure board clattering through its flaps, then a lone motorcycle riding away
from camera into rolling dust on an empty desert road at golden hour, long shadows, heat
shimmer. Cut to a slowly spinning antique globe with a glowing gold route line drawing itself
across the continents. Warm dust, film grain, sun flare, deep violet dusk sky. The drawn route
runs off the edge of the land, continues a short way into open ocean, and stops dead in the
middle of nothing. Adventurous, cinematic. Holds on the globe with open space beside it.
```

---

## CH 10 — AFTER DARK

*Sopranos*, *Breaking Bad*, *Mad Men*, *Severance*, *X-Files*. Prestige drama, late.

**The tell:** one fluorescent tube stutters and never settles. Perfect for this shelf — it's
half Lumon corridor, half every FBI basement in television.

```
Late night prestige drama opener: rain running down dark window glass, hard venetian blind
shadows striping an empty room, a cigarette smouldering in a glass ashtray, then a slow
steady push down a long empty corridor lit by cold overhead fluorescents. Neon reflected in
wet asphalt outside. Deep shadow, teal and violet, high contrast, heavy film grain, anamorphic
flares. One fluorescent tube partway down the corridor stutters and flickers arrhythmically
for the whole shot and never settles. Ominous, controlled, unhurried. Holds at the end of the
corridor with clear dark space in the centre.
```

---

## CH 11 — LAST LAUGH

*Chappelle*, *Python*, *Little Britain*, *Eastbound*. Comedy, late, empty room.

**The tell:** the spotlight is aimed slightly off the microphone. It drifts, corrects the
wrong way, and settles somewhere else entirely wrong.

```
An empty comedy club at one in the morning: a single vintage microphone on a chrome stand
before a worn exposed brick wall, dusty red curtain to one side, upturned chairs on tables in
the darkened background, cigarette smoke curling through a hard spotlight beam. Slow push in
on the microphone. Deep shadow, warm gold key light, violet rim, heavy grain, dust in the
beam. The spotlight sits slightly off the microphone; it drifts as if being corrected,
overshoots the other way, and settles somewhere else wrong entirely, leaving the microphone in
half-shadow. Nobody adjusts it. Holds on the stage.
```

---

## CH 2 — GUIDE

Skip it. The guide is a scrolling text grid over instrumental music — a bumper has nowhere to
play, and anything moving behind the grid hurts legibility. What channel 2 could use instead
is on the extras list below.

---

## Extras worth having

Cheap to generate, disproportionately high value — these are what make the box feel like a
station rather than a playlist.

**Technical difficulties.** For when a file fails to open. Right now that's a black screen,
which reads as broken hardware; a card reads as *character*.

```
1990s television technical difficulties card: a static studio shot of an empty broadcast
control desk with rows of small monitors all showing colour static, one office chair slowly
rotating on its own, harsh fluorescent lighting, institutional grey and violet. Completely
still camera, no cuts. Heavy VHS artefacts, tracking noise across the bottom of the frame.
Nobody present. No text or lettering anywhere. Holds with clear empty space at the centre.
```

**Sign-off.** For a scheduled overnight dead hour, if we ever want one.

```
1990s television station sign-off: a slow aerial drift over a sleeping city at four in the
morning, scattered lights, deep violet pre-dawn sky, low cloud lit gold from beneath.
Grainy telecine, gentle gate weave, warm haze. Melancholy and still. No text or lettering.
Holds on the horizon with open space above it.
```

**Up next / we'll be right back.** Sits at the end of an ad pod, before the show resumes.

```
Empty 1990s television studio between takes: a lone camera on a pedestal facing an unlit
news desk, cables coiled on the floor, one work light casting a long hard shadow, dust
hanging in still air. Violet and gold gels on the back wall, deliberately unglamorous.
Very slow dolly. Video noise, slight chroma bleed. No people. No text or lettering.
Holds on the empty desk.
```

---

## Delivery

- **MP4, H.264, 1920×1080, 5–6 s.** Whatever Grok gives is fine — I'll normalise everything.
- **Naming:** `ch04-a.mp4`, `ch04-b.mp4`, `ch03-a.mp4`, `extra-difficulties-a.mp4`. Channel
  number zero-padded, letter for the variant.
- **Drop them anywhere** — a folder on the NAS or the Mac, your call. I'll build the
  per-station `bump` pools from there, so the same clip can serve more than one channel
  without a second copy.
- **Audio:** if the clips come with sound, don't level them by hand. Bumper audio generated
  independently of the library will be far hotter than a 1997 TV rip, and the jump is
  jarring. I'll loudness-match everything to the programme material on the way in.
- **Placement:** at block boundaries with a cooldown, not on every ad pod. A bumper you see
  four times an hour stops being a bumper and becomes a nuisance — the same reason the
  channel name now only appears on a channel change.
