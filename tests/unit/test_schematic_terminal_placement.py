from __future__ import annotations
import unittest
from backend.schematic.kicad_geometry import text_width_mm
from backend.schematic.presentation_geometry import Bounds
from backend.schematic.route_simplification import connects_or_overlaps
from backend.schematic.terminal_placement import (
    EndSymbol, LabelEnd, can_attach_directly, choose_rail_symbol, choose_safe_terminal, power_symbol_footprint, hung_from_pin,
    rail_obstructions, _runs_through, _segments_intersect, _touches_bbox,
)


class SafeTerminalPlacementTests(unittest.TestCase):
    def test_two_collinear_foreign_flags_never_get_joined(self):
        first=(22.86,20.32)
        second=(22.86,31.75)
        plan=choose_safe_terminal(
            source=first,
            preferred_normal=(0,1),
            own_bounds=Bounds(21.59,18.0,24.13,21.0),
            foreign_bounds=(Bounds(21.59,30.48,24.13,33.02),),
            foreign_pin_points=(second,),
            existing_wire_segments=(((22.86,31.75),(22.86,52.07)),),
            grid_mm=1.27,clearance_mm=3.81,
        )
        self.assertFalse(any(
            _touches_bbox(seg, Bounds(second[0]-.635,second[1]-.635,
                                       second[0]+.635,second[1]+.635),0)
            for seg in plan.segments
        ))
        self.assertFalse(plan.strategy.startswith("fallback"))
        self.assertGreater(plan.length_mm,0)

    def test_direct_exit_when_route_is_clear(self):
        plan=choose_safe_terminal(
            source=(10.16,10.16),preferred_normal=(1,0),
            own_bounds=Bounds(7.62,7.62,10.16,12.70),
            foreign_bounds=(),grid_mm=1.27,clearance_mm=2.54,
        )
        self.assertEqual(plan.bends,0)
        self.assertGreater(plan.position[0],10.16)

    def test_safe_zero_wire_attachment_only_when_pin_is_clear(self):
        self.assertTrue(can_attach_directly(
            source=(10.16,10.16),foreign_bounds=(),
        ))
        self.assertFalse(can_attach_directly(
            source=(10.16,10.16),foreign_bounds=(),
            foreign_pin_points=((10.16,10.16),),
        ))
        self.assertFalse(can_attach_directly(
            source=(10.16,10.16),foreign_bounds=(),
            existing_wire_segments=(((7.62,10.16),(12.7,10.16)),),
        ))
        self.assertFalse(can_attach_directly(
            source=(10.16,10.16),
            foreign_bounds=(Bounds(7.62,7.62,12.7,12.7),),
        ))

    def test_unroutable_terminal_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError,"No unobstructed"):
            choose_safe_terminal(
                source=(10.16,10.16),preferred_normal=(1,0),
                own_bounds=None,
                foreign_bounds=(Bounds(-40,-40,80,80),),
                grid_mm=1.27,clearance_mm=2.54,
            )

    def test_existing_wire_is_not_reused_by_unrelated_terminal(self):
        plan=choose_safe_terminal(
            source=(10.16,10.16),preferred_normal=(1,0),
            own_bounds=Bounds(8.89,8.89,10.16,11.43),
            foreign_bounds=(),
            existing_wire_segments=(((12.7,10.16),(25.4,10.16)),),
            grid_mm=1.27,clearance_mm=2.54,
        )
        self.assertFalse(any(
            # Overlap with existing horizontal run is unsafe, but a legal
            # perpendicular detour or opposite-side terminal is available.
            seg[0][1]==seg[1][1]==10.16
            and max(seg[0][0],seg[1][0])>=12.7
            for seg in plan.segments
        ))


class OffGridSourceTests(unittest.TestCase):
    def test_float_noise_in_the_source_coordinates_does_not_abort_planning(self):
        # Pin positions come out of rotation maths with noise in the last digits; a candidate path must stay
        # orthogonal anyway instead of raising for the whole terminal.
        noisy = (152.39999999999998, 156.21000000000004)
        plan = choose_safe_terminal(
            source=noisy, preferred_normal=(0, 1), own_bounds=Bounds(150.0, 150.0, 154.8, 156.2),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54,
        )
        for (ax, ay), (bx, by) in plan.segments:
            self.assertTrue(abs(ax - bx) < 1e-6 or abs(ay - by) < 1e-6)

    def test_every_candidate_of_an_off_grid_source_is_orthogonal(self):
        plan = choose_safe_terminal(
            source=(10.3, 10.3), preferred_normal=(1, 0), own_bounds=Bounds(5.0, 5.0, 10.3, 15.0),
            foreign_bounds=(Bounds(11.0, 5.0, 40.0, 40.0),), grid_mm=1.27, clearance_mm=2.54,
        )
        self.assertGreater(plan.length_mm, 0)


