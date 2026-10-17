"""Multi-unit parts as placeables: ``U1`` drawn in units becomes ``U1:1``, ``U1:2``, ... (spec 4c)."""
from __future__ import annotations

import unittest
from dataclasses import replace

from backend.schematic import FunctionalBlockIntent, NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.designators import split_unit, unit_reference
from backend.schematic.intent import expand_units
from backend.schematic.ladder_plan import plan_and_layout
from backend.schematic.ladder_roles import classify
from backend.schematic.placement_refinement import Connection, PinGeometry
from tests.unit import test_ladder_layout as layout_tests

ep = PinEndpoint
UNIT_PINS = {"U1": {1: {"1", "2", "3"}, 2: {"5", "6", "7"}, 3: {"4", "8"}}}       # two op-amps and the power unit


def intent() -> SchematicIntent:
    symbols = (
        SymbolIntent("V1", "Simulation_SPICE", "VDC", "in"),
        SymbolIntent("U1", "Amplifier_Operational", "LM358", "amp"),
        SymbolIntent("T1", "Device", "R", "tail", role="output_connector"),
    )
    nets = (
        NetIntent("n_in", (ep("V1", "1"), ep("U1", "3"))),
        NetIntent("n_mid", (ep("U1", "1"), ep("U1", "5"))),
        NetIntent("n_out", (ep("U1", "7"), ep("T1", "1"))),
        NetIntent("VCC", (ep("U1", "8"),), kind="power"),
        NetIntent("GND", (ep("V1", "2"), ep("U1", "4"), ep("T1", "2")), kind="ground"),
    )
    return SchematicIntent(symbols=symbols, nets=nets, blocks=(
        FunctionalBlockIntent("main", ("V1", "U1", "T1"), inputs=("n_in",), outputs=("n_out",)),))


class ExpandUnitsTests(unittest.TestCase):
    def test_placeable_names_split_back_to_the_part_and_unit(self):
        self.assertEqual(unit_reference("U5", 2), "U5:2")
        self.assertEqual(split_unit("U5:2"), ("U5", 2))
        self.assertEqual(split_unit("U5"), ("U5", None))
        self.assertEqual(split_unit("R1"), ("R1", None))

    def test_a_part_becomes_one_symbol_per_unit_with_its_endpoints_and_block_members_moved(self):
        expanded = expand_units(intent(), UNIT_PINS)
        self.assertEqual([s.reference for s in expanded.symbols], ["V1", "U1:1", "U1:2", "U1:3", "T1"])
        self.assertEqual({s.symbol_name for s in expanded.symbols if s.reference.startswith("U1")}, {"LM358"})
        endpoints = {net.name: [e.token for e in net.endpoints] for net in expanded.nets}
        self.assertEqual(endpoints["n_in"], ["V1.1", "U1:1.3"])
        self.assertEqual(endpoints["n_mid"], ["U1:1.1", "U1:2.5"])
        self.assertEqual(endpoints["VCC"], ["U1:3.8"])
        self.assertEqual(expanded.blocks[0].members, ("V1", "U1:1", "U1:2", "U1:3", "T1"))

    def test_a_unit_nothing_reaches_is_not_a_placeable_and_a_single_unit_part_is_untouched(self):
        base = intent()
        trimmed = SchematicIntent(symbols=base.symbols, nets=tuple(n for n in base.nets if n.name != "VCC"), blocks=base.blocks)
        trimmed = SchematicIntent(symbols=trimmed.symbols, nets=tuple(
            NetIntent("GND", tuple(e for e in n.endpoints if e.pin != "4"), kind="ground") if n.name == "GND" else n
            for n in trimmed.nets), blocks=trimmed.blocks)
        self.assertNotIn("U1:3", [s.reference for s in expand_units(trimmed, UNIT_PINS).symbols])
        self.assertIs(expand_units(base, {"U1": {1: {"1", "2", "3", "4", "5", "6", "7", "8"}}}), base)
        self.assertIs(expand_units(base, {}), base)

    def test_a_pin_no_unit_draws_is_refused_and_a_pin_every_unit_draws_goes_to_the_lowest(self):
        with self.assertRaises(ValueError):
            expand_units(intent(), {"U1": {1: {"1"}, 2: {"5"}}})
        shared = expand_units(intent(), {"U1": {1: {"1", "2", "3", "4", "8"}, 2: {"5", "6", "7", "4", "8"}}})
        endpoints = {net.name: [e.token for e in net.endpoints] for net in shared.nets}
        self.assertEqual(endpoints["VCC"], ["U1:1.8"])
        self.assertEqual(endpoints["GND"], ["V1.2", "U1:1.4", "T1.2"])


