from __future__ import annotations

import re
import importlib.util
import unittest

from backend.schematic.symbol_edit import set_mirror, upright_value_text

NEEDS_MCP_PRO = unittest.skipUnless(importlib.util.find_spec("kicad_mcp"), "needs kicad-mcp-pro (it parses the sheet)")

CONNECTOR = """\t(symbol
\t\t(lib_id "Connector_Generic:Conn_01x02")
\t\t(at 100.33 50.8 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "da86a658-d3bb-412e-85a2-57ca23b47452")
\t\t(property "Reference" "J1"
\t\t\t(at 100.33 45.72 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(property "Value" "INPUT"
\t\t\t(at 100.33 58.42 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "3e584739-a2bc-459e-9597-931ead4d77e2")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "7e584739-a2bc-459e-9597-931ead4d77e3")
\t\t)
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/e913a32c-bccd-4288-91fc-171cd316d022"
\t\t\t\t\t(reference "J1")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)"""

OTHER = CONNECTOR.replace('"J1"', '"J2"').replace("100.33", "150.0")

SHEET = f"""(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(uuid "e913a32c-bccd-4288-91fc-171cd316d022")
\t(paper "A4")
{CONNECTOR}
{OTHER}
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
)
"""


def mirror_of(content, reference):
    block = re.search(rf'\(reference "{reference}"\)', content)
    start = content.rfind("(symbol", 0, block.start())
    # the instance block this reference's last (reference ...) belongs to starts at the previous "\t(symbol\n"
    start = content.rfind("\t(symbol\n", 0, block.start())
    found = re.search(r"\(mirror\s+([xy])\)", content[start:content.find("\n\t)\n", start)])
    return found.group(1) if found else ""


@NEEDS_MCP_PRO
class SetMirrorTests(unittest.TestCase):
    def test_the_mirror_line_goes_after_the_symbols_own_position(self):
        done = set_mirror(SHEET, "J1", "y")
        self.assertIn("\t\t(at 100.33 50.8 0)\n\t\t(mirror y)\n\t\t(unit 1)", done)

    def test_only_the_named_symbol_changes(self):
        done = set_mirror(SHEET, "J1", "y")
        self.assertEqual(mirror_of(done, "J1"), "y")
        self.assertEqual(mirror_of(done, "J2"), "")
        self.assertEqual(done.count("(mirror"), 1)

    def test_a_mirror_can_be_changed_and_taken_away(self):
        done = set_mirror(set_mirror(SHEET, "J1", "y"), "J1", "x")
        self.assertEqual(done.count("(mirror"), 1)
        self.assertEqual(mirror_of(done, "J1"), "x")
        self.assertEqual(set_mirror(done, "J1", ""), SHEET)

    def test_an_unknown_reference_or_axis_is_an_error(self):
        with self.assertRaises(ValueError):
            set_mirror(SHEET, "J9", "y")
        with self.assertRaises(ValueError):
            set_mirror(SHEET, "J1", "z")


POWER_SHEET = (
    "(kicad_sch\n"
    "\t(symbol\n\t\t(lib_id \"power:VEE\")\n\t\t(at 50.8 30.48 180)\n\t\t(unit 1)\n"
    "\t\t(property \"Reference\" \"#PWR1\"\n\t\t\t(at 50.8 24.13 180)\n\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n\t\t)\n"
    "\t\t(property \"Value\" \"VEE\"\n\t\t\t(at 50.8 26.67 180)\n\t\t\t(effects (font (size 1.27 1.27)))\n\t\t)\n"
    "\t)\n"
    "\t(symbol\n\t\t(lib_id \"power:VEE\")\n\t\t(at 80.0 30.48 0)\n\t\t(unit 1)\n"
    "\t\t(property \"Value\" \"VEE\"\n\t\t\t(at 80.0 26.67 0)\n\t\t\t(effects (font (size 1.27 1.27)))\n\t\t)\n"
    "\t)\n)\n"
)


class UprightValueTests(unittest.TestCase):
    def test_a_symbol_at_180_gets_its_value_at_angle_0_below_it(self):
        done = upright_value_text(POWER_SHEET, "power:VEE", 50.8, 30.48)
        self.assertIn('(property "Value" "VEE"\n\t\t\t(at 50.8 34.29 0)', done)
        self.assertIn('(property "Reference" "#PWR1"\n\t\t\t(at 50.8 24.13 180)', done)  # other text untouched

    def test_only_the_symbol_at_that_place_changes(self):
        done = upright_value_text(POWER_SHEET, "power:VEE", 50.8, 30.48)
        self.assertIn("(at 80.0 26.67 0)", done)
        self.assertEqual(upright_value_text(POWER_SHEET, "power:VCC", 50.8, 30.48), POWER_SHEET)


if __name__ == "__main__":
    unittest.main()
