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

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lineup.json"


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

    def test_wrapping_past_midnight(self):
        part = Daypart(*parse_span("23-6"), "x")
        self.assertEqual(part.quarters()[:4], [92, 93, 94, 95])
        self.assertEqual(part.quarters()[4:8], [0, 1, 2, 3])
        self.assertEqual(len(part.quarters()), 7 * 4)

    def test_a_span_that_ends_where_it_starts_is_the_whole_day(self):
        # Long-standing behaviour of the hour arithmetic this replaces; nothing on the dial
        # uses it, and changing it silently would be a change nobody asked for.
        part = Daypart(*parse_span("6-6"), "x")
        self.assertEqual(len(part.quarters()), QUARTERS_PER_DAY)
        self.assertEqual(part.quarters()[0], 24)

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

    `audit()` reads the compiled quarter grid, so a daypart starting at 16:45 puts its tag in
    the children's window at quarter 67 and the guard fires. Anything that judged a daypart
    by whole hours instead — and an earlier version of this did — would have to choose a
    rounding direction, and the inward choice makes the fifteen minutes before the watershed
    on a family channel simply not exist: no `problems`, no `unjudged`, nothing printed.

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

    def test_and_the_quarters_are_what_carry_it(self):
        late = Daypart(*parse_span("16:45-20"), "shows-dinner")
        from tub3.lineup import in_childrens_hours_q
        # 16:45 is quarter 67, the last one inside the children's window.
        self.assertIn(67, late.quarters())
        self.assertTrue(in_childrens_hours_q(67))
        self.assertFalse(any(in_childrens_hours_q(q) for q in late.quarters() if q > 67))


class Lattice(unittest.TestCase):
    """The finest boundary a channel can land on — a floor, never a promise.

    A block's length is `increment * ceil(content / increment)` and the next slot lookup
    happens wherever that lands, so a minute that does not divide into every increment on the
    channel can never be a mark, whatever the content. That much the editor can refuse
    outright. Whether a divisible edge *does* get a mark depends on the durations leading up
    to it, and nothing here promises it.
    """

    def fixture(self):
        from tub3.lineup import load
        return {c.number: c for c in load(FIXTURE)}

    def test_the_real_dial(self):
        from tub3.lineup import lattice
        dial = self.fixture()
        # Channel 3 runs half-hours with one 60-minute daypart for The Price Is Right, so a
        # boundary at :30 is reachable and :15 is not.
        self.assertEqual(lattice(dial[3]), 30)
        # THE PICTURES and MATINEE run two-hour films: the finest edge is an even hour.
        self.assertEqual(lattice(dial[6]), 120)
        self.assertEqual(lattice(dial[7]), 120)
        # THE GOOD LIFE runs five-minute increments, so the quarter grid is entirely reachable.
        self.assertEqual(lattice(dial[8]), 5)
        # AFTER DARK is hourly.
        self.assertEqual(lattice(dial[10]), 60)
        # THE ZONE mixes 30 with two 120-minute film dayparts; the gcd is what binds.
        self.assertEqual(lattice(dial[12]), 30)

    def test_a_channel_that_names_no_increment_takes_the_default(self):
        from tub3.lineup import DEFAULT_INCREMENT, Channel, lattice
        self.assertEqual(lattice(Channel(number=99, name="T")), DEFAULT_INCREMENT)

    def test_a_per_daypart_increment_can_only_make_it_finer(self):
        from tub3.lineup import Channel, Daypart, lattice, parse_span
        coarse = Channel(number=99, name="T", increment=60,
                         dayparts=[Daypart(*parse_span("6-10"), "a")])
        self.assertEqual(lattice(coarse), 60)
        mixed = Channel(number=99, name="T", increment=60,
                        dayparts=[Daypart(*parse_span("6-10"), "a"),
                                  Daypart(*parse_span("10-12"), "b", increment=45)])
        self.assertEqual(lattice(mixed), 15)


