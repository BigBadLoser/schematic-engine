from __future__ import annotations

import unittest

from backend.schematic.flag_attachment import DIRECTIONS, flag_footprint, plan_flag_attachment
from backend.schematic.presentation_geometry import Bounds

TEXT = 8.6          # the width of "PWR_FLAG" at KiCad's default size


def plan(wires, **kw):
    kw.setdefault("prefer", wires[0][0])
    return plan_flag_attachment(wires=tuple(wires), text_width_mm=TEXT, **kw)


class FlagFootprintTests(unittest.TestCase):
    def test_a_flag_standing_up_reserves_its_graphic_and_its_name_above_the_point(self):
        box = flag_footprint((50.0, 50.0), 0, TEXT)
        self.assertEqual((box.left, box.right), (50.0 - TEXT / 2, 50.0 + TEXT / 2))
        self.assertLess(box.top, 50.0 - 4.0)
        self.assertEqual(box.bottom, 50.0)

    def test_a_flag_hanging_down_reserves_the_space_below(self):
        box = flag_footprint((50.0, 50.0), 180, TEXT)
        self.assertEqual(box.top, 50.0)
        self.assertGreater(box.bottom, 54.0)

    def test_a_flag_standing_off_a_vertical_wire_reserves_its_graphic_to_the_side(self):
        left = flag_footprint((50.0, 50.0), 90, TEXT)
        right = flag_footprint((50.0, 50.0), 270, TEXT)
        self.assertEqual((left.right, right.left), (50.0, 50.0))
        self.assertLess(left.left, 48.0)
        self.assertGreater(right.right, 52.0)

    def test_rotations_point_the_way_a_placed_flag_does(self):
        self.assertEqual(DIRECTIONS, {0: (0.0, -1.0), 90: (-1.0, 0.0), 180: (0.0, 1.0), 270: (1.0, 0.0)})


class FlagPlanTests(unittest.TestCase):
    def test_a_flag_goes_on_the_wire_standing_up_near_where_it_was(self):
        wire = ((10.16, 50.8), (60.96, 50.8))
        found = plan([wire], prefer=(30.48, 40.0))
        self.assertIsNotNone(found)
        self.assertEqual(found.rotation, 0)
        self.assertEqual(found.point[1], 50.8)
        self.assertAlmostEqual(found.point[0], 30.48, delta=1.28)

    def test_a_flag_hangs_below_a_wire_with_a_symbol_above_it(self):
        wire = ((10.16, 50.8), (60.96, 50.8))
        body = Bounds(5.0, 38.0, 70.0, 49.0)
        found = plan([wire], foreign_bounds=(body,))
        self.assertEqual(found.rotation, 180)

    def test_a_flag_prefers_a_horizontal_wire_to_a_vertical_one_as_near(self):
        horizontal = ((10.16, 50.8), (40.64, 50.8))
        vertical = ((10.16, 50.8), (10.16, 20.32))
        found = plan([vertical, horizontal], prefer=(15.24, 45.72))
        # the vertical wire is as near as the horizontal one; the flag stands up off the horizontal one
        self.assertEqual(found.segment, horizontal)
        self.assertEqual(found.rotation, 0)

    def test_the_plan_names_the_wire_it_lands_on_so_that_it_can_be_split_there(self):
        near = ((10.16, 50.8), (60.96, 50.8))
        far = ((10.16, 100.0), (60.96, 100.0))
        found = plan([far, near], prefer=(30.48, 45.0))
        self.assertEqual(found.segment, near)

    def test_a_flag_stands_off_a_vertical_wire_sideways(self):
        wire = ((50.8, 10.16), (50.8, 60.96))
        found = plan([wire])
        self.assertIn(found.rotation, (90, 270))
        self.assertEqual(found.point[0], 50.8)

    def test_a_flag_keeps_off_the_wires_of_other_nets(self):
        wire = ((10.16, 50.8), (60.96, 50.8))
        # a wire crosses straight above the whole length of this one, and another straight below it
        above = ((5.08, 47.625), (66.04, 47.625))
        below = ((5.08, 53.975), (66.04, 53.975))
        self.assertIsNone(plan([wire], other_wires=(above, below)))

    def test_a_flag_is_not_put_where_the_net_already_joins_or_on_a_pin(self):
        wires = [((10.16, 50.8), (30.48, 50.8)), ((30.48, 50.8), (30.48, 20.32))]
        found = plan(wires, foreign_pin_points=((20.32, 50.8),))
        self.assertNotEqual(found.point, (20.32, 50.8))
        self.assertNotEqual(found.point, (30.48, 50.8))

    def test_nothing_is_planned_for_a_wire_too_short_to_hold_a_flag(self):
        self.assertIsNone(plan([((10.16, 50.8), (11.43, 50.8))]))

    def test_a_text_box_under_the_flag_costs_but_does_not_forbid(self):
        wire = ((10.16, 50.8), (20.32, 50.8))
        crowded = plan([wire], text_boxes=(Bounds(0.0, 40.0, 40.0, 50.0),))
        free = plan([wire])
        self.assertIsNotNone(crowded)
        self.assertGreater(crowded.cost, free.cost)


class FlagReachTests(unittest.TestCase):
    WIRE = ((0.0, 0.0), (200.0, 0.0))

    def test_a_point_far_from_every_host_pin_is_never_chosen(self):
        got = plan([self.WIRE], prefer=(200.0, 0.0), host_points=((0.0, 0.0),))
        self.assertIsNotNone(got)
        self.assertLessEqual(got.point[0], 10.16)

    def test_with_only_far_points_the_flag_is_unplaced(self):
        self.assertIsNone(plan([self.WIRE], host_points=((100.0, 50.0),)))

    def test_the_point_nearest_a_pin_wins_among_valid_ones(self):
        got = plan([self.WIRE], prefer=(8.0, 0.0), host_points=((0.0, 0.0),))
        self.assertEqual(got.point, (1.27, 0.0))


if __name__ == "__main__":
    unittest.main()
