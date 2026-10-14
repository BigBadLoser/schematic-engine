"""The engine's redraw of a demo beside the human original: the pure parts (no KiCad, no engine run)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import redraw  # noqa: E402

SHEET = '''(kicad_sch
\t(version 20250114)
\t(wire
\t\t(pts (xy 10 10) (xy 20 10))
\t)
\t(text "SPICE: .tran 1m 500m (see \\"notes\\")"
\t\t(at 5 5 0)
\t\t(effects (font (size 1.27 1.27)))
\t)
\t(polyline
\t\t(pts (xy 0 0) (xy 5 5))
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 15 10 0)
\t\t(property "Reference" "R1"
\t\t\t(at 16 9 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1k"
\t\t\t(at 16 11 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Sim.Params" "r=(1k)"
\t\t\t(at 16 13 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 16 15 0)
\t\t\t(hide yes)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t)
)
'''


class SkipBlockTests(unittest.TestCase):
    def test_a_block_ends_at_its_own_closing_parenthesis(self):
        text = "(a (b) (c))(d)"
        self.assertEqual(text[: redraw._skip_block(text, 0)], "(a (b) (c))")

    def test_parentheses_and_quotes_in_a_string_do_not_count(self):
        text = '(text "a ) b \\" ( c") tail'
        self.assertEqual(text[: redraw._skip_block(text, 0)], '(text "a ) b \\" ( c")')


class WithoutAnnotationsTests(unittest.TestCase):
    def setUp(self):
        self.clean = redraw.without_annotations(SHEET)

    def test_free_text_and_shapes_are_gone_and_wires_and_symbols_stay(self):
        self.assertNotIn("SPICE", self.clean)
        self.assertNotIn("(polyline", self.clean)
        self.assertIn("(wire", self.clean)
        self.assertIn('(lib_id "Device:R")', self.clean)

    def test_the_reference_and_the_value_stay_visible_and_other_fields_are_hidden(self):
        self.assertNotIn('"Reference" "R1" (hide yes)', self.clean)
        self.assertNotIn('"Value" "1k" (hide yes)', self.clean)
        self.assertIn('(property "Sim.Params" "r=(1k)" (hide yes)', self.clean)

    def test_a_field_already_hidden_is_not_hidden_twice(self):
        self.assertEqual(self.clean.count("(hide yes)"), 2)
        self.assertEqual(redraw.without_annotations(self.clean), self.clean)

    def test_the_result_is_still_one_balanced_sheet(self):
        self.assertEqual(redraw._skip_block(self.clean, 0), len(self.clean.rstrip()))


class BlindPageTests(unittest.TestCase):
    def test_the_three_drawings_are_shuffled_with_the_key_kept_apart(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            out = Path(directory) / "out"
            sheets = {"human": Path("h.kicad_sch"), "shipped": Path("s.kicad_sch"), "engine": Path("e.kicad_sch")}
            drawn: list[tuple[str, bool]] = []

            def fake_draw(sheet, *, clean=False, scale=8.0):
                drawn.append((sheet.name, clean))
                return f"<svg id='{sheet.stem}'></svg>", (10.0, 5.0)

            with patch.object(redraw, "sheets_of", return_value=sheets), patch.object(redraw, "draw", fake_draw):
                key = redraw.blind_page(run, ["demo_x"], out, seed=1)
            self.assertEqual(sorted(key["demo_x"]), ["A", "B", "C"])
            self.assertEqual(sorted(key["demo_x"].values()), ["engine", "human", "shipped"])
            self.assertEqual(json.loads((out / "key.json").read_text()), key)
            self.assertIn(("h.kicad_sch", True), drawn)              # only the human sheet is stripped of annotations
            self.assertIn(("e.kicad_sch", False), drawn)
            page = (out / "demo_x.html").read_text()
            for word in ("human", "shipped", "engine"):
                self.assertNotIn(word, page.replace("<svg id='h'>", "").replace("id='s'", "").replace("id='e'", ""))
            self.assertLess(page.index("<h2>A</h2>"), page.index("<h2>B</h2>"))

    def test_the_same_seed_gives_the_same_order(self):
        with tempfile.TemporaryDirectory() as directory:
            sheets = {"human": Path("h.kicad_sch"), "shipped": Path("s.kicad_sch"), "engine": Path("e.kicad_sch")}
            results = []
            for _ in range(2):
                with patch.object(redraw, "sheets_of", return_value=sheets), \
                     patch.object(redraw, "draw", lambda *a, **k: ("<svg></svg>", (1.0, 1.0))):
                    results.append(redraw.blind_page(Path("r"), ["demo_x", "demo_y"], Path(directory), seed=7))
            self.assertEqual(results[0], results[1])


class TextCollisionTests(unittest.TestCase):
    """`auto-demo7`: the inductor of the buck converter had its name and its value written along the part, one over
    the other; the engine's own count of text overlaps leaves a symbol's own texts alone and said none."""

    @staticmethod
    def scene(*items):
        from backend.schematic.presentation_geometry import Bounds, GeometryItem, GeometryScene

        return GeometryScene(items=tuple(
            GeometryItem(f"{owner}.{kind.name}", kind, owner, Bounds(*box)) for owner, kind, box in items
        ))

    def test_a_reference_standing_on_its_own_value_is_a_collision(self):
        from backend.schematic.presentation_geometry import GeometryKind as K

        scene = self.scene(("L1", K.REFERENCE, (0, 0, 1.3, 4.0)), ("L1", K.VALUE, (0, 1.0, 1.3, 7.0)))
        self.assertEqual(redraw.text_collisions(scene), 1)

    def test_two_texts_of_a_symbol_that_stand_apart_are_not(self):
        from backend.schematic.presentation_geometry import GeometryKind as K

        scene = self.scene(("L1", K.REFERENCE, (0, 0, 4.0, 1.3)), ("L1", K.VALUE, (0, 2.0, 4.0, 3.3)))
        self.assertEqual(redraw.text_collisions(scene), 0)

    def test_a_text_on_another_symbols_text_is_one_too(self):
        from backend.schematic.presentation_geometry import GeometryKind as K

        scene = self.scene(("R1", K.VALUE, (0, 0, 4.0, 1.3)), ("C1", K.REFERENCE, (2.0, 0.5, 6.0, 1.8)))
        self.assertEqual(redraw.text_collisions(scene), 1)


class CrowdedTextTests(unittest.TestCase):
    @staticmethod
    def scene(*items):
        from backend.schematic.presentation_geometry import Bounds, GeometryItem, GeometryScene

        return GeometryScene(items=tuple(
            GeometryItem(f"{o}.{k.name}.{n}", k, o, Bounds(*b), net=net) for n, (o, k, b, net) in enumerate(items)
        ))

    def test_a_text_near_another_owners_text_or_foreign_wire_is_crowded_and_a_far_one_is_not(self):
        from backend.schematic.presentation_geometry import GeometryKind as K

        scene = self.scene(
            ("R1", K.REFERENCE, (0, 0, 4, 1.3), None), ("C1", K.VALUE, (4.5, 0, 8, 1.3), None),
            ("U1", K.REFERENCE, (30, 0, 34, 1.3), None), ("U1", K.PIN_ENDPOINT, (30, 5, 30, 5), "A"),
            ("w", K.WIRE, (30, 1.8, 36, 1.8), "B"),
        )
        self.assertEqual(redraw.crowded_text(scene), round(3 / 3, 3))
        own = self.scene(("U1", K.REFERENCE, (30, 0, 34, 1.3), None), ("U1", K.PIN_ENDPOINT, (30, 5, 30, 5), "A"),
                         ("w", K.WIRE, (30, 1.8, 36, 1.8), "A"))
        self.assertEqual(redraw.crowded_text(own), 0.0)


class ComparisonTests(unittest.TestCase):
    def test_every_sheet_gets_a_row_and_a_missing_value_is_a_dash(self):
        measured = {
            "human": {name: 1.0 for name, _label in redraw.COLUMNS},
            "engine": {**{name: 2.5 for name, _label in redraw.COLUMNS}, "bent_wire_rate": None},
        }
        text = redraw.format_comparison("demo_x", measured)
        self.assertEqual(len(text.splitlines()), 4)
        self.assertIn("human", text)
        self.assertNotIn("shipped", text)
        self.assertTrue(text.splitlines()[3].split()[4] == "-")

    def test_the_human_sheet_is_measured_under_its_own_references(self):
        from backend.schematic import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent

        intent = SchematicIntent(
            symbols=(SymbolIntent("R5", "Device", "R", "1"), SymbolIntent("R6", "Device", "R", "1")),
            nets=(NetIntent("A", (PinEndpoint("R5", "1"), PinEndpoint("R6", "1"))),),
        )
        with patch.dict(redraw.__dict__, {}):
            from scripts import schematic_demo_circuits as data

            with patch.dict(data.DEMO_RENAMES, {"demo_x": {"R5": "Rmeas1"}}):
                drawn = redraw.intent_as_drawn(intent, "demo_x")
        self.assertEqual([e.token for e in drawn.all_nets[0].endpoints], ["Rmeas1.1", "R6.1"])
        self.assertIs(redraw.intent_as_drawn(intent, "demo_without_renames"), intent)


if __name__ == "__main__":
    unittest.main()
