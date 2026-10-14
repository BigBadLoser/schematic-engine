"""Orientation costs learned from the KiCad demo sheets."""
from __future__ import annotations

import math
import unittest

from backend.schematic.orientation import Orientation
from backend.schematic.orientation_prior import (
    FLOOR, SCALE, canonical_orientation, orientation_costs, part_class, part_context,
)
from backend.schematic.orientation_prior_data import PRIOR
from backend.schematic.placement_refinement import (
    Connection, OrientedGeometry, PinGeometry, PlacedSymbol, Weights, _Model, refine_placement, rotate_geometry,
)

UP, DOWN, LEFT, RIGHT = (0.0, -1.0), (0.0, 1.0), (-1.0, 0.0), (1.0, 0.0)
ALL = [Orientation(r, m) for r in (0, 90, 180, 270) for m in ("", "x", "y")]


def two_pin() -> dict[Orientation, OrientedGeometry]:
    upright = OrientedGeometry(
        {"1": PinGeometry((0.0, -3.81), UP), "2": PinGeometry((0.0, 3.81), DOWN)}, (-1.02, -3.81, 1.02, 3.81),
    )
    return {o: rotate_geometry(upright, o.rotation, o.mirror) for o in ALL}


def chip() -> dict[Orientation, OrientedGeometry]:
    base = OrientedGeometry(
        {"1": PinGeometry((-5.08, 0.0), LEFT), "2": PinGeometry((5.08, 0.0), RIGHT), "3": PinGeometry((0.0, 5.08), DOWN)},
        (-5.08, -5.08, 5.08, 5.08),
    )
    return {o: rotate_geometry(base, o.rotation, o.mirror) for o in ALL}


def cheapest(costs):
    low = min(costs.values())
    return [o for o, c in costs.items() if c <= low + 1e-9]


class ClassifierTests(unittest.TestCase):
    def test_classes(self):
        cases = [
            (("Device", "R", "R1", 2), "resistor"), (("Device", "C", "C1", 2), "capacitor"),
            (("Device", "CP", "C2", 2), "capacitor_polarized"), (("Device", "R_Potentiometer", "RV1", 3), "other"),
            (("Regulator_Linear", "LM317_TO-220", "U1", 3), "regulator"),
            (("Amplifier_Operational", "LM358", "U2", 8), "opamp"), (("Device", "LED", "D1", 2), "led"),
            (("Diode", "1N4148", "D2", 2), "diode"), (("Connector", "Conn_01x02", "J1", 2), "connector"),
            (("MCU_Microchip", "ATtiny85", "U3", 8), "ic"), (("Transistor_FET", "IRF540", "Q1", 3), "transistor_fet"),
            (("Device", "Q_NPN_BCE", "Q2", 3), "transistor_bjt"), (("Device", "L", "L1", 2), "inductor"),
        ]
        for args, expected in cases:
            self.assertEqual(part_class(*args), expected, args)

    def test_contexts(self):
        self.assertEqual(part_context({"1": "signal", "2": "ground"}), "shunt_ground")
        self.assertEqual(part_context({"1": "signal", "2": "supply"}), "shunt_supply")
        self.assertEqual(part_context({"1": "signal", "2": "signal"}), "series")
        self.assertEqual(part_context({"1": "supply", "2": "ground"}), "across_rails")
        self.assertEqual(part_context({"1": "signal", "2": "ground", "3": "signal"}), "multipin")
        self.assertEqual(part_context({"1": "signal", "2": "nc"}), "other")

    def test_canonical_form_rewrites_mirror_y(self):
        self.assertEqual(canonical_orientation(Orientation(0, "y")), "180/x")
        self.assertEqual(canonical_orientation(Orientation(270, "y")), "90/x")
        self.assertEqual(canonical_orientation(Orientation(90)), "90/none")


