"""The build side of multi-unit parts: placeable names at the MCP boundary, unit-aware sheet edits, netlist identity."""
from __future__ import annotations

import unittest

from backend.schematic import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.connectivity import collapse_units
from backend.schematic.direct_workflow import DirectWorkflow
from backend.schematic.symbol_edit import find_symbol_block, move_symbol, set_mirror

ep = PinEndpoint


def instance(reference: str, unit: int, x: float, y: float) -> str:
    return (
        f'\t(symbol\n\t\t(lib_id "Amplifier_Operational:LM358")\n\t\t(at {x} {y} 0)\n\t\t(unit {unit})\n'
        f'\t\t(uuid "00000000-0000-0000-0000-00000000000{unit}")\n'
        f'\t\t(property "Reference" "{reference}"\n\t\t\t(at {x + 2.54} {y - 5.08} 0)\n\t\t)\n'
        f'\t\t(property "Value" "LM358"\n\t\t\t(at {x + 2.54} {y + 5.08} 0)\n\t\t)\n'
        f'\t\t(instances\n\t\t\t(project "p"\n\t\t\t\t(path "/x"\n\t\t\t\t\t(reference "{reference}")\n\t\t\t\t\t(unit {unit})\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
    )


SHEET = "(kicad_sch\n" + instance("U5", 1, 10.0, 10.0) + instance("U5", 2, 40.0, 10.0) + instance("R1", 1, 70.0, 10.0).replace(
    "Amplifier_Operational:LM358", "Device:R") + ")\n"


class UnitEditTests(unittest.TestCase):
    def test_a_unit_placeable_finds_its_own_instance_and_a_plain_reference_the_first(self):
        self.assertEqual(find_symbol_block(SHEET, "U5:2")[3]["unit"], 2)
        self.assertEqual(find_symbol_block(SHEET, "U5:1")[3]["unit"], 1)
        self.assertEqual(find_symbol_block(SHEET, "U5")[3]["unit"], 1)
        self.assertIsNone(find_symbol_block(SHEET, "U5:3"))

    def test_moving_a_unit_shifts_it_and_its_fields_and_leaves_the_others(self):
        moved = move_symbol(SHEET, "U5:2", 50.0, 20.0)
        block = find_symbol_block(moved, "U5:2")[0]
        self.assertIn("(at 50 20 0)", block)
        self.assertIn("(at 52.54 14.92 0)", block)                       # the Reference text moved by the same step
        self.assertIn("(at 10.0 10.0 0)", find_symbol_block(moved, "U5:1")[0])
        with self.assertRaises(ValueError):
            move_symbol(SHEET, "U5:9", 0.0, 0.0)

    def test_mirroring_a_unit_marks_that_instance_only(self):
        mirrored = set_mirror(SHEET, "U5:2", "y")
        self.assertIn("(mirror y)", find_symbol_block(mirrored, "U5:2")[0])
        self.assertNotIn("(mirror", find_symbol_block(mirrored, "U5:1")[0])


class UnitIdentityTests(unittest.TestCase):
    def test_a_unit_symbol_is_sent_to_the_server_as_its_part_with_its_unit(self):
        sent = SymbolIntent("U5:2", "Amplifier_Operational", "LM358", "amp").to_mcp()
        self.assertEqual((sent["reference"], sent["unit"]), ("U5", 2))
        self.assertNotIn("unit", SymbolIntent("R1", "Device", "R", "1k").to_mcp())

    def test_the_netlist_check_sees_the_part_not_the_unit(self):
        intent = SchematicIntent(
            symbols=(SymbolIntent("U5:1", "A", "LM358", "amp"), SymbolIntent("U5:2", "A", "LM358", "amp"),
                     SymbolIntent("R1", "Device", "R", "1k")),
            nets=(NetIntent("a", (ep("U5:1", "1"), ep("R1", "1"))), NetIntent("b", (ep("U5:2", "7"), ep("R1", "2")))),
            no_connects=(ep("U5:2", "5"),),
        )
        whole = collapse_units(intent)
        self.assertEqual([s.reference for s in whole.symbols], ["U5", "R1"])
        self.assertEqual([e.token for net in whole.nets for e in net.endpoints], ["U5.1", "R1.1", "U5.7", "R1.2"])
        self.assertEqual(whole.no_connects, (ep("U5", "5"),))
        plain = SchematicIntent(symbols=(SymbolIntent("R1", "Device", "R", "1k"),), nets=())
        self.assertIs(collapse_units(plain), plain)

    def test_the_symbol_listing_names_each_unit_of_a_part_drawn_in_several(self):
        listing = {"result": (
            "Symbols (3 total):\n"
            "- U5 LM358 A:LM358 @ (10.00, 10.00) rot=0 unit=1\n"
            "- U5 LM358 A:LM358 @ (40.00, 10.00) rot=90 unit=2\n"
            "- R1 1k Device:R @ (70.00, 10.00) rot=0 unit=1")}
        found = DirectWorkflow._symbol_placements_from_listing(listing, {"U5:1", "U5:2", "R1"})
        self.assertEqual(found["U5:2"], {"x_mm": 40.0, "y_mm": 10.0, "rotation": 90, "unit": 2})
        self.assertEqual(found["R1"]["x_mm"], 70.0)


if __name__ == "__main__":
    unittest.main()
