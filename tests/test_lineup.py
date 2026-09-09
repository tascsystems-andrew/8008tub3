"""What the lineup compiles to, pinned.

The first test in this repository, and it exists because the channel editor is about to
change `lineup.py` underneath fifteen channels that a household watches. Everything here is
a golden comparison: the point is not that today's output is *correct* — it is that a change
which alters it has to say so out loud.

Hermetic. No share, no Plex, no box, no network. Run:  python3 -m unittest discover tests
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lineup_golden import GOLDEN, build, load, plant


class LineupGolden(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.built = build(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def golden(self, name):
        return json.loads((GOLDEN / f"{name}.json").read_text())

    def test_station_configs_are_unchanged(self):
        """What upstream actually reads, per channel."""
        self.assertEqual(self.built["confs"], self.golden("confs"))

    def test_pools_are_unchanged(self):
        """What each tag would link.

        Separate from the configs because `compile_station` never looks inside `sources`: a
        change that drops an exclude list or reorders a source list emits byte-identical
        configs and a different channel.
        """
        self.assertEqual(self.built["pools"], self.golden("pools"))

    def test_day_templates_are_unchanged(self):
        self.assertEqual(self.built["templates"], self.golden("templates"))

    def test_per_weekday_scheduling_actually_reaches_the_config(self):
        """Named, rather than left implicit in a golden blob.

        Channel 3 is the only channel using `days`, and the weekly grid the editor is being
        built around depends entirely on this working end to end. It emits two templates and
        hour ten differs between them: The Price Is Right on a weekday, kids' shows at the
        weekend.
        """
        ch3 = self.built["confs"]["3"]["station_conf"]
        self.assertEqual(sorted(ch3["day_templates"]), ["daily", "saturday"])
        self.assertEqual(ch3["day_templates"]["daily"]["10"]["tags"], "shows-price")
        self.assertEqual(ch3["day_templates"]["saturday"]["10"]["tags"], "shows-kids")
        self.assertEqual(ch3["monday"], "daily")
        self.assertEqual(ch3["saturday"], "saturday")
        self.assertEqual(ch3["sunday"], "saturday")

    def test_exclude_is_per_tag_not_per_folder(self):
        """The same folder, filtered on one channel's tag and not on another's.

        How It's Made feeds both `shows-works`, which excludes three furniture episodes, and
        `shows-made`, which excludes nothing. The decoy files the fixture plants appear in the
        second pool and not the first. Without this, an editor that moved `exclude` to the
        wrong level would look correct in every config it wrote.
        """
        works = self.built["pools"]["shows-works"]
        made = self.built["pools"]["shows-made"]
        self.assertFalse([n for n in works if "decoy" in n],
                         "shows-works excludes those episodes and must not link them")
        self.assertTrue([n for n in made if "decoy" in n],
                        "shows-made has no exclude list and must link everything")

    def test_every_channel_still_compiles(self):
        """A blunt one, kept because a schema change that throws is the loudest failure."""
        with tempfile.TemporaryDirectory() as tmp:
            channels = load(plant(Path(tmp)))
        self.assertEqual(len(channels), 15)
        self.assertEqual(sum(1 for c in channels if c.kind == "guide"), 1)


if __name__ == "__main__":
    unittest.main()
