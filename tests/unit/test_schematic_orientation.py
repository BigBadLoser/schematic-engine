from __future__ import annotations

import math
import unittest

from backend.schematic.orientation import (
    ENGINE_EXTENSION_WEIGHT, EXTENSION_WEIGHT, MIRRORS, QUARTER_TURN_GRIDS, ROTATIONS, Orientation, PinTarget,
    choose_orientation, extension_penalty, mirror_box, order_inversions, score_orientation,
)


def place(point, rotation, mirror):
    """A library point (y up) or direction as KiCad draws it: flip to the page, turn counter-clockwise, then mirror."""
    x, y = point[0], -point[1]
    radians = math.radians(-rotation)
    rx = x * math.cos(radians) - y * math.sin(radians)
    ry = x * math.sin(radians) + y * math.cos(radians)
    if mirror == "x":
        ry = -ry
    elif mirror == "y":
        rx = -rx
    return (round(rx, 4) + 0.0, round(ry, 4) + 0.0)


def candidates_of(pins, normals):
    return {
        Orientation(rotation, mirror): (
            {pin: place(point, rotation, mirror) for pin, point in pins.items()},
            {pin: place(vector, rotation, mirror) for pin, vector in normals.items()},
        )
        for rotation in ROTATIONS for mirror in MIRRORS
    }


# Conn_01x02: pins on the left of the body, pin 1 above pin 2, each pointing left out of the symbol (library y up)
CONNECTOR = candidates_of(
    {"1": (-5.08, 0.0), "2": (-5.08, -2.54)}, {"1": (-1.0, 0.0), "2": (-1.0, 0.0)},
)
# a four-pin connector, to see order along a longer side
HEADER = candidates_of(
    {str(n): (-5.08, -2.54 * (n - 1)) for n in range(1, 5)}, {str(n): (-1.0, 0.0) for n in range(1, 5)},
)


class OrderTests(unittest.TestCase):
    def test_pins_in_order_down_a_side_have_no_inversions(self):
        pins = {"1": (0.0, 0.0), "2": (0.0, 2.54), "3": (0.0, 5.08)}
        normals = {pin: (-1.0, 0.0) for pin in pins}
        self.assertEqual(order_inversions(pins, normals), 0)

    def test_a_reversed_side_counts_every_pair(self):
        pins = {"1": (0.0, 5.08), "2": (0.0, 2.54), "3": (0.0, 0.0)}
        self.assertEqual(order_inversions(pins, {pin: (1.0, 0.0) for pin in pins}), 3)

    def test_along_a_top_or_bottom_side_the_order_is_left_to_right(self):
        pins = {"1": (2.54, 0.0), "2": (0.0, 0.0)}
        self.assertEqual(order_inversions(pins, {pin: (0.0, -1.0) for pin in pins}), 1)

    def test_pins_on_two_sides_have_no_one_order(self):
        pins = {"1": (0.0, 5.08), "2": (3.0, 0.0)}
        self.assertEqual(order_inversions(pins, {"1": (-1.0, 0.0), "2": (0.0, -1.0)}), 0)

    def test_named_pins_are_not_ordered(self):
        self.assertEqual(order_inversions({"A1": (0.0, 5.0), "B1": (0.0, 0.0)}, {"A1": (1.0, 0.0), "B1": (1.0, 0.0)}), 0)


class MirrorBoxTests(unittest.TestCase):
    def test_mirroring_about_the_anchor_swaps_the_sides_it_lies_on(self):
        box = (10.0, 20.0, 16.0, 26.0)
        self.assertEqual(mirror_box(box, (10.0, 20.0), "y"), (4.0, 20.0, 10.0, 26.0))
        self.assertEqual(mirror_box(box, (10.0, 20.0), "x"), (10.0, 14.0, 16.0, 20.0))
        self.assertEqual(mirror_box(box, (10.0, 20.0), ""), box)


