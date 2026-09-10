"""Plex sometimes answers with the filename, and the bug used to print it.

`describe` trusts Plex's episode title over any filename parsing, and rightly — a scene
release name is mostly codecs and group tags. But Plex hands back the file's own name when it
matched the *series* and not the episode, and 440 files in this library are in that state:
every Simpsons and every Office (US), which between them are most of channel 3's evening. The
television and the app both read "The Office (US) — The Office S06E01 Gossip".

The counts here are measured against the box's real `titles.json`, not invented.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuner.titles import unfiled                                 # noqa: E402


class Unfiled(unittest.TestCase):
    def test_a_title_carrying_its_own_marker_is_a_filename(self):
        self.assertEqual(unfiled("The Office S06E01 Gossip"), "Gossip")
        self.assertEqual(unfiled("The Simpsons S09E22 Trash of the Titans"),
                         "Trash of the Titans")

    def test_a_real_title_is_left_alone(self):
        # No marker, so nothing to recover — and "" is the caller's signal to keep Plex's
        # answer rather than replace it with a guess.
        self.assertEqual(unfiled("Oxford, 1999"), "")
        self.assertEqual(unfiled("The Sagra"), "")
        self.assertEqual(unfiled("Pregnancy Test"), "")

    def test_the_separator_style_comes_from_the_half_in_front(self):
        # Dot-separated filename: the dots after the marker are separators too.
        self.assertEqual(unfiled("Show.Name.S01E02.The.Meeting.1080p.WEBDL"), "The Meeting")
        # Spaced filename: the dots are punctuation, and flattening them gives "A A R M".
        self.assertEqual(unfiled("The Office S09E22 A.A.R.M."), "A.A.R.M.")

    def test_short_titles_survive(self):
        # `clean` strips trailing capitals and would leave nothing; `_plausible` insists on
        # two words. Both are defences against release debris and both are wrong against a
        # name a person typed.
        self.assertEqual(unfiled("The Office S07E16 PDA"), "PDA")
        self.assertEqual(unfiled("The Office S03E06 Diwali"), "Diwali")
        self.assertEqual(unfiled("The Simpsons S09E21 Girly Edition"), "Girly Edition")
        self.assertEqual(unfiled("The Simpsons S08E13 SimpsoncalifragilisticexpialaD'OHcious"),
                         "SimpsoncalifragilisticexpialaD'OHcious")

    def test_release_noise_still_goes(self):
        self.assertEqual(unfiled("Escape.to.the.Country.S23E51 - Lancashire @W4NT0Ks"),
                         "Lancashire")
        self.assertEqual(unfiled("Some.Show.S01E01.Pilot.2160p.HDR.x265"), "Pilot")

    def test_a_marker_with_nothing_after_it_recovers_nothing(self):
        # Not a filename in disguise, just a bare marker. Better to keep what Plex said.
        self.assertEqual(unfiled("The Office S06E01"), "")
        self.assertEqual(unfiled(""), "")


if __name__ == "__main__":
    unittest.main()