class CrossingTests(unittest.TestCase):
    OWN = Bounds(7.62, -5.0, 12.7, 10.16)

    def plan(self, wires, foreign=()):
        return choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(0, 1), own_bounds=self.OWN, foreign_bounds=tuple(foreign),
            existing_wire_segments=tuple(wires), grid_mm=1.27, clearance_mm=2.54,
        )

    def test_a_stub_that_can_avoid_a_wire_does_not_cross_it(self):
        across = ((2.54, 15.24), (17.78, 15.24))          # a horizontal wire under the pin
        plan = self.plan((across,))
        for segment in plan.segments:
            self.assertFalse(connects_or_overlaps(segment, across))
            self.assertFalse(_segments_cross(segment, across), plan.strategy)

    def test_a_crossing_is_allowed_when_it_is_the_only_way_out(self):
        # Walls of foreign bodies on both sides and above leave only the way down, across the wire.
        walls = (Bounds(-30, -30, 6.0, 40), Bounds(14.3, -30, 50, 40))
        across = ((0.0, 12.7), (20.0, 12.7))               # just below the pin tip: any way down crosses it
        plan = self.plan((across,), foreign=walls)
        self.assertTrue(any(_segments_cross(segment, across) for segment in plan.segments))

    def test_touching_or_overlapping_a_wire_is_still_refused(self):
        along = ((10.16, 12.7), (10.16, 30.0))             # runs on the line the stub would take
        plan = self.plan((along,))
        for segment in plan.segments:
            self.assertFalse(connects_or_overlaps(segment, along), plan.strategy)


def _segments_cross(first, second) -> bool:
    """Both interiors meet: a proper crossing of two orthogonal segments."""
    (ax, ay), (bx, by) = first
    (cx, cy), (dx, dy) = second
    if abs(ax - bx) < 1e-9 and abs(cy - dy) < 1e-9:
        return min(cx, dx) < ax < max(cx, dx) and min(ay, by) < cy < max(ay, by)
    if abs(ay - by) < 1e-9 and abs(cx - dx) < 1e-9:
        return min(ax, bx) < cx < max(ax, bx) and min(cy, dy) < ay < max(cy, dy)
    return False


