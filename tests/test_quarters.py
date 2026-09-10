"""The quarter grid — the part the golden files cannot prove.

`tests/golden/` pins what fifteen hour-aligned channels compile to, so it proves this change
broke nothing. It cannot prove the change *added* anything, because there is no sub-hour
daypart on the dial to exercise. That is what this file is for.

The rounding direction in `hours()` is the load-bearing decision here. Its callers are the
rating checks in `audit()` and the settings page, and both are asking "which hours does this
touch" — so a fifteen-minute sliver of a late-night pool at 19:45 has to be judged against
hour 19, which is before the watershed. Rounding inward would drop it, silently, in the one
piece of code that stands between a child and something they should not see.

Hermetic. No share, no Plex, no box, no network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tub3.lineup import (                                        # noqa: E402
    QUARTERS_PER_DAY, Channel, Daypart, _collapse, _day_template, parse_span)


class ParseSpan(unittest.TestCase):
    def test_the_form_every_channel_is_written_in(self):
        self.assertEqual(parse_span("6-10"), (24, 40))
        self.assertEqual(parse_span("23-6"), (92, 24))
        # "20-24" has always meant the end of the day, and 96 is where that lands.
        self.assertEqual(parse_span("20-24"), (80, 96))

    def test_the_form_the_grid_editor_will_write(self):
        self.assertEqual(parse_span("17:00-20:30"), (68, 82))
        self.assertEqual(parse_span("19:45-20:00"), (79, 80))
        self.assertEqual(parse_span("00:15-24:00"), (1, 96))

    def test_the_two_forms_mix(self):
        # A person editing one boundary should not have to rewrite the other end.
        self.assertEqual(parse_span("17-20:30"), (68, 82))
        self.assertEqual(parse_span(" 6 - 10 "), (24, 40))

    def test_off_grid_is_refused_rather_than_rounded(self):
        # Rounding here would put a boundary somewhere the editor never showed him.
        for span in ("17:10-20:00", "6-10:07", "0:01-1:00"):
            with self.assertRaises(ValueError) as caught:
                parse_span(span)
            self.assertIn("quarter hour", str(caught.exception))

    def test_nonsense_is_refused_with_the_span_in_the_message(self):
        for span in ("6", "", "six-ten", "25-26", "6-10:60", "24:15-1"):
            with self.assertRaises(ValueError) as caught:
                parse_span(span)
            self.assertIn(repr(span) if span else "hours", str(caught.exception))


class Quarters(unittest.TestCase):
    def test_a_plain_range(self):
        part = Daypart(*parse_span("6-10"), "x")
        self.assertEqual(part.quarters(), list(range(24, 40)))
        self.assertEqual(part.hours(), [6, 7, 8, 9])

    def test_wrapping_past_midnight(self):
        part = Daypart(*parse_span("23-6"), "x")
        self.assertEqual(part.quarters()[:4], [92, 93, 94, 95])
        self.assertEqual(part.quarters()[4:8], [0, 1, 2, 3])
        self.assertEqual(part.hours(), [23, 0, 1, 2, 3, 4, 5])

    def test_a_span_that_ends_where_it_starts_is_the_whole_day(self):
        # Long-standing behaviour of the hour arithmetic this replaces; nothing on the dial
        # uses it, and changing it silently would be a change nobody asked for.
        part = Daypart(*parse_span("6-6"), "x")
        self.assertEqual(len(part.quarters()), QUARTERS_PER_DAY)
        self.assertEqual(part.hours(), list(range(6, 24)) + list(range(0, 6)))

    def test_hours_rounds_outward(self):
        # The sliver that must not disappear. 19:45-20:00 is before the watershed.
        self.assertEqual(Daypart(*parse_span("19:45-20:00"), "x").hours(), [19])
        self.assertEqual(Daypart(*parse_span("17:00-20:30"), "x").hours(),
                         [17, 18, 19, 20])
        self.assertEqual(Daypart(*parse_span("20:30-23:00"), "x").hours(),
                         [20, 21, 22])

    def test_hours_matches_the_arithmetic_it_replaces(self):
        # Every daypart on the dial is hour-aligned, so for all of them the derived version
        # has to agree with the old `range(start, end)` exactly — order included.
        for start in range(24):
            for end in range(24):
                old = (list(range(start, end)) if end > start
                       else list(range(start, 24)) + list(range(0, end)))
                new = Daypart(*parse_span(f"{start}-{end}"), "x").hours()
                self.assertEqual(new, old, f"{start}-{end}")

    def test_span_and_hours_text(self):
        aligned = Daypart(*parse_span("6-10"), "x")
        self.assertEqual(aligned.span(), "06:00-10:00")
        # Compact when it can be, so a file written back reads the way it was written.
        self.assertEqual(aligned.hours_text(), "6-10")
        odd = Daypart(*parse_span("17:00-20:30"), "x")
        self.assertEqual(odd.span(), "17:00-20:30")
        self.assertEqual(odd.hours_text(), "17:00-20:30")

    def test_span_matches_the_printer_it_replaces(self):
        # `main`'s listing used to format `f"{start:02d}:00-{end:02d}:00"` straight off the
        # hour fields, and nothing covers that line. Quarters made those fields meaningless —
        # printed raw, a 23-6 daypart would have read "92:00-24:00".
        for start in range(24):
            for end in range(24):
                part = Daypart(*parse_span(f"{start}-{end}"), "x")
                self.assertEqual(part.span(), f"{start:02d}:00-{end:02d}:00")

    def test_hours_text_round_trips(self):
        for span in ("6-10", "23-6", "20-24", "17:00-20:30", "19:45-20:00", "00:15-24:00"):
            part = Daypart(*parse_span(span), "x")
            again = Daypart(*parse_span(part.hours_text()), "x")
            self.assertEqual((again.start_q, again.end_q), (part.start_q, part.end_q), span)


class Collapse(unittest.TestCase):
    def test_an_unchanging_hour_is_a_bare_string(self):
        # What all fifteen channels emit today, and what keeps the golden files identical.
        self.assertEqual(_collapse(["a", "a", "a", "a"]), "a")

    def test_a_half_hour_boundary_is_a_pair(self):
        self.assertEqual(_collapse(["a", "a", "b", "b"]), ["a", "b"])

    def test_a_quarter_boundary_is_four(self):
        self.assertEqual(_collapse(["a", "b", "b", "b"]), ["a", "b", "b", "b"])
        self.assertEqual(_collapse(["a", "a", "a", "b"]), ["a", "a", "a", "b"])
        self.assertEqual(_collapse(["a", "b", "c", "d"]), ["a", "b", "c", "d"])

    def test_never_three(self):
        # Twenty-minute thirds are legal upstream and undrawable on a quarter grid.
        for quarters in [[a, b, c, d]
                         for a in "ab" for b in "ab" for c in "ab" for d in "ab"]:
            out = _collapse(quarters)
            self.assertIn(len(out) if isinstance(out, list) else 1, (1, 2, 4), quarters)


class SubHourReachesTheConfig(unittest.TestCase):
    """The whole point of the change: a boundary at :30 has to survive compilation."""

    def channel(self, *spans):
        return Channel(number=99, name="TEST", rating="late",
                       sources={t: [] for _, t in spans},
                       dayparts=[Daypart(*parse_span(s), t) for s, t in spans])

    def test_a_half_hour_boundary_emits_a_pair(self):
        slots = _day_template(self.channel(("17:00-20:30", "dinner"),
                                           ("20:30-23:00", "evening")), "monday")
        self.assertEqual(slots["19"]["tags"], "dinner")
        self.assertEqual(slots["20"]["tags"], ["dinner", "evening"])
        self.assertEqual(slots["21"]["tags"], "evening")

    def test_a_quarter_boundary_emits_four(self):
        slots = _day_template(self.channel(("6-19:45", "day"),
                                           ("19:45-23:00", "night")), "monday")
        self.assertEqual(slots["19"]["tags"], ["day", "day", "day", "night"])

    def test_an_hour_aligned_channel_still_emits_bare_strings(self):
        slots = _day_template(self.channel(("6-10", "morning"), ("10-15", "day")), "monday")
        self.assertTrue(all(isinstance(v["tags"], str) for v in slots.values()))

    def test_every_hour_of_the_day_is_present_and_filled(self):
        # An unmentioned hour is dead air, and a viewer reads dead air as a fault.
        slots = _day_template(self.channel(("6-10", "morning")), "monday")
        self.assertEqual(sorted(slots, key=int), [str(h) for h in range(24)])
        self.assertEqual(slots["3"]["tags"], "morning")

    def test_a_later_daypart_still_wins_a_shared_hour(self):
        slots = _day_template(self.channel(("10-15", "kids"), ("10-11", "price")), "monday")
        self.assertEqual(slots["10"]["tags"], "price")
        self.assertEqual(slots["11"]["tags"], "kids")

    def test_a_later_daypart_wins_only_the_quarters_it_covers(self):
        slots = _day_template(self.channel(("10-15", "kids"), ("10:30-11:00", "price")),
                              "monday")
        self.assertEqual(slots["10"]["tags"], ["kids", "price"])
        self.assertEqual(slots["11"]["tags"], "kids")


if __name__ == "__main__":
    unittest.main()


class OneDayExpander(unittest.TestCase):
    """The week page and the scheduler have to agree about which days a daypart airs.

    They did not. `tub3/web.py` carried its own expander that knew `"weekdays"` and not
    `"weekday"`, while `Daypart.applies_on` accepts both — so a daypart written the singular
    way was scheduled correctly and then vanished from the page whose whole job is to show
    what is scheduled. The page now asks the daypart.
    """

    def test_both_spellings_of_the_shorthand(self):
        for spelling in ("weekday", "weekdays"):
            part = Daypart(*parse_span("10-11"), "price", days=[spelling])
            self.assertTrue(part.applies_on("monday"), spelling)
            self.assertFalse(part.applies_on("saturday"), spelling)
        for spelling in ("weekend", "weekends"):
            part = Daypart(*parse_span("10-11"), "film", days=[spelling])
            self.assertTrue(part.applies_on("saturday"), spelling)
            self.assertFalse(part.applies_on("monday"), spelling)

    def test_the_week_page_uses_that_one_answer(self):
        import tub3.web as web
        self.assertFalse(hasattr(web, "_expand_days"),
                         "the duplicate expander is back; the page will drift again")

    def test_no_days_means_every_day(self):
        part = Daypart(*parse_span("6-10"), "x")
        self.assertTrue(all(part.applies_on(d) for d in
                            ("monday", "wednesday", "saturday", "sunday")))


class WeekGridDrawsWhatIsScheduled(unittest.TestCase):
    """`/week` paints per quarter for the same reason the compiler does.

    Painting per hour, a daypart owning only part of an hour claimed the whole of it and
    erased the one before — a boundary drawn on the page that the television does not have.
    Nothing on the dial is sub-hour today, so this is what stops the first one being a lie.
    """

    def channel(self, *spans, **kw):
        return Channel(number=99, name="TEST", rating="late",
                       sources={t: [] for _, t in spans},
                       dayparts=[Daypart(*parse_span(s), t, **kw) for s, t in spans])

    def cells(self, channel, day="monday"):
        from tub3.web import week_cells
        return week_cells(channel, day)

    def test_an_hour_aligned_channel_keeps_the_shape_it_always_had(self):
        cells = self.cells(self.channel(("6-10", "morning"), ("10-15", "day")))
        self.assertEqual(len(cells), 24)
        self.assertEqual(cells[6], {"tag": "morning", "name": "morning"})
        self.assertEqual(cells[10], {"tag": "day", "name": "day"})
        # No `quarters` key anywhere: the payload the page already knew how to draw.
        self.assertFalse(any("quarters" in c for c in cells if c))

    def test_an_uncovered_hour_is_still_empty(self):
        cells = self.cells(self.channel(("6-10", "morning")))
        self.assertIsNone(cells[3])
        self.assertIsNone(cells[10])

    def test_a_split_hour_carries_its_quarters(self):
        cells = self.cells(self.channel(("15-16:45", "afternoon"), ("16:45-20", "evening")))
        self.assertEqual(cells[15], {"tag": "afternoon", "name": "afternoon"})
        self.assertEqual(cells[16]["quarters"],
                         ["afternoon", "afternoon", "afternoon", "evening"])
        # The label is what the hour opens with, so a page that ignores `quarters` is
        # imprecise rather than wrong.
        self.assertEqual(cells[16]["tag"], "afternoon")
        self.assertEqual(cells[17], {"tag": "evening", "name": "evening"})

    def test_the_later_daypart_no_longer_eats_the_whole_hour(self):
        # The defect, stated directly: with hour painting, cells[16] read "evening" and the
        # first three quarters of the afternoon block vanished off the page.
        cells = self.cells(self.channel(("15-16:45", "afternoon"), ("16:45-20", "evening")))
        self.assertNotEqual(cells[16]["quarters"], ["evening"] * 4)

    def test_a_half_covered_hour_shows_the_gap(self):
        cells = self.cells(self.channel(("6-10:30", "morning")))
        self.assertEqual(cells[10]["quarters"], ["morning", "morning", None, None])

    def test_days_are_honoured_through_the_daypart(self):
        weekday_only = Channel(number=99, name="T", sources={"kids": [], "price": []},
                               dayparts=[Daypart(*parse_span("10-15"), "kids"),
                                         Daypart(*parse_span("10-11"), "price",
                                                 days=["weekday"])])
        self.assertEqual(self.cells(weekday_only, "monday")[10]["tag"], "price")
        self.assertEqual(self.cells(weekday_only, "saturday")[10]["tag"], "kids")


class TheSliverReachesTheGuard(unittest.TestCase):
    """The reason `hours()` rounds outward, stated as a test.

    `audit()` still reasons in whole hours — moving it to quarters is a later step — so the
    only thing carrying a sub-hour daypart into the children's-hours check is the rounding
    direction of `hours()`. Round inward and a daypart starting at 16:45 reports hours
    17, 18, 19: the fifteen minutes before the watershed on a family channel simply are not
    there, no `problems`, no `unjudged`, nothing printed. Round outward and hour 16 is in the
    set and the guard fires.

    Nothing on the dial can express this yet. That is exactly why it is pinned here — the
    golden files cannot see it, because every daypart today is hour-aligned and both
    roundings agree on all of them.
    """

    def channel(self, span):
        # `family` rating, and a tag drawing from a folder no rule marks as for children.
        return Channel(number=3, name="BOOBTUBE", rating="family",
                       sources={"shows-kids": ["/media/Kids TV/Bluey"],
                                "shows-dinner": ["/media/TV/Friends"]},
                       dayparts=[Daypart(*parse_span("6-16:45"), "shows-kids"),
                                 Daypart(*parse_span(span), "shows-dinner")])

    def refusals(self, span):
        from tub3.lineup import audit
        problems, _overrides, _unjudged, _mixed, _inert = audit([self.channel(span)])
        return [p for p in problems if "shows-dinner" in p]

    def test_after_the_childrens_hours_end_it_passes(self):
        self.assertEqual(self.refusals("17-20"), [])
        # The same span in the new syntax means the same thing.
        self.assertEqual(self.refusals("17:00-20:00"), [])

    def test_a_quarter_hour_before_it_does_not(self):
        hit = self.refusals("16:45-20")
        self.assertTrue(hit, "a 16:45 start put adult content in children's hours unnoticed")
        self.assertIn("shows-dinner", hit[0])

    def test_and_the_hour_list_is_what_carries_it(self):
        late = Daypart(*parse_span("16:45-20"), "shows-dinner")
        self.assertIn(16, late.hours())
        self.assertEqual(late.hours(), [16, 17, 18, 19])