class UnitRolesAndLayoutTests(unittest.TestCase):
    PIN_COUNTS = {"V1": 2, "U1:1": 3, "U1:2": 3, "U1:3": 2, "T1": 1}

    def roles(self):
        return classify(expand_units(intent(), UNIT_PINS), self.PIN_COUNTS)

    def test_op_amp_units_are_chain_parts_and_the_power_unit_stands_with_the_supplies(self):
        roles = self.roles()
        chain = next(iter(roles.chains.values()))
        self.assertIn("U1:1", chain)
        self.assertIn("U1:2", chain)
        self.assertEqual(roles.pairs, ())                                    # the units share no pin number
        self.assertIn("U1:3", roles.other)                                   # every pin on a rail: a supply, not a chain part

    def amp_unit(self, ref, pins):
        a, b, out = pins
        return layout_tests.part(ref, {a: PinGeometry((-7.62, 2.54), layout_tests.LEFT), b: PinGeometry((-7.62, -2.54), layout_tests.LEFT),
                                       out: PinGeometry((7.62, 0.0), layout_tests.RIGHT)}, (-7.62, -5.08, 7.62, 5.08),
                                 orientations=None)

    def toy(self):
        power = layout_tests.part("U1:3", {"4": PinGeometry((0.0, 7.62), layout_tests.DOWN), "8": PinGeometry((0.0, -7.62), layout_tests.UP)},
                                  (-2.54, -7.62, 2.54, 7.62))
        symbols = [layout_tests.vertical("V1"), self.amp_unit("U1:1", ("3", "2", "1")), self.amp_unit("U1:2", ("5", "6", "7")),
                   power, layout_tests.horizontal("T1")]
        nets = [Connection("n_in", (("V1", "1"), ("U1:1", "3"))), Connection("n_mid", (("U1:1", "1"), ("U1:2", "5"))),
                Connection("n_out", (("U1:2", "7"), ("T1", "1"))),
                Connection("VCC", (("U1:3", "8"),), style="power"),
                Connection("GND", (("V1", "2"), ("U1:3", "4"), ("T1", "2")), style="power")]
        return symbols, nets, self.roles()

    def test_the_heuristic_places_every_unit_on_its_own_without_overlap(self):
        symbols, nets, roles = self.toy()
        plan, placed = plan_and_layout(symbols, nets, roles, text_widths={})
        self.assertEqual(set(placed), {s.reference for s in symbols})
        by_ref = {s.reference: s for s in symbols}
        boxes = {r: layout_tests.extent(by_ref[r], placed) for r in placed}
        for a in boxes:
            for b in boxes:
                if a < b:
                    self.assertFalse(boxes[a][0] < boxes[b][2] and boxes[b][0] < boxes[a][2]
                                     and boxes[a][1] < boxes[b][3] and boxes[b][1] < boxes[a][3], (a, b))
        self.assertLess(placed["U1:1"].position[0], placed["U1:2"].position[0])      # the units follow the signal

    def test_the_power_unit_stands_beside_a_placed_unit_of_its_part_and_is_planned_in_its_row(self):
        symbols, nets, roles = self.toy()
        plan, placed = plan_and_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        power = layout_tests.extent(by_ref["U1:3"], placed)
        near = min(min(abs(power[0] - sib[2]), abs(sib[0] - power[2]))
                   for sib in (layout_tests.extent(by_ref["U1:1"], placed), layout_tests.extent(by_ref["U1:2"], placed)))
        self.assertLess(near, 12.0)                                         # next to a sibling unit, not in the far supply column
        row = next(r for r in plan.rows if "U1:3" in r.order)
        index = row.order.index("U1:3")
        self.assertTrue({"U1:1", "U1:2"} & {row.order[max(index - 1, 0)], row.order[min(index + 1, len(row.order) - 1)]})
        self.assertEqual(index, len(row.order) - 1)                         # at the end: no wire of the row runs under it
        self.assertEqual(plan.pins["U1:3"], "")                             # its anchor on the row
        self.assertIn("U1:3", plan.orientations)

    def test_a_bypass_capacitor_across_the_two_rails_of_a_part_stands_beside_that_part(self):
        symbols, nets, roles = self.toy()
        symbols = [*symbols, layout_tests.vertical("CB")]
        nets = [Connection("VCC", (("U1:3", "8"), ("CB", "1")), style="power") if n.net == "VCC" else
                Connection("GND", (*n.pins, ("CB", "2")), style="power") if n.net == "GND" else n for n in nets]
        roles = replace(roles, other=(*roles.other, "CB"))
        plan, placed = plan_and_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        cap, power = layout_tests.extent(by_ref["CB"], placed), layout_tests.extent(by_ref["U1:3"], placed)
        self.assertLess(min(abs(cap[0] - power[2]), abs(power[0] - cap[2])), 12.0)         # next to the unit whose rails it bypasses
        row = next(r for r in plan.rows if "CB" in r.order)
        self.assertEqual(row.order[row.order.index("CB") - 1], "U1:3")

    def test_the_solver_places_the_units_too(self):
        try:
            import ortools  # noqa: F401
        except ImportError:
            self.skipTest("ortools is not installed")
        from backend.schematic.ladder_solve import legalise
        symbols, nets, roles = self.toy()
        plan, _placed = plan_and_layout(symbols, nets, roles, text_widths={})
        placed = legalise(plan, symbols, nets)
        self.assertEqual(set(placed), set(plan.parts()))
        self.assertIn("U1:1", placed)


if __name__ == "__main__":
    unittest.main()