class EndSymbolFootprintTests(unittest.TestCase):
    """The planner keeps the symbol at the end of a stub, and text, clear of everything else."""

    GROUND = EndSymbol(half_width_mm=1.27, reach_mm=5.72, direction=1)      # reaches down the page
    SUPPLY = EndSymbol(half_width_mm=1.65, reach_mm=5.72, direction=-1)     # reaches up
    BODY_BELOW = Bounds(7.62, 10.16, 12.7, 25.0)                            # a pin that points up the page

    def plan(self, end_symbol, normal=(0, 1), foreign=(), avoid=(), own=Bounds(7.62, -5.0, 12.7, 10.16)):
        return choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=normal, own_bounds=own,
            foreign_bounds=tuple(foreign), grid_mm=1.27, clearance_mm=2.54,
            end_symbol=end_symbol, avoid_boxes=tuple(avoid),
        )

    def test_power_symbol_footprints_reach_away_from_their_pin(self):
        ground = power_symbol_footprint("GND", pin_normal_y=-1.0)
        supply = power_symbol_footprint("+3V3", pin_normal_y=1.0)
        self.assertEqual((ground.direction, supply.direction), (1, -1))
        # The text is as wide as KiCad draws it (about 3.0 mm for "GND" at 1.27 mm): two ground symbols a graphic
        # apart would otherwise put their text on each other, as the regulator's did.
        self.assertAlmostEqual(ground.half_width_mm, text_width_mm("GND", 1.27) / 2)
        self.assertAlmostEqual(ground.half_width_mm, 1.5, delta=0.1)
        self.assertAlmostEqual(supply.half_width_mm, text_width_mm("+3V3", 1.27) / 2)
        self.assertEqual(ground.footprint_at((10.0, 20.0)).bottom, 25.72)
        self.assertEqual(supply.footprint_at((10.0, 20.0)).top, 20.0 - 5.72)

    def test_a_ground_symbol_is_not_hung_over_the_stub_that_feeds_it(self):
        # The pin points up the page, so a straight stub would arrive from the side the ground symbol hangs on.
        plan = self.plan(self.GROUND, normal=(0, -1), own=self.BODY_BELOW)
        footprint = self.GROUND.footprint_at(plan.position)
        self.assertGreaterEqual(plan.bends, 1)
        for segment in plan.segments:
            self.assertFalse(_runs_through(segment, footprint), (plan.strategy, segment))

    def test_the_same_pin_takes_a_straight_stub_when_the_symbol_reaches_the_other_way(self):
        plan = self.plan(self.SUPPLY, normal=(0, -1), own=self.BODY_BELOW)
        self.assertEqual(plan.bends, 0)
        self.assertLess(plan.position[1], 10.16)

    def test_the_end_symbol_keeps_clear_of_other_symbols_and_text(self):
        free = self.plan(self.GROUND)
        text_where_it_would_land = Bounds(
            free.position[0] - 1.0, free.position[1] + 3.0, free.position[0] + 1.0, free.position[1] + 4.0,
        )
        moved = self.plan(self.GROUND, avoid=(text_where_it_would_land,))
        self.assertNotEqual(moved.position, free.position)
        self.assertFalse(self.GROUND.footprint_at(moved.position).overlaps(text_where_it_would_land))
        body_in_the_way = Bounds(free.position[0] - 3, free.position[1] + 2, free.position[0] + 3, free.position[1] + 8)
        again = self.plan(self.GROUND, foreign=(body_in_the_way,))
        self.assertFalse(self.GROUND.footprint_at(again.position).overlaps(body_in_the_way))

    def test_a_stub_does_not_run_through_another_terminals_symbol(self):
        free = self.plan(self.GROUND)
        symbol_on_the_stub = Bounds(9.9, free.segments[0][1][1] - 0.6, 10.4, free.segments[0][1][1] - 0.2)
        plan = self.plan(self.GROUND, avoid=(symbol_on_the_stub,))
        for segment in plan.segments:
            self.assertFalse(_touches_bbox(segment, symbol_on_the_stub, 0.0), plan.strategy)

    def soft_plan(self, soft, end=None):
        return choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(0, 1), own_bounds=Bounds(7.62, -5.0, 12.7, 10.16),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54,
            end_symbol=end or self.GROUND, soft_boxes=tuple(soft),
        )

    def last_heading(self, plan):
        (ax, ay), (bx, by) = plan.segments[-1]
        return ((bx > ax) - (bx < ax), (by > ay) - (by < ay))

    def test_a_ground_stub_enters_its_symbol_from_above_even_with_text_on_the_way(self):
        # The text is a symbol's name, which the field placement moves; arriving at a ground symbol from the side is
        # what stays.
        free = self.soft_plan(())
        text = Bounds(9.9, free.segments[0][1][1] - 0.6, 10.4, free.segments[0][1][1] - 0.2)
        plan = self.soft_plan((text,))
        self.assertEqual(self.last_heading(free), (0, 1))
        self.assertEqual(self.last_heading(plan), (0, 1))

    def test_a_stub_from_a_sideways_pin_turns_to_enter_a_ground_symbol_from_above(self):
        plan = choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(1, 0), own_bounds=Bounds(2.54, 5.08, 10.16, 15.24),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54, end_symbol=self.GROUND,
        )
        self.assertEqual(self.last_heading(plan), (0, 1))
        self.assertGreaterEqual(plan.bends, 1)

    CONNECTOR = Bounds(2.54, 5.08, 7.62, 12.7)            # a connector whose pins point up: tips at y = 5.08

    def up_pin(self, walls):
        return choose_safe_terminal(
            source=(5.08, 5.08), preferred_normal=(0, -1), own_bounds=self.CONNECTOR,
            foreign_bounds=walls, grid_mm=1.27, clearance_mm=2.54, end_symbol=self.GROUND,
        )

    def test_a_stub_never_runs_back_through_the_part_it_leaves(self):
        # A ground symbol wants a wire arriving from above. With room only above the pin and nothing beside it, the one
        # way to arrive along the symbol's axis is straight back down through the body: the planner has to refuse that
        # and say the pin has no exit, rather than draw it (the regulator's connector, `auto-v19`).
        enclosed = (
            Bounds(-30.0, -30.0, 40.0, 3.0), Bounds(-30.0, -30.0, 3.5, 5.0), Bounds(6.7, -30.0, 40.0, 5.0),
        )
        with self.assertRaises(ValueError) as raised:
            self.up_pin(enclosed)
        self.assertIn("stub through its own symbol", str(raised.exception))

    def test_with_room_beside_the_pin_the_stub_turns_and_does_not_go_through_the_part(self):
        plan = self.up_pin((Bounds(-30.0, -30.0, 40.0, 3.0),))
        for segment in plan.segments:
            self.assertFalse(_runs_through(segment, Bounds(2.84, 5.38, 7.32, 12.4)), plan.strategy)

    def test_and_a_supply_symbol_from_below(self):
        supply = power_symbol_footprint("+3V3", 1.0)
        plan = choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(1, 0), own_bounds=Bounds(2.54, 5.08, 10.16, 15.24),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54, end_symbol=supply,
        )
        self.assertEqual(self.last_heading(plan), (0, -1))

    def test_when_nothing_turns_it_a_stub_may_still_arrive_sideways(self):
        # a wall of wire above and below the stub's way leaves only the straight run
        walls = (((11.43, -20.0), (11.43, 5.0)), ((11.43, 15.0), (11.43, 40.0)))
        plan = choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(1, 0), own_bounds=Bounds(2.54, 5.08, 10.16, 15.24),
            foreign_bounds=(Bounds(11.0, -20.0, 40.0, 5.0), Bounds(11.0, 15.0, 40.0, 40.0)),
            grid_mm=1.27, clearance_mm=2.54, end_symbol=self.GROUND, existing_wire_segments=walls,
        )
        self.assertEqual(self.last_heading(plan), (1, 0))

    def test_text_over_the_pin_itself_does_not_leave_the_pin_without_a_terminal(self):
        # The host's own value text is drawn over its pin tip, so every stub from it touches that text.
        over_the_tip = Bounds(7.0, 9.8, 13.3, 11.4)
        plan = self.soft_plan((over_the_tip,))
        self.assertGreater(plan.length_mm, 0)
        # ...but the symbol at the end of the stub still keeps off it.
        self.assertFalse(self.GROUND.footprint_at(plan.position).overlaps(over_the_tip))

    def test_the_end_symbol_may_not_sit_on_soft_text(self):
        free = self.soft_plan(())
        text_under_it = self.GROUND.footprint_at(free.position)
        moved = self.soft_plan((text_under_it,))
        self.assertFalse(self.GROUND.footprint_at(moved.position).overlaps(text_under_it))

    def test_without_an_end_symbol_the_planner_behaves_as_before(self):
        a = choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(0, 1), own_bounds=Bounds(7.62, -5.0, 12.7, 10.16),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54,
        )
        b = choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(0, 1), own_bounds=Bounds(7.62, -5.0, 12.7, 10.16),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54,
            end_symbol=None, avoid_boxes=(Bounds(0, 0, 1, 1),),     # boxes only matter with an end symbol
        )
        self.assertEqual(a, b)

    def test_no_room_for_the_symbol_is_an_explicit_failure(self):
        everything = Bounds(-60, -60, 90, 90)
        with self.assertRaisesRegex(ValueError, "No unobstructed"):
            self.plan(self.GROUND, avoid=(everything,), own=None)

    def test_a_terminal_with_no_exit_says_what_turned_its_candidates_away(self):
        everything = Bounds(-60, -60, 90, 90)
        with self.assertRaises(ValueError) as caught:
            self.plan(self.GROUND, avoid=(everything,), own=None)
        message = str(caught.exception)
        self.assertIn("turned away:", message)
        self.assertIn("end symbol on text", message)
        # ...and the located part of the message is untouched, so failures are still attributed to the pin.
        self.assertTrue(message.startswith("No unobstructed power terminal exit from"))


