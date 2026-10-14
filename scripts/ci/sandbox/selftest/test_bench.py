"""The comparison bench: the pure parts (table formatting, run resolution); no KiCad, no engine run."""
from __future__ import annotations

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

from sbx import bench, env, redraw  # noqa: E402


class TableTests(unittest.TestCase):
    def test_markdown_table_shape(self) -> None:
        text = bench.markdown_table(["a", "b"], [["x", 1.23456], ["y", None]])
        self.assertEqual(text.splitlines(), ["| a | b |", "|---|---:|", "| x | 1.235 |", "| y | - |"])

    def test_demo_table_puts_human_first_and_skips_missing_values(self) -> None:
        rows = [("engine a", {"wire_mm": 10}), ("human", {"wire_mm": 20, "jogs": 1})]
        lines = bench.demo_table("demo_x", rows).splitlines()
        self.assertEqual(lines[0], "## demo_x")
        self.assertTrue(lines[4].startswith("| human | 20 |"))
        self.assertTrue(lines[5].startswith("| engine a | 10 |"))

    def test_fixtures_table_leaves_demos_out(self) -> None:
        kpi = {"wire_length_mm": 5.0, "corners": 2, "jogs": 0, "crossings": 1, "extent_width_mm": 40.4, "extent_height_mm": 20.0}
        text = bench.fixtures_table([("r1", {"rc_filter": kpi, "demo_buck": kpi}), ("r2", {"rc_filter": kpi})])
        self.assertEqual(text.count("rc_filter"), 2)
        self.assertNotIn("demo_buck", text)
        self.assertIn("40x20", text)


class TimeSectionTests(unittest.TestCase):
    def test_time_section_from_fake_timing(self) -> None:
        fake = {
            "wall_seconds": 100.0,
            "stages": {
                "trial:01-compact": {"seconds": 60.0, "calls": 1, "mcp_seconds": 0.0},
                "trial:01-compact/placement": {"seconds": 30.0, "calls": 1, "mcp_seconds": 10.0},
                "trial:01-compact/placement/refine": {"seconds": 15.0, "calls": 2, "mcp_seconds": 0.0},
                "trial:01-compact/routing": {"seconds": 20.0, "calls": 1, "mcp_seconds": 5.0},
            },
            "mcp_tools": {"sch_move_symbol": {"seconds": 10.0, "calls": 40}, "run_erc": {"seconds": 5.0, "calls": 1}},
        }
        text = bench.time_section({"rc_filter": fake})
        self.assertIn("## Time", text)
        self.assertIn("| rc_filter | 100 | 01-compact 60.0s |", text)
        self.assertIn("| trial:01-compact/placement | 30 | 30.0% |", text)
        self.assertIn("| sch_move_symbol | 10 | 40 |", text)
        self.assertIn("| MCP calls | 15 | 15.0% |", text)
        self.assertIn("| placement | 20 | 20.0% |", text)    # 30 - 15 child - 10 mcp + 15 refine
        self.assertEqual(bench.stage_family("trial:1-x/routing/labels"), "routing")

    def test_load_timings_reads_artifact_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "artifacts" / "schematic-engine-rc"
            d.mkdir(parents=True)
            (d / "timing.json").write_text('{"wall_seconds": 1}', encoding="utf-8")
            self.assertEqual(bench.load_timings(Path(tmp)), {"rc": {"wall_seconds": 1}})


class RunResolutionTests(unittest.TestCase):
    def test_label_and_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "20260101T000000Z-st1").mkdir()
            (runs / "20260102T000000Z-st1").mkdir()
            (runs / "20260103T000000Z-st2").mkdir()
            with patch.object(env, "RUNS_DIR", runs):
                resolved = bench.resolve_runs("st1", "st2")
            self.assertEqual([r.name for r in resolved], ["20260102T000000Z-st1", "20260103T000000Z-st2"])
            self.assertEqual(bench.label_of(resolved[0]), "st1")
            self.assertEqual(len(bench.resolve_runs(str(runs / "20260101T000000Z-st1"), None)), 1)
            with patch.object(env, "RUNS_DIR", runs), self.assertRaises(FileNotFoundError):
                bench.resolve_runs("nope", None)


class NnGapTests(unittest.TestCase):
    def _scene(self, boxes):
        from types import SimpleNamespace as N
        from backend.schematic.presentation_geometry import GeometryKind
        return N(items=[N(kind=GeometryKind.BODY, owner=o, bounds=N(left=l, top=t, right=r, bottom=b))
                        for o, (l, t, r, b) in boxes.items()])

    def test_known_gaps(self) -> None:
        from sbx import redraw
        scene = self._scene({"A": (0, 0, 2, 2), "B": (5, 0, 7, 2), "C": (12, 0, 14, 2), "#PWR1": (2.5, 0, 3, 1)})
        self.assertEqual(redraw.nn_gaps(scene), (3.0, 3.0))
        self.assertEqual(redraw.nn_gaps(self._scene({"A": (0, 0, 2, 2), "B": (1, 1, 3, 3)})), (0.0, 0.0))


class FreeTextCropTests(unittest.TestCase):
    SHEET = (
        '(kicad_sch (version 20250114)\n'
        '\t(lib_symbols (symbol \"X\" (text \"lib\" (at 9 9 0) (effects (font (size 1 1))))))\n'
        '\t(text \"Regulator\" (at 10 20 0) (effects (font (size 2 2))))\n'
        '\t(symbol (lib_id \"X\") (at 10 25 0))\n'
        ')'
    )

    def test_a_top_level_text_gets_a_box_above_its_anchor_and_library_text_does_not(self) -> None:
        # 0.75 * 2 mm * 9 characters = 13.5 mm run, 2 mm tall, 2 mm margin all round
        self.assertEqual(redraw.free_text_boxes(self.SHEET), [(8.0, 16.0, 25.5, 22.0)])

    def test_a_quarter_turn_swaps_the_width_and_the_height(self) -> None:
        turned = self.SHEET.replace("(at 10 20 0)", "(at 10 20 90)")
        self.assertEqual(redraw.free_text_boxes(turned), [(8.0, 4.5, 14.0, 22.0)])

    def test_a_parenthesis_inside_a_string_does_not_end_the_block(self) -> None:
        # an unclosed parenthesis in the text: 7 characters = 10.5 mm run
        odd = self.SHEET.replace('"Regulator"', '"Reg (5V"')
        self.assertEqual(redraw.free_text_boxes(odd), [(8.0, 16.0, 22.5, 22.0)])


if __name__ == "__main__":
    unittest.main()
