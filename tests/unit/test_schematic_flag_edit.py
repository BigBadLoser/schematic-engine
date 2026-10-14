from __future__ import annotations

import re
import importlib.util
import unittest

from backend.schematic.flag_edit import attach_flag, split_wire

NEEDS_MCP_PRO = unittest.skipUnless(importlib.util.find_spec("kicad_mcp"), "needs kicad-mcp-pro (it parses the sheet)")

WIRE = """\t(wire
\t\t(pts (xy 100 139.7) (xy 120.65 139.7))
\t\t(stroke (width 0) (type solid))
\t\t(uuid "b4ffb284-270d-409d-a298-ca5452141ff9")
\t)"""

OTHER_WIRE = """\t(wire
\t\t(pts (xy 120.65 139.7) (xy 120.65 160.02))
\t\t(stroke (width 0) (type solid))
\t\t(uuid "ecbecc00-5ab0-470a-b6fd-7424be41d64e")
\t)"""

FLAG = """\t(symbol
\t\t(lib_id "power:PWR_FLAG")
\t\t(at 85.09 31.75 0)
\t\t(unit 1)
\t\t(body_style 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(in_pos_files yes)
\t\t(dnp no)
\t\t(uuid "da86a658-d3bb-412e-85a2-57ca23b47452")
\t\t(property "Reference" "#FLG1"
\t\t\t(at 85.09 25.4 0)
\t\t\t(hide yes)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(property "Value" "PWR_FLAG"
\t\t\t(at 85.09 26.67 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "3e584739-a2bc-459e-9597-931ead4d77e2")
\t\t)
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/e913a32c-bccd-4288-91fc-171cd316d022"
\t\t\t\t\t(reference "#FLG1")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)"""

SHEET = f"""(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(uuid "e913a32c-bccd-4288-91fc-171cd316d022")
\t(paper "A4")
{WIRE}
{OTHER_WIRE}
{FLAG}
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
)
"""

SEGMENT = ((100.0, 139.7), (120.65, 139.7))
POINT = (110.49, 139.7)


def wires(content):
    return [
        tuple(float(v) for v in match)
        for match in re.findall(r"\(wire\s+\(pts \(xy ([-\d.]+) ([-\d.]+)\) \(xy ([-\d.]+) ([-\d.]+)\)\)", content)
    ]


@NEEDS_MCP_PRO
class SplitWireTests(unittest.TestCase):
    def test_the_wire_is_cut_in_two_where_the_pin_will_be_and_the_others_are_untouched(self):
        cut = split_wire(SHEET, SEGMENT, POINT)
        self.assertEqual(
            wires(cut),
            [(100.0, 139.7, 110.49, 139.7), (110.49, 139.7, 120.65, 139.7), (120.65, 139.7, 120.65, 160.02)],
        )
        # every half is a whole block of its own with its own identity
        ids = re.findall(r'\(wire[^"]*\(uuid "([^"]+)"', cut)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(cut.count("(wire"), 3)

    def test_the_wire_may_be_named_from_either_end(self):
        cut = split_wire(SHEET, (SEGMENT[1], SEGMENT[0]), POINT)
        self.assertEqual(len(wires(cut)), 3)

    def test_a_wire_that_is_not_there_is_an_error(self):
        with self.assertRaises(ValueError):
            split_wire(SHEET, ((0.0, 0.0), (5.0, 0.0)), (2.54, 0.0))


@NEEDS_MCP_PRO
class AttachFlagTests(unittest.TestCase):
    def test_the_flag_moves_to_the_cut_and_its_fields_go_with_it(self):
        done = attach_flag(SHEET, "#FLG1", POINT, 0, SEGMENT)
        self.assertIn("(at 110.49 139.7 0)", done)
        # the fields keep their offsets from the symbol: 6.35 above the pin for the reference
        self.assertIn("(at 110.49 133.35 0)", done)
        self.assertNotIn("(at 85.09 31.75 0)", done)
        self.assertEqual(len(wires(done)), 3)

    def test_the_flag_is_turned_to_stand_off_the_wire(self):
        done = attach_flag(SHEET, "#FLG1", POINT, 180, SEGMENT)
        self.assertIn("(at 110.49 139.7 180)", done)
        # only the symbol's own angle changed: the fields were not rotated
        self.assertEqual(done.count(" 180)"), 1)

    def test_the_value_text_is_hidden_and_the_reference_is_unchanged(self):
        done = attach_flag(SHEET, "#FLG1", POINT, 0, SEGMENT)
        value = done[done.index('(property "Value"'):]
        self.assertLess(value.index("(hide yes)"), value.index("(effects"))
        self.assertEqual(done.count("(hide yes)"), 2)  # the reference's own and the value's
        self.assertEqual(done.count('"#FLG1"'), 2)

    def test_the_reader_sees_no_text_for_the_flag(self):
        import tempfile
        from pathlib import Path

        from backend.schematic.kicad_geometry import measured_scene_from_kicad

        done = attach_flag(SHEET, "#FLG1", POINT, 0, SEGMENT)
        with tempfile.TemporaryDirectory() as folder:
            for name, sheet, expected in (("before", SHEET, True), ("after", done, False)):
                path = Path(folder) / f"{name}.kicad_sch"
                path.write_text(sheet, encoding="utf-8")
                scene, _ = measured_scene_from_kicad(path)
                kinds = [str(item.kind) for item in scene.items]
                self.assertEqual(any("VALUE" in k.upper() or "POWER_TEXT" in k.upper() for k in kinds), expected, kinds)

    def test_a_flag_that_is_not_on_the_sheet_is_an_error(self):
        with self.assertRaises(ValueError):
            attach_flag(SHEET, "#FLG9", POINT, 0, SEGMENT)


if __name__ == "__main__":
    unittest.main()