class LabelEndTests(unittest.TestCase):
    OWN = Bounds(7.62, -5.0, 12.7, 10.16)

    def plan(self, end, normal=(0, 1), foreign=(), avoid=(), own=OWN):
        return choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=normal, own_bounds=own, foreign_bounds=tuple(foreign),
            grid_mm=1.27, clearance_mm=2.54, end_symbol=end, avoid_boxes=tuple(avoid),
        )

    def test_a_local_label_is_centred_above_its_anchor_and_a_global_label_runs_away_from_it(self):
        local = LabelEnd(text_width_mm=8.0, size_mm=1.0)
        flagged = LabelEnd(text_width_mm=8.0, size_mm=1.0, flagged=True)
        left, top, right, bottom = (lambda b: (b.left, b.top, b.right, b.bottom))(local.footprint_at((10, 20), (1, 0)))
        self.assertAlmostEqual(left, 6.0)
        self.assertAlmostEqual(right, 14.0)
        self.assertAlmostEqual(top, 20 - 0.82)         # the text rises above the wire...
        self.assertAlmostEqual(bottom, 20 + 0.18)      # ...and its descenders barely drop below it
        vertical = local.footprint_at((10, 20), (0, 1))
        self.assertAlmostEqual(vertical.left, 10 - 0.82)    # a rotated label's text is to the left of its wire
        self.assertAlmostEqual(vertical.top, 16.0)
        self.assertAlmostEqual(vertical.bottom, 24.0)
        east = flagged.footprint_at((10, 20), (1, 0))
        self.assertAlmostEqual(east.left, 10.0)
        self.assertAlmostEqual(east.right, 10.0 + 8.0 + 1.5)
        west = flagged.footprint_at((10, 20), (-1, 0))
        self.assertAlmostEqual(west.right, 10.0)
        north = flagged.footprint_at((10, 20), (0, -1))
        self.assertAlmostEqual(north.bottom, 20.0)
        self.assertAlmostEqual(north.top, 20.0 - 9.5)

    def test_a_straight_stub_may_end_in_its_own_label(self):
        # a pin pointing right: the stub is horizontal, so its label is too, and it needs no turn
        plan = self.plan(LabelEnd(text_width_mm=6.0, size_mm=1.5), normal=(1, 0), own=Bounds(-5.0, 7.62, 10.16, 12.7))
        self.assertEqual(plan.bends, 0)

    def test_the_label_keeps_clear_of_other_symbols_and_text(self):
        label = LabelEnd(text_width_mm=6.0, size_mm=1.5)
        free = self.plan(label)
        heading_down = (0, 1)
        body_under_it = Bounds(free.position[0] - 2, free.position[1] + 0.5, free.position[0] + 2, free.position[1] + 3)
        moved = self.plan(label, foreign=(body_under_it,))
        self.assertFalse(label.footprint_at(moved.position, heading_down).overlaps(body_under_it))
        self.assertNotEqual(moved.position, free.position)

    def test_a_label_cannot_sit_on_text(self):
        label = LabelEnd(text_width_mm=6.0, size_mm=1.5)
        free = self.plan(label)
        text = label.footprint_at(free.position, (0, 1))
        moved = self.plan(label, avoid=(text,))
        self.assertFalse(label.footprint_at(moved.position, (0, 1)).overlaps(text))