class ConnectorOrientationTests(unittest.TestCase):
    # the peers are to the right: each pin should face right, toward a pin that points back at it
    def targets(self, y1=0.0, y2=2.54):
        return [
            PinTarget("1", (20.0, y1), (-1.0, 0.0)),
            PinTarget("2", (20.0, y2), (-1.0, 0.0)),
        ]

    def test_turning_a_connector_to_face_right_by_half_a_turn_reverses_its_pins(self):
        best, _scores = choose_orientation(CONNECTOR, self.targets(), ordered=True)
        self.assertEqual(best, Orientation(180, ""))
        pins, _normals = CONNECTOR[best]
        self.assertGreater(pins["1"][1], pins["2"][1])

    def test_with_mirrors_a_connector_facing_right_keeps_pin_1_on_top(self):
        best, _scores = choose_orientation(CONNECTOR, self.targets(), ordered=True, mirrors=True)
        self.assertEqual(best, Orientation(0, "y"))
        pins, normals = CONNECTOR[best]
        self.assertLess(pins["1"][1], pins["2"][1])
        self.assertGreater(normals["1"][0], 0.5)

    def test_reading_order_wins_even_when_the_peers_sit_in_the_opposite_order(self):
        # two pins 2.54 mm apart gain at most about 5 mm by swapping places, less than the 8 mm a pair out of order costs
        best, _scores = choose_orientation(
            CONNECTOR, self.targets(y1=20.0, y2=-20.0), ordered=True, mirrors=True,
        )
        self.assertEqual(best, Orientation(0, "y"))

    def test_without_the_ordering_asked_for_the_nearer_pins_decide(self):
        best, _scores = choose_orientation(CONNECTOR, self.targets(y1=0.0, y2=-2.54), ordered=False, mirrors=True)
        self.assertEqual(best, Orientation(180, ""))

    def test_a_tie_never_mirrors(self):
        # a single pin: mirror y and rotation 180 differ only in where the other pin would be
        single = candidates_of({"1": (-5.08, 0.0)}, {"1": (-1.0, 0.0)})
        best, _scores = choose_orientation(
            single, [PinTarget("1", (20.0, 0.0), (-1.0, 0.0))], ordered=True, mirrors=True,
        )
        self.assertEqual(best.mirror, "")

    def test_a_header_facing_right_reads_top_to_bottom(self):
        targets = [PinTarget(str(n), (30.0, 2.54 * (n - 1)), (-1.0, 0.0)) for n in range(1, 5)]
        best, _scores = choose_orientation(HEADER, targets, ordered=True, mirrors=True)
        pins, _normals = HEADER[best]
        self.assertEqual(order_inversions(pins, {pin: (1.0, 0.0) for pin in pins}), 0)

    def test_a_connector_below_its_circuit_faces_up_with_pin_1_on_the_left(self):
        # the peers are above: the pins face up, and read left to right
        targets = [PinTarget("1", (0.0, -20.0), (0.0, 1.0)), PinTarget("2", (2.54, -20.0), (0.0, 1.0))]
        best, _scores = choose_orientation(CONNECTOR, targets, ordered=True, mirrors=True)
        pins, normals = CONNECTOR[best]
        self.assertLess(pins["1"][0], pins["2"][0])
        self.assertLess(normals["1"][1], -0.5)


class ScoreTests(unittest.TestCase):
    def test_a_missing_pin_makes_an_orientation_invalid(self):
        pins, normals = CONNECTOR[Orientation(0, "")]
        self.assertIsNone(score_orientation(pins, normals, [PinTarget("9", (0.0, 0.0), (1.0, 0.0))]))

    def test_an_orientation_with_nothing_valid_is_an_error(self):
        with self.assertRaises(ValueError):
            choose_orientation(CONNECTOR, [PinTarget("9", (0.0, 0.0), (1.0, 0.0))])

    def test_an_orientation_is_a_quarter_turn_and_a_known_mirror(self):
        with self.assertRaises(ValueError):
            Orientation(45)
        with self.assertRaises(ValueError):
            Orientation(0, "z")


class ExtensionPenaltyTests(unittest.TestCase):
    """Which way a hung part's far pin points: toward its rail, or on along a chain."""

    UP = (0.0, -1.0)
    GRID = 2.54

    def penalty(self, far, weight=EXTENSION_WEIGHT):
        return extension_penalty((0.0, 0.0), far, self.UP, grid_mm=self.GRID, weight=weight)

    def test_a_far_pin_that_points_the_asked_way_costs_nothing_and_one_that_points_back_costs_most(self):
        self.assertAlmostEqual(self.penalty((0.0, -7.62)), 0.0)
        self.assertAlmostEqual(self.penalty((7.62, 0.0)), self.GRID * EXTENSION_WEIGHT)
        self.assertAlmostEqual(self.penalty((0.0, 7.62)), 2 * self.GRID * EXTENSION_WEIGHT)

    def test_a_part_with_both_pins_in_one_place_cannot_be_pointed_anywhere(self):
        self.assertAlmostEqual(self.penalty((0.0, 0.0)), self.GRID * 20.0)

    def test_a_diode_lying_down_stays_down_by_default_and_stands_up_under_the_engines_weight(self):
        # pointing sideways instead of up, against the quarter turn that would stand it up
        turn = QUARTER_TURN_GRIDS * self.GRID
        self.assertLess(self.penalty((7.62, 0.0)), turn)
        self.assertGreater(self.penalty((7.62, 0.0), ENGINE_EXTENSION_WEIGHT), turn)

    def test_a_resistor_that_has_to_be_turned_over_is_turned_over_either_way(self):
        # a half turn keeps the part's axis, so it pays no quarter turn: pointing back always costs more than it saves
        turn = QUARTER_TURN_GRIDS * self.GRID
        self.assertGreater(self.penalty((0.0, 7.62)), 0.0)
        self.assertGreater(self.penalty((0.0, 7.62), ENGINE_EXTENSION_WEIGHT), turn)


if __name__ == "__main__":
    unittest.main()
