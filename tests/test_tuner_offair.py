"""A file that will not open must put a card up, not leave the last frame there.

`Box.tune` called the player, took its latency, and never read `TuneResult.ok`. On a failed
open the tuner set the channel, drew the bug, and left whatever was already on screen — so a
missing file was indistinguishable from a programme that had frozen, indefinitely, on a box
whose whole premise is that it cannot get stuck.

Proving that on the live box turned out to be the wrong tool: twice the schedule advanced past
the file that had been hidden before the re-tune landed, once into an ad break. The clock is
the problem, so this takes the clock out. Fakes for the two collaborators `Box` already
accepts as arguments, no mpv, no database, no share.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuner.box import Box                                        # noqa: E402
from tuner.player import TuneResult                              # noqa: E402


@dataclass
class FakePlayer:
    """Records what was drawn. `ok` decides whether the file opens."""
    ok: bool = True
    overlays: dict = field(default_factory=dict)
    hidden: list = field(default_factory=list)
    tuned: list = field(default_factory=list)
    alive: bool = True

    def tune(self, path, seek, **kw):
        self.tuned.append(path)
        return TuneResult(ok=self.ok, latency_ms=12.0,
                          error=None if self.ok else "no such file")

    def show_overlay(self, text, overlay_id=1, **kw):
        self.overlays[overlay_id] = text

    def hide_overlay(self, overlay_id=1, **kw):
        self.hidden.append(overlay_id)
        self.overlays.pop(overlay_id, None)

    def clear_loop(self): pass
    def play_loop(self, clips): pass
    def show_backdrop(self, *a, **k): pass
    def get_property(self, *a, **k): return None


@dataclass
class FakeProgram:
    path: str = "/pool/shows/whatever.mkv"
    duration: float = 1800.0
    name: str = "Whatever"


@dataclass
class FakeAiring:
    program: FakeProgram = field(default_factory=FakeProgram)
    seek: float = 0.0
    remaining: float = 900.0
    off_air: bool = False
    title: str = "Whatever"
    # Everything `Box` reads off an airing, taken from `grep -oE "airing\.[a-z_]+"` rather
    # than guessed one AttributeError at a time.
    channel: int = 17
    channel_name: str = "STIFF PEAKS"
    feature_path: str = "/pool/shows/whatever.mkv"
    programme_remaining: float = 900.0


@dataclass
class FakeStation:
    number: int = 17
    name: str = "STIFF PEAKS"
    kind: str = "standard"


class FakeLineup:
    channels = []
    numbers = [17]

    def __init__(self, airing=None):
        self._airing = airing

    def get(self, channel):
        return FakeStation(number=channel)

    def now(self, channel, at):
        return self._airing

    def surf(self, *a, **k):
        return 17


def build(ok: bool, airing):
    player = FakePlayer(ok=ok)
    box = Box(FakeLineup(airing), player)
    return box, player


def card_of(player):
    return player.overlays.get(2, "")


class FailedTuneShowsTheCard(unittest.TestCase):

    def setUp(self):
        # The state file is a side effect on a real path; this test is about the picture.
        patcher = mock.patch.object(Box, "_write_state", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_file_that_will_not_open_puts_the_card_up(self):
        box, player = build(ok=False, airing=FakeAiring())
        box.tune(17)
        self.assertIn("OFF AIR", card_of(player),
                      "a failed open left whatever was on screen")
        self.assertIn("CHANNEL 17", card_of(player))

    def test_the_tuning_number_comes_down_with_it(self):
        """The card is the whole screen; the number drawn before it must not sit on top."""
        box, player = build(ok=False, airing=FakeAiring())
        box.tune(17)
        self.assertIn(3, player.hidden)

    def test_a_file_that_opens_does_not(self):
        box, player = build(ok=True, airing=FakeAiring())
        box.tune(17)
        self.assertNotIn("OFF AIR", card_of(player))
        self.assertEqual(player.tuned, ["/pool/shows/whatever.mkv"])

    def test_genuine_dead_air_still_shows_the_same_card(self):
        """One card, two causes. Two implementations would drift."""
        box, player = build(ok=True, airing=None)
        box.tune(17)
        self.assertIn("OFF AIR", card_of(player))

    def test_the_state_file_records_that_nothing_is_on(self):
        """The publisher reads this to decide whether a channel may be rebuilt."""
        written = {}
        with mock.patch.object(Box, "_write_state",
                               lambda self, ch, on_air=True: written.update(
                                   channel=ch, on_air=on_air)):
            box, _ = build(ok=False, airing=FakeAiring())
            box.tune(17)
        self.assertEqual(written, {"channel": 17, "on_air": False})


if __name__ == "__main__":
    unittest.main()