class EndSymbolOnAWireTests(unittest.TestCase):
    """A label or a power symbol laid over a wire drawn earlier reads as struck through (`linear_regulator`: the second
    VIN label stood on the stub of the first, whose pin was planned a step before)."""

    LABEL = LabelEnd(text_width_mm=7.0, size_mm=1.524)

    def plan(self, wires=()):
        return choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(1, 0), own_bounds=Bounds(2.54, 5.08, 10.16, 15.24),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54, end_symbol=self.LABEL, existing_wire_segments=wires,
        )

    def footprint(self, plan):
        (ax, ay), (bx, by) = plan.segments[-1]
        return self.LABEL.footprint_at(plan.position, ((bx > ax) - (bx < ax), (by > ay) - (by < ay)))

    def wire_through(self, footprint):
        # level with the middle of the text, well clear of the stub that feeds it
        y = (footprint.top + footprint.bottom) / 2.0
        return ((footprint.left - 4.0, y), (footprint.right + 4.0, y))

    def test_a_label_keeps_off_a_wire_that_is_already_drawn(self):
        free = self.plan()
        wire = self.wire_through(self.footprint(free))
        self.assertTrue(_runs_through(wire, self.footprint(free)), "the wire has to be in the way of the free plan")
        moved = self.plan((wire,))
        self.assertFalse(_runs_through(wire, self.footprint(moved)), (moved.strategy, moved.segments))

    def test_a_wire_beside_the_label_changes_nothing(self):
        free = self.plan()
        box = self.footprint(free)
        beside = ((box.left - 4.0, box.bottom + 3.0), (box.right + 4.0, box.bottom + 3.0))
        self.assertEqual(self.plan((beside,)).segments, free.segments)

    def test_with_no_other_place_the_label_still_goes_where_it_must(self):
        # a wall of wire across every way out of the pin: the cost is a cost, not a refusal
        walls = tuple(((x, -30.0), (x, 50.0)) for x in (12.7, 15.24, 17.78, 20.32, 22.86, 25.4, 27.94, 30.48))
        plan = choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=(1, 0), own_bounds=Bounds(2.54, 5.08, 10.16, 15.24),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=0.0, end_symbol=LabelEnd(text_width_mm=2.0, size_mm=1.524),
            existing_wire_segments=walls[:1],
        )
        self.assertGreater(plan.length_mm, 0)


