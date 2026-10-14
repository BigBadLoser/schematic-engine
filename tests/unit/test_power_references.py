"""Power symbols are numbered in order of creation, not at random, so two builds of one intent agree."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from backend.kicad import power_references

NEEDS_MCP_PRO = unittest.skipUnless(importlib.util.find_spec("kicad_mcp"), "needs kicad-mcp-pro")


def sheet(*references: str) -> str:
    body = "".join(f'(symbol (lib_id "power:GND") (property "Reference" "{r}" (at 0 0 0)))\n' for r in references)
    return f"(kicad_sch\n{body})\n"


class SequentialPowerReferenceTests(unittest.TestCase):
    def setUp(self):
        power_references._issued.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def write(self, name: str, text: str) -> Path:
        path = Path(self.directory.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_an_empty_sheet_starts_at_one_and_counts_up_within_a_batch(self):
        path = self.write("a.kicad_sch", sheet("R1"))
        got = [power_references.next_power_reference(path) for _ in range(3)]
        self.assertEqual(got, ["#PWR01", "#PWR02", "#PWR03"])

    def test_a_sheet_that_has_pwr03_gets_pwr04_next(self):
        path = self.write("b.kicad_sch", sheet("#PWR03", "#PWR01", "R1"))
        self.assertEqual(power_references.next_power_reference(path), "#PWR04")

    def test_after_a_write_numbering_follows_the_sheet(self):
        path = self.write("c.kicad_sch", sheet())
        self.assertEqual(power_references.next_power_reference(path), "#PWR01")
        path.write_text(sheet("#PWR01", "#PWR02", "#PWR07"), encoding="utf-8")
        self.assertEqual(power_references.next_power_reference(path), "#PWR08")

    def test_two_builds_of_the_same_sheet_agree(self):
        runs = []
        for name in ("one.kicad_sch", "two.kicad_sch"):
            path = self.write(name, sheet("U1"))
            runs.append([power_references.next_power_reference(path) for _ in range(4)])
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(len(set(runs[0])), 4)

    @NEEDS_MCP_PRO
    def test_install_replaces_the_random_name_in_mcp_pro(self):
        import kicad_mcp.schematic.basic_authoring as basic
        import kicad_mcp.schematic.connectivity_authoring as connectivity
        import kicad_mcp.schematic.references as references
        originals = [(m, m.power_reference_from_uuid) for m in (references, basic, connectivity)]
        self.addCleanup(lambda: [setattr(m, "power_reference_from_uuid", f) for m, f in originals])
        path = self.write("d.kicad_sch", sheet("#PWR03"))
        tools = type("T", (), {"_get_schematic_file": staticmethod(lambda: path)})
        power_references.install(tools)
        names = [module.power_reference_from_uuid("0000") for module in (references, basic, connectivity)]
        self.assertEqual(names, ["#PWR04", "#PWR05", "#PWR06"])


if __name__ == "__main__":
    unittest.main()
