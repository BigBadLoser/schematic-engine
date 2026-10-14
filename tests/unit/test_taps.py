"""A tap: a one-pin part (a test point) on a net other pins share hangs from that net's wire, its pin facing the wire."""
from __future__ import annotations

import unittest

from backend.schematic import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.ladder_plan import plan_and_layout
from backend.schematic.ladder_roles import classify
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Connection, OrientedGeometry, PinGeometry, PlacedSymbol, rotate_geometry
from tests.unit import test_ladder_layout as layout_tests

ep = PinEndpoint


def test_point(ref: str) -> PlacedSymbol:
    base = OrientedGeometry({"1": PinGeometry((0.0, 2.54), layout_tests.DOWN)}, (-1.27, -2.54, 1.27, 2.54), (-1.27, -1.27, 1.27, 1.27))
    return PlacedSymbol(ref, (64.0, 50.8), layout_tests.FLAT, {Orientation(r): rotate_geometry(base, r) for r in (0, 90, 180, 270)})


class TapRoleTests(unittest.TestCase):
    def circuit(self, tp_net_extra=True, rail=False):
        symbols = (SymbolIntent("A", "Device", "R", "a"), SymbolIntent("B", "Device", "R", "b"), SymbolIntent("TP1", "Connector", "TestPoint", "t"))
        mid = (ep("A", "2"), ep("B", "1"), ep("TP1", "1")) if tp_net_extra else (ep("TP1", "1"),)
        nets = (NetIntent("mid", mid), NetIntent("in", (ep("A", "1"),)), NetIntent("out", (ep("B", "2"),)))
        if rail:
            nets = (NetIntent("GND", (ep("TP1", "1"), ep("A", "2"), ep("B", "1")), kind="ground"), nets[1], nets[2])
        from backend.schematic import FunctionalBlockIntent
        return classify(SchematicIntent(symbols=symbols, nets=nets, blocks=(FunctionalBlockIntent("main", ("A", "B"), inputs=("in",), outputs=("out",)),)),
                        {"A": 2, "B": 2, "TP1": 1})

    def test_a_one_pin_part_on_a_shared_signal_net_is_a_tap_with_no_rail(self):
        roles = self.circuit()
        self.assertEqual(roles.attachments["TP1"].rail, "")
        self.assertEqual(roles.attachments["TP1"].net, "mid")
        self.assertNotIn("TP1", roles.other)

    def test_a_one_pin_part_alone_on_its_net_or_on_a_rail_is_not_a_tap(self):
        self.assertIn("TP1", self.circuit(tp_net_extra=False).other)
        self.assertIn("TP1", self.circuit(rail=True).other)


class TapLayoutTests(unittest.TestCase):
    def test_a_tap_stands_on_the_wire_of_its_net_with_its_pin_toward_it_and_is_planned_as_a_hang(self):
        symbols = [layout_tests.horizontal("A"), layout_tests.horizontal("B"), test_point("TP1")]
        nets = [Connection("mid", (("A", "2"), ("B", "1"), ("TP1", "1")))]
        roles = layout_tests.Roles({}, (), {"main": ("A", "B")}, {"TP1": layout_tests.Attachment("A", "mid", "", "supply")}, {}, {}, (), ())
        plan, placed = plan_and_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        geometry = by_ref["TP1"].geometry[placed["TP1"].orientation]
        tip = (placed["TP1"].position[0] + geometry.pins["1"].point[0], placed["TP1"].position[1] + geometry.pins["1"].point[1])
        wire_y = placed["A"].position[1] + by_ref["A"].geometry[placed["A"].orientation].pins["2"].point[1]
        self.assertAlmostEqual(tip[1], wire_y)                                        # its pin lands on the wire
        self.assertEqual(geometry.pins["1"].normal, layout_tests.DOWN)                # facing the wire, standing above it
        hang = next(h for h in plan.hangs if h.reference == "TP1")
        self.assertEqual((hang.wire, hang.side), ("mid", "above"))
        self.assertEqual(plan.parts(), {"A", "B", "TP1"})


if __name__ == "__main__":
    unittest.main()