class SegmentIntersectionTests(unittest.TestCase):
    def test_a_horizontal_and_a_vertical_segment_meet_only_where_their_ranges_overlap(self):
        horizontal = ((10.0, 0.0), (20.0, 0.0))
        self.assertTrue(_segments_intersect(horizontal, ((15.0, -5.0), (15.0, 5.0))))
        self.assertTrue(_segments_intersect(horizontal, ((20.0, 0.0), (20.0, 5.0))))
        # beside the segment, past its end: no meeting, whichever segment comes first
        self.assertFalse(_segments_intersect(horizontal, ((40.0, -5.0), (40.0, 5.0))))
        self.assertFalse(_segments_intersect(((40.0, -5.0), (40.0, 5.0)), horizontal))
        self.assertFalse(_segments_intersect(horizontal, ((15.0, 3.0), (15.0, 9.0))))


class NeighbourExitTests(unittest.TestCase):
    """A stub that has to turn goes the way that does not cut across the pin beside it."""

    LABEL = LabelEnd(text_width_mm=7.0, size_mm=1.524)

    def plan(self, neighbour_x):
        # two pins on the top edge of a symbol, pointing up; the one at x=10.16 is planned first
        return choose_safe_terminal(
            source=(10.16, 20.32), preferred_normal=(0, -1), own_bounds=Bounds(2.54, 20.32, 12.7, 30.48),
            foreign_bounds=(), foreign_pin_points=((neighbour_x, 20.32),), grid_mm=1.27, clearance_mm=2.54,
            end_symbol=self.LABEL,
        )

    def lateral_heading(self, plan):
        (ax, _ay), (bx, _by) = plan.segments[-1]
        return (bx > ax) - (bx < ax)

    def test_the_stub_turns_away_from_the_pin_beside_it(self):
        toward_left = self.plan(neighbour_x=7.62)       # a pin to the left: turn right
        self.assertEqual(self.lateral_heading(toward_left), 1)
        toward_right = self.plan(neighbour_x=12.7)      # a pin to the right: turn left
        self.assertEqual(self.lateral_heading(toward_right), -1)

    def test_a_pin_on_another_edge_does_not_count(self):
        # a pin far above the source is not beside it, so the usual tie-break stands
        plan = choose_safe_terminal(
            source=(10.16, 20.32), preferred_normal=(0, -1), own_bounds=Bounds(2.54, 20.32, 12.7, 30.48),
            foreign_bounds=(), foreign_pin_points=((7.62, 5.08),), grid_mm=1.27, clearance_mm=2.54,
            end_symbol=self.LABEL,
        )
        self.assertEqual(self.lateral_heading(plan), -1)


