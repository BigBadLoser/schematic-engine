from unittest import TestCase

from backend.schematic.intent import (
    FunctionalBlockIntent, NetIntent, PinEndpoint, SchematicIntent, SymbolIntent,
)
from backend.schematic.presentation_geometry import (
    Bounds, GeometryItem, GeometryKind, GeometryScene,
)
from backend.schematic.readability import measure_readability
from backend.schematic.wire_labels import is_auto_name, nets_to_name, plan_caption, plan_wire_label


def ends(*pins):
    return tuple(PinEndpoint(ref, pin) for ref, pin in pins)


def intent_with(nets, blocks=()):
    refs = sorted({e.reference for n in nets for e in n.endpoints})
    return SchematicIntent(
        symbols=tuple(SymbolIntent(reference=r, library="Device", symbol_name="R", value="1k") for r in refs),
        nets=tuple(nets), blocks=tuple(blocks),
    )


class NetsToNameTests(TestCase):
    def test_auto_generated_names_are_not_meaningful(self):
        for name in ("N_1", "Net-(R1-Pad1)", "unconnected-(U1-NC)", "N$3"):
            self.assertTrue(is_auto_name(name), name)
        self.assertFalse(is_auto_name("VIN"))

    def test_three_pins_or_block_edge_qualifies(self):
        many = NetIntent("FB", ends(("R1", "1"), ("R2", "1"), ("C1", "1")))
        edge = NetIntent("OUT", ends(("R1", "2"), ("R3", "1")))
        auto = NetIntent("N_7", ends(("R4", "2"), ("R2", "2"), ("R3", "2")))
        small = NetIntent("MID", ends(("R5", "2"), ("R6", "2")))
        label = NetIntent("LBL", ends(("R7", "1"), ("R8", "1"), ("R9", "1")), routing="label")
        block = FunctionalBlockIntent(name="b", members=("R1", "R3"), outputs=("OUT",))
        names = [n.name for n in nets_to_name(intent_with([many, edge, auto, small, label], [block]))]
        self.assertEqual(names, ["FB", "OUT"])


class PlanTests(TestCase):
    LINE = (((0.0, 0.0), (20.32, 0.0)),)

    def test_label_sits_at_the_middle_of_the_longest_horizontal(self):
        point, rotation = plan_wire_label("VIN", self.LINE, [], 5.0, 1.27)
        self.assertEqual((point[1], rotation), (0.0, 0))
        self.assertAlmostEqual(point[0], 10.16, delta=1.3)

    def test_label_slides_past_an_obstacle(self):
        point, _ = plan_wire_label("VIN", self.LINE, [Bounds(7.0, -3.0, 13.0, -0.3)], 5.0, 1.27)
        free, _ = plan_wire_label("VIN", self.LINE, [], 5.0, 1.27)
        self.assertNotEqual(point, free)

    def test_vertical_when_there_is_no_horizontal_and_none_when_blocked(self):
        vertical = (((5.0, 0.0), (5.0, 12.7)),)
        self.assertEqual(plan_wire_label("A", vertical, [], 3.0, 1.27)[1], 90)
        self.assertIsNone(plan_wire_label("A", self.LINE, [Bounds(-5, -5, 30, 5)], 5.0, 1.27))

    def test_caption_above_block_lifts_and_gives_up(self):
        block = Bounds(0.0, 20.0, 20.0, 40.0)
        x, y = plan_caption(block, "Buck", [], 6.0, 2.0)
        self.assertEqual((round(x, 2), round(y, 2)), (7.62, 17.78))
        lifted = plan_caption(block, "Buck", [Bounds(0, 15, 20, 18.5)], 6.0, 2.0)
        self.assertLess(lifted[1], y)
        self.assertIsNone(plan_caption(block, "Buck", [Bounds(-50, -50, 50, 19)], 6.0, 2.0))


class ReadabilityTests(TestCase):
    def test_a_label_on_its_own_wire_is_not_text_on_wire(self):
        wire = GeometryItem("wire:0:0", GeometryKind.WIRE, "wire:0", Bounds(0, 0, 20, 0), segment=((0, 0), (20, 0)))
        width = 6.0
        label = GeometryItem(
            "label:0", GeometryKind.NET_LABEL, "label:0", Bounds(7, -1.3, 13, 0.6),
            net="VIN", anchor=(10.0, 0.0), text="VIN", angle=0.0,
        )
        result = measure_readability(GeometryScene((wire, label)))
        self.assertEqual(result.count("text_on_wire"), 0)