class CostTests(unittest.TestCase):
    def test_formula_floor_and_unseen_values(self):
        costs = orientation_costs("resistor", "shunt_ground", ["1"], two_pin())
        shares = PRIOR[("resistor", "shunt_ground")]["shares"]
        best = max(shares.values())
        # pin 1 points up at rotation 0
        self.assertAlmostEqual(costs[Orientation(0)], SCALE * math.log(best / shares["up"]))
        self.assertAlmostEqual(costs[Orientation(180)], 0.0)       # pin 1 points down
        flat = OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT), "2": PinGeometry((1.0, 0.0), RIGHT)}, (0, 0, 1, 1))
        unseen = orientation_costs("ic", "multipin", [], {Orientation(0, "x"): flat})
        top = max(PRIOR[("ic", "multipin")]["shares"].values())
        self.assertAlmostEqual(unseen[Orientation(0, "x")], SCALE * math.log(top / FLOOR))

    def test_shunt_resistor_is_cheapest_with_ground_pin_down(self):
        costs = orientation_costs("resistor", "shunt_ground", ["2"], two_pin())
        geometry = two_pin()
        for orientation in cheapest(costs):
            self.assertEqual(geometry[orientation].pins["2"].normal, DOWN)

    def test_series_resistor_is_cheapest_horizontal(self):
        costs = orientation_costs("resistor", "series", [], two_pin())
        geometry = two_pin()
        for orientation in cheapest(costs):
            self.assertNotEqual(geometry[orientation].pins["1"].point[0], geometry[orientation].pins["2"].point[0])

    def test_ic_is_cheapest_unturned_and_mirror_x_costs_more_than_mirror_y(self):
        costs = orientation_costs("ic", "multipin", [], chip())
        self.assertEqual(cheapest(costs), [Orientation(0)])
        self.assertGreater(costs[Orientation(0, "x")], costs[Orientation(0, "y")])

    def test_fallbacks_and_connectors(self):
        self.assertEqual(orientation_costs("regulator", "multipin", [], chip()), orientation_costs("ic", "multipin", [], chip()))
        self.assertEqual(
            orientation_costs("capacitor_polarized", "series", [], two_pin()),
            orientation_costs("capacitor", "series", [], two_pin()),
        )
        self.assertIsNone(orientation_costs("connector", "multipin", [], chip()))
        self.assertIsNone(orientation_costs("crystal", "series", [], two_pin()))


class RefinementTests(unittest.TestCase):
    def test_a_sideways_shunt_resistor_is_turned_so_its_ground_pin_points_down(self):
        geometry = two_pin()
        costs = orientation_costs("resistor", "shunt_ground", ["2"], geometry)
        symbols = [PlacedSymbol("R1", (50.8, 50.8), Orientation(90), geometry, movable=True, orientation_cost=costs)]
        result = refine_placement(symbols, [], start_conventional=True)
        self.assertLess(result.after.total, result.before.total)
        self.assertEqual(geometry[result.placements["R1"].orientation].pins["2"].normal, DOWN)

    def test_a_symbol_without_costs_still_pays_for_turning(self):
        geometry = two_pin()
        symbols = [PlacedSymbol("R1", (50.8, 50.8), Orientation(0), geometry, movable=True)]
        self.assertEqual(refine_placement(symbols, []).placements, {})
        model = _Model(symbols, [], {}, Weights(), None)
        base = model.cost({"R1": ((50.8, 50.8), Orientation(0))}).total
        turned = model.cost({"R1": ((50.8, 50.8), Orientation(90))}).total
        self.assertGreaterEqual(turned - base, Weights().turn - 1e-6)

    def test_with_costs_the_flip_and_turn_prices_are_replaced(self):
        geometry = two_pin()
        costs = {o: 0.0 for o in geometry}
        symbols = [PlacedSymbol("R1", (50.8, 50.8), Orientation(0), geometry, movable=True, orientation_cost=costs)]
        model = _Model(symbols, [], {}, Weights(), None)
        base = model.cost({"R1": ((50.8, 50.8), Orientation(0))}).total
        self.assertAlmostEqual(model.cost({"R1": ((50.8, 50.8), Orientation(90, "x"))}).total, base)


if __name__ == "__main__":
    unittest.main()