class RailObstructionTests(unittest.TestCase):
    """A shared rail is only worth drawing when it does not cut across what is already on the sheet."""

    PINS = ((10.16, 0.0), (25.4, 0.0))

    def test_a_rail_with_nothing_in_its_way_is_clear(self):
        found = rail_obstructions(horizontal=True, rail=-10.16, pins=self.PINS)
        self.assertEqual(found, {})

    def test_a_rail_that_crosses_a_wire_already_drawn_is_refused(self):
        # a supply wire from a pin on the sheet, running up between the two pins of the bank
        supply = ((17.78, 20.32), (17.78, -30.48))
        found = rail_obstructions(horizontal=True, rail=-10.16, pins=self.PINS, existing_wire_segments=(supply,))
        self.assertEqual(found, {"rail meets a wire": 1})

    def test_a_wire_beside_the_rail_is_not_in_its_way(self):
        beside = ((40.64, 20.32), (40.64, -30.48))
        found = rail_obstructions(horizontal=True, rail=-10.16, pins=self.PINS, existing_wire_segments=(beside,))
        self.assertEqual(found, {})

    def test_a_vertical_rail_is_checked_the_same_way(self):
        pins = ((0.0, 10.16), (0.0, 25.4))
        across = ((-10.16, 17.78), (20.32, 17.78))
        found = rail_obstructions(horizontal=False, rail=-5.08, pins=pins, existing_wire_segments=(across,))
        self.assertEqual(found, {"rail meets a wire": 1})

    def test_a_rail_through_a_symbol_or_a_pin_is_refused(self):
        body = Bounds(15.24, -12.7, 20.32, -7.62)
        found = rail_obstructions(horizontal=True, rail=-10.16, pins=self.PINS, foreign_bounds=(body,))
        self.assertEqual(found, {"rail runs through a symbol": 1})
        found = rail_obstructions(
            horizontal=True, rail=-10.16, pins=self.PINS, foreign_pin_points=((17.78, -10.16), self.PINS[0]),
        )
        self.assertEqual(found, {"rail runs through a pin": 1})


class LabelOrientationTests(unittest.TestCase):
    """A label at the end of a vertical stub is drawn vertically; a reader expects it horizontal."""

    LABEL = LabelEnd(text_width_mm=7.0, size_mm=1.524)

    def plan(self, end, normal):
        # a pin on the top of a symbol, pointing up (or on its right, pointing right), with open space round it
        return choose_safe_terminal(
            source=(10.16, 10.16), preferred_normal=normal, own_bounds=Bounds(5.08, 10.16, 15.24, 20.32),
            foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54, end_symbol=end,
        )

    def test_a_pin_pointing_up_gets_a_stub_that_ends_horizontally(self):
        plan = self.plan(self.LABEL, (0, -1))
        (ax, ay), (bx, by) = plan.segments[-1]
        self.assertGreater(abs(bx - ax), abs(by - ay), plan.strategy)
        self.assertGreaterEqual(plan.bends, 1)

    def test_a_pin_pointing_right_keeps_its_straight_stub(self):
        plan = self.plan(self.LABEL, (1, 0))
        self.assertEqual(plan.bends, 0)

    def test_a_power_symbol_is_entered_along_its_own_axis(self):
        ground = power_symbol_footprint("GND", -1.0)       # hangs down the page
        supply = power_symbol_footprint("+3V3", 1.0)       # stands up
        self.assertEqual(ground.heading_cost((0, 1)), 0.0)
        self.assertGreater(ground.heading_cost((1, 0)), 0.0)
        self.assertGreater(ground.heading_cost((-1, 0)), 0.0)
        self.assertEqual(supply.heading_cost((0, -1)), 0.0)
        self.assertGreater(supply.heading_cost((1, 0)), 0.0)
        # a label, on the other hand, minds only whether it ends up vertical
        self.assertGreater(self.LABEL.heading_cost((0, 1)), 0.0)
        self.assertEqual(self.LABEL.heading_cost((1, 0)), 0.0)