class Spans(unittest.TestCase):
    """A refusal names the times that actually caused it.

    The hull this replaces was `min(hours)`..`max(hours) + 1` over the union of every
    daypart sharing a tag, so a tag used twice in a day was reported as one long block.
    """

    def test_one_run(self):
        from tub3.lineup import _spans, parse_span, Daypart
        self.assertEqual(_spans(set(Daypart(*parse_span("6-10"), "x").quarters())),
                         "06:00-10:00")

    def test_two_runs_are_not_merged_into_a_hull(self):
        # Channel 16 THE WORKS runs `shows-made` at 2-7 and again at 15-18. Its daylight
        # hours are 6, 15, 16 and 17 — the hull rendered that "06:00-18:00", claiming twelve
        # hours for four.
        from tub3.lineup import _spans, after_watershed_q, load
        ch16 = [c for c in load(FIXTURE) if c.number == 16][0]
        lit = set()
        for part in ch16.dayparts:
            if part.tag != "shows-made":
                continue
            lit |= {q for q in part.quarters() if not after_watershed_q(q)}
        self.assertEqual(_spans(lit), "06:00-07:00 and 15:00-18:00")

    def test_quarter_precision_survives(self):
        from tub3.lineup import _spans, parse_span, Daypart
        self.assertEqual(_spans(set(Daypart(*parse_span("19:45-20:00"), "x").quarters())),
                         "19:45-20:00")
        self.assertEqual(_spans(set(Daypart(*parse_span("17:00-20:30"), "x").quarters())),
                         "17:00-20:30")

    def test_nothing_renders_as_nothing(self):
        from tub3.lineup import _spans
        self.assertEqual(_spans(set()), "")