class RailSymbolTests(unittest.TestCase):
    """The one symbol on a rail that joins several pins: the middle if it is free, else the nearest free place."""

    GROUND = power_symbol_footprint("GND", -1.0)     # reaches 5.72 mm down the page from its anchor

    def place(self, *, foreign=(), pins=(), wires=(), text=(), avoid=()):
        return choose_rail_symbol(
            horizontal=True, rail=10.0, low=0.0, high=10.0, end_symbol=self.GROUND,
            foreign_bounds=tuple(foreign), foreign_pin_points=tuple(pins), existing_wire_segments=tuple(wires),
            text_boxes=tuple(text), avoid_boxes=tuple(avoid), grid_mm=1.27,
        )

    def test_a_free_rail_puts_the_symbol_in_the_middle(self):
        plan = self.place()
        self.assertAlmostEqual(plan.position[0], 5.08)
        self.assertEqual(plan.position[1], 10.0)
        self.assertIsNone(plan.extension)

    def test_a_symbol_slides_along_the_rail_to_clear_what_is_under_the_middle(self):
        body = Bounds(2, 12, 8, 20)
        plan = self.place(foreign=(body,))
        self.assertFalse(self.GROUND.footprint_at(plan.position).overlaps(body))
        self.assertIsNone(plan.extension)
        self.assertTrue(0.0 <= plan.position[0] <= 10.0)

    def test_a_symbol_goes_past_the_end_of_the_rail_when_the_span_is_blocked(self):
        wide = Bounds(-5, 12, 20, 20)
        plan = self.place(foreign=(wide,))
        self.assertFalse(self.GROUND.footprint_at(plan.position).overlaps(wide))
        self.assertLess(plan.position[0], 0.0)
        # the rail is carried from its end to the symbol
        self.assertEqual(plan.extension, ((0.0, 10.0), (plan.position[0], 10.0)))

    def test_a_symbol_does_not_sit_on_text_or_on_another_symbol(self):
        text = Bounds(3.5, 13.0, 6.5, 14.3)       # where the middle symbol's value would be
        plan = self.place(text=(text,))
        self.assertFalse(self.GROUND.footprint_at(plan.position).overlaps(text))
        taken = Bounds(4.0, 10.5, 6.0, 16.0)
        plan = self.place(avoid=(taken,))
        self.assertFalse(self.GROUND.footprint_at(plan.position).overlaps(taken))

    def test_a_wire_or_a_pin_in_the_way_moves_the_symbol(self):
        wire = ((5.0, 11.0), (5.0, 14.0))
        plan = self.place(wires=(wire,))
        self.assertFalse(_touches_bbox(wire, self.GROUND.footprint_at(plan.position), 0.0))
        pin = (5.08, 13.0)
        plan = self.place(pins=(pin,))
        footprint = self.GROUND.footprint_at(plan.position)
        self.assertFalse(footprint.left <= pin[0] <= footprint.right and footprint.top <= pin[1] <= footprint.bottom)

    def test_an_extension_may_not_run_into_a_pin_or_connect_to_a_wire(self):
        wide = Bounds(-5, 12, 20, 20)
        free = self.place(foreign=(wide,))
        pin_on_the_way = (free.position[0] / 2.0, 10.0)
        plan = self.place(foreign=(wide,), pins=(pin_on_the_way,))
        self.assertTrue(plan is None or plan.position[0] > 10.0)
        wire_on_the_way = ((free.position[0] / 2.0, 8.0), (free.position[0] / 2.0, 10.0))     # ends on the rail line
        plan = self.place(foreign=(wide,), wires=(wire_on_the_way,))
        self.assertTrue(plan is None or plan.position[0] != free.position[0])

    def test_nowhere_to_put_it_is_none(self):
        self.assertIsNone(self.place(foreign=(Bounds(-50, 12, 50, 20),)))

    def test_a_vertical_rail_slides_along_y(self):
        plan = choose_rail_symbol(
            horizontal=False, rail=10.0, low=0.0, high=10.0, end_symbol=self.GROUND,
            foreign_bounds=(Bounds(8, 2, 14, 8),), grid_mm=1.27,
        )
        self.assertEqual(plan.position[0], 10.0)
        self.assertFalse(self.GROUND.footprint_at(plan.position).overlaps(Bounds(8, 2, 14, 8)))

    def test_a_hanging_rail_reaches_down_and_is_entered_straight_by_a_pin_pointing_down(self):
        standing = EndSymbol(half_width_mm=1.27, reach_mm=5.72, direction=-1)
        hung, rotation = hung_from_pin(standing, True)
        self.assertEqual(rotation, 180)
        self.assertEqual(hung.footprint_at((10.0, 10.0)).bottom, 10.0 + 5.72)
        self.assertEqual(standing.footprint_at((10.0, 10.0)).top, 10.0 - 5.72)
        common = dict(source=(10.16, 10.16), preferred_normal=(0, 1), own_bounds=Bounds(7.62, 5.0, 12.7, 10.16),
                      foreign_bounds=(), grid_mm=1.27, clearance_mm=2.54)
        self.assertEqual(choose_safe_terminal(end_symbol=hung, **common).bends, 0)
        self.assertGreater(choose_safe_terminal(end_symbol=standing, **common).bends, 0)

    def test_a_ground_or_a_rail_that_does_not_hang_is_unchanged(self):
        ground = EndSymbol(half_width_mm=1.27, reach_mm=5.72, direction=1)
        standing = EndSymbol(half_width_mm=1.27, reach_mm=5.72, direction=-1)
        self.assertEqual(hung_from_pin(ground, True), (ground, 0))
        self.assertEqual(hung_from_pin(standing, False), (standing, 0))


if __name__ == "__main__":
    unittest.main()