class WindowEdges(unittest.TestCase):
    """The two guarded windows, asked of a quarter rather than an hour.

    Equivalent while both edges sit on the hour, and written this way so they stay correct
    if either ever moves off it — the hour version would then answer for a whole hour that
    the window only partly covers, in the one check that is not allowed to be approximate.
    """

    def test_the_watershed_covers_exactly_the_night(self):
        from tub3.lineup import (QUARTERS_PER_DAY, WATERSHED_END, WATERSHED_HOUR,
                                 after_watershed_q)
        for q in range(QUARTERS_PER_DAY):
            hour = q // 4
            self.assertEqual(after_watershed_q(q),
                             hour >= WATERSHED_HOUR or hour < WATERSHED_END, q)

    def test_the_childrens_window_likewise(self):
        from tub3.lineup import (CHILDRENS_HOURS_END, CHILDRENS_HOURS_START,
                                 QUARTERS_PER_DAY, in_childrens_hours_q)
        for q in range(QUARTERS_PER_DAY):
            self.assertEqual(in_childrens_hours_q(q),
                             CHILDRENS_HOURS_START <= q // 4 < CHILDRENS_HOURS_END, q)

    def test_the_edges_themselves(self):
        from tub3.lineup import after_watershed_q, in_childrens_hours_q
        self.assertFalse(after_watershed_q(79))   # 19:45 — the last daylight quarter
        self.assertTrue(after_watershed_q(80))    # 20:00
        self.assertTrue(after_watershed_q(23))    # 05:45, still inside the night
        self.assertFalse(after_watershed_q(24))   # 06:00
        self.assertTrue(in_childrens_hours_q(67))   # 16:45
        self.assertFalse(in_childrens_hours_q(68))  # 17:00
        self.assertTrue(in_childrens_hours_q(24))   # 06:00
        self.assertFalse(in_childrens_hours_q(23))  # 05:45


class TheWatershedSliver(unittest.TestCase):
    """A quarter of an hour of adult content before 20:00 is still before 20:00.

    The watershed is the global rule — nothing above 14A airs before it, whatever the
    channel is rated — so this is the check that a `late` channel cannot opt out of. A pool
    dragged fifteen minutes earlier than the watershed has to be refused, and the refusal has
    to say 19:45 rather than 19:00, because a person reading it needs to know which edge to
    move.

    A one-title source on purpose: `mixed` downgrades any catalogued source resolving to two
    or more titles, so only a single title can produce an actual refusal here.
    """

    CATALOGUE = {"titles": [
        {"path": "/media/TV/Californication", "content_rating": "TV-MA", "seconds": 1800},
        {"path": "/media/TV/Bluey", "content_rating": "TV-Y", "seconds": 420},
    ]}

    def channel(self, span):
        # A daytime block as well as the late one, so the day is fully covered. Without it
        # the fourteen uncovered hours fall back to the channel's first tag and the late pool
        # airs all afternoon — which is a different defect, pinned in `AnUncoveredGap` below.
        return Channel(number=10, name="AFTER DARK", rating="late", increment=60,
                       sources={"shows-day": ["/media/TV/Bluey"],
                                "shows-dark": ["/media/TV/Californication"]},
                       dayparts=[Daypart(*parse_span("6-20"), "shows-day"),
                                 Daypart(*parse_span(span), "shows-dark")])

    def refusals(self, span):
        from tub3.lineup import audit
        problems, _o, _u, _m, _i = audit([self.channel(span)], catalogue=self.CATALOGUE)
        return problems

    def test_at_the_watershed_it_passes(self):
        self.assertEqual(self.refusals("20-6"), [])
        self.assertEqual(self.refusals("20:00-06:00"), [])

    def test_a_quarter_of_an_hour_early_is_refused(self):
        hit = self.refusals("19:45-6")
        self.assertTrue(hit, "fifteen minutes of TV-MA before the watershed went unnoticed")
        self.assertIn("Californication", hit[0])
        self.assertIn("nothing above 14A may air", hit[0])

    def test_and_the_refusal_names_the_quarter_not_the_hour(self):
        # "19:00-20:00" would send someone looking for an edge that is not there.
        hit = self.refusals("19:45-6")
        self.assertIn("19:45-20:00", hit[0])
        self.assertNotIn("19:00-20:00", hit[0])

    def test_the_lattice_says_whether_that_edge_was_even_reachable(self):
        from tub3.lineup import lattice
        # AFTER DARK is hourly, so 19:45 could never have been a mark in the first place —
        # the editor refuses to draw it. The guard still has to hold for the channels where
        # it is drawable, which is why both exist.
        self.assertEqual(lattice(self.channel("19:45-6")), 60)
        self.assertNotEqual((19 * 60 + 45) % 60, 0)


class AnUncoveredGap(unittest.TestCase):
    """The guard has to judge what airs, not what the dayparts mention.

    A daypart list need not cover the day. `_quarter_grid` fills whatever it leaves with
    `channel.default_tag()` — the first daypart's tag — and for as long as `audit()` read the
    dayparts instead of the grid, those quarters were never looked at. One daypart of `20-6`
    on an adult pool put TV-MA on screen at seven in the evening and the audit returned five
    empty lists.

    Both sides derive from `_quarter_grid` now, so the guard cannot disagree with the
    schedule by construction rather than by the two of them happening to be written alike.
    """

    CATALOGUE = {"titles": [{"path": "/media/TV/Californication",
                             "content_rating": "TV-MA", "seconds": 1800}]}

    def audit(self, channel):
        from tub3.lineup import audit
        return audit([channel], catalogue=self.CATALOGUE)

    def late_only(self, span="20-6"):
        return Channel(number=10, name="AFTER DARK", rating="late", increment=60,
                       sources={"shows-dark": ["/media/TV/Californication"]},
                       dayparts=[Daypart(*parse_span(span), "shows-dark")])

    def test_the_gap_is_what_actually_airs(self):
        from tub3.lineup import _quarter_grid
        grid = _quarter_grid(self.late_only(), "monday")
        # 07:00, 12:00, 17:00, 19:00 — none of them mentioned by any daypart.
        for quarter in (28, 48, 68, 76):
            self.assertEqual(grid[quarter], "shows-dark")

    def test_and_the_guard_now_sees_it(self):
        problems, _o, _u, _m, _i = self.audit(self.late_only())
        self.assertTrue(problems, "TV-MA aired through the whole afternoon unremarked")
        self.assertIn("Californication", problems[0])
        self.assertIn("06:00-20:00", problems[0])

    def test_a_zero_length_span_is_refused_outright(self):
        # "24-0" was the one span of 9,409 whose quarters() came out empty: it painted
        # nothing, so the whole day fell back to the channel's first tag. Reading the grid
        # would now catch that anyway, but a daypart that silently means nothing is still
        # not something to accept.
        with self.assertRaises(ValueError) as caught:
            parse_span("24-0")
        self.assertIn("end of the day", str(caught.exception))

    def test_and_a_span_built_that_way_by_hand_is_still_caught(self):
        # Constructed past the parser, which is what a future editor writing quarters
        # directly could do.
        by_hand = Channel(number=10, name="AFTER DARK", rating="late", increment=60,
                          sources={"shows-dark": ["/media/TV/Californication"]},
                          dayparts=[Daypart(96, 0, "shows-dark")])
        self.assertEqual(by_hand.dayparts[0].quarters(), [])
        problems, _o, _u, _m, _i = self.audit(by_hand)
        self.assertTrue(problems, "a daypart painting nothing hid the whole channel")

    def test_a_fully_covered_day_is_unaffected(self):
        covered = Channel(number=10, name="AFTER DARK", rating="late", increment=60,
                          sources={"shows-day": ["/media/TV/Bluey"],
                                   "shows-dark": ["/media/TV/Californication"]},
                          dayparts=[Daypart(*parse_span("6-20"), "shows-day"),
                                    Daypart(*parse_span("20-6"), "shows-dark")])
        problems, _o, _u, _m, _i = self.audit(covered)
        self.assertEqual([p for p in problems if "Californication" in p], [])

    def test_a_tag_that_only_reaches_daylight_on_one_day_is_still_judged(self):
        # The check is deliberately day-agnostic: Saturday counts.
        weekend_only = Channel(number=10, name="AFTER DARK", rating="late", increment=60,
                               sources={"shows-day": ["/media/TV/Bluey"],
                                        "shows-dark": ["/media/TV/Californication"]},
                               dayparts=[Daypart(*parse_span("6-20"), "shows-day"),
                                         Daypart(*parse_span("20-6"), "shows-dark"),
                                         Daypart(*parse_span("14-16"), "shows-dark",
                                                 days=["saturday"])])
        problems, _o, _u, _m, _i = self.audit(weekend_only)
        self.assertTrue(problems)
        self.assertIn("14:00-16:00", problems[0])


class IncrementsAndValidation(unittest.TestCase):
    """`lattice()` has to describe the channel that gets compiled, not the one written down."""

    def test_the_surviving_increment_is_the_last_dayparts(self):
        from tub3.lineup import increments, lattice
        # `compile_station` writes `tag_overrides` keyed by TAG and `update`s in daypart
        # order, so two dayparts sharing a tag leave only the last one's increment. Reading
        # the dayparts reported a channel finer than the one it compiles.
        ch = Channel(number=99, name="T", increment=60,
                     sources={"a": []},
                     dayparts=[Daypart(*parse_span("6-10"), "a", increment=45),
                               Daypart(*parse_span("10-14"), "a", increment=90)])
        self.assertEqual(increments(ch)["a"], 90)
        # 90, not gcd(45, 90) == 45: the 45 never reaches the station config at all. The
        # channel's own 60 does not appear either, because every tag here is overridden and
        # a station-level increment only applies to tags that are not.
        self.assertEqual(lattice(ch), 90)

    def test_zero_means_no_lattice_at_all_not_a_fine_one(self):
        from tub3.lineup import lattice
        # Upstream's `_calc_target_duration` short-circuits on a zero multiple and returns
        # the raw duration, so marks land wherever content ends. `gcd(0, n) == n` would have
        # hidden that behind the other increments.
        ch = Channel(number=99, name="T", increment=60, sources={"a": []},
                     dayparts=[Daypart(*parse_span("6-10"), "a", increment=0)])
        self.assertEqual(lattice(ch), 0)

    def test_a_nonsense_increment_is_refused_rather_than_answered(self):
        from tub3.lineup import lattice
        for bad in (-30, 45.0, "30", True):
            ch = Channel(number=99, name="T", increment=bad, sources={"a": []})
            with self.assertRaises(ValueError, msg=repr(bad)):
                lattice(ch)

    def test_the_answer_does_not_depend_on_whether_a_daypart_exists(self):
        from tub3.lineup import lattice
        # A one-element reduce never calls gcd, so a bare channel used to skip every check
        # gcd would have applied — -30 came back as -30, and 45.0 as 45.0.
        bare = Channel(number=99, name="T", increment=30, sources={"a": []})
        with_part = Channel(number=99, name="T", increment=30, sources={"a": []},
                            dayparts=[Daypart(*parse_span("6-10"), "a")])
        self.assertEqual(lattice(bare), lattice(with_part))


class SpansReadAsASentence(unittest.TestCase):
    def test_two_runs_are_joined_with_and(self):
        from tub3.lineup import _spans
        # This goes inside "airs X at {span}, but Plex rates it Y" — a second comma there
        # makes one broken list of two separate things.
        self.assertEqual(_spans({24, 25, 26, 27, 60, 61}), "06:00-07:00 and 15:00-15:30")

    def test_three_runs_keep_the_commas_and_the_and(self):
        from tub3.lineup import _spans
        self.assertEqual(_spans({0, 1, 40, 41, 80, 81}),
                         "00:00-00:30, 10:00-10:30 and 20:00-20:30")
