"""libavoid dump analysis on hand-built dumps (no libavoid needed)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
if str(SANDBOX) not in sys.path:
    sys.path.insert(0, str(SANDBOX))

from sbx import dumps  # noqa: E402

GRID = 1.27


def point(x, y):
    return {"x": x, "y": y}


def dump(routes, endpoints, edges):
    return {
        "protect_wires": False,
        "payload": {"graph": {"edges": edges}},
        "raw_routes": routes,
        "endpoints": endpoints,
    }


class FrameTests(unittest.TestCase):
    """The engine poses libavoid's problem with a grid corner as its origin; a dump is read back in the sheet's frame."""

    @staticmethod
    def posed(offset):
        ox, oy = offset
        node = {"id": "portal::U1.1", "x": 10.0 - ox - 0.01, "y": 20.0 - oy - 0.01, "width": 0.02, "height": 0.02}
        return {
            "payload": {"graph": {"children": [node, {"id": "obstacle::U1", "x": 12.0 - ox, "y": 18.0 - oy,
                                                      "width": 4.0, "height": 6.0}], "edges": []}},
            "raw_routes": {"e": {"sourcePoint": point(10.0 - ox, 20.0 - oy), "bendPoints": [point(12.0 - ox, 20.0 - oy)],
                                 "targetPoint": point(12.0 - ox, 30.0 - oy)}},
            "endpoints": {"U1.1": {"portal": [10.0, 20.0], "point": [9.0, 20.0]}},
        }

    def test_a_dump_posed_from_a_corner_is_read_back_in_the_sheets_frame(self):
        shifted = dumps.absolute(self.posed((7.62, 5.08)))
        nodes = {c["id"]: c for c in shifted["payload"]["graph"]["children"]}
        self.assertAlmostEqual(nodes["obstacle::U1"]["x"], 12.0)
        self.assertAlmostEqual(nodes["obstacle::U1"]["y"], 18.0)
        route = shifted["raw_routes"]["e"]
        self.assertEqual((route["sourcePoint"]["x"], route["sourcePoint"]["y"]), (10.0, 20.0))
        self.assertEqual((route["bendPoints"][0]["x"], route["targetPoint"]["y"]), (12.0, 30.0))

    def test_a_dump_from_before_the_change_is_returned_as_it_is(self):
        data = self.posed((0.0, 0.0))
        self.assertIs(dumps.absolute(data), data)


class DumpAnalysisTests(unittest.TestCase):
    def rows(self, data):
        return {row["edge"]: row for row in dumps.analyze_dump(data, GRID)}

    def test_leaving_a_portal_back_toward_its_pin_is_backtracking(self):
        # Pin faces WEST; the portal is west of it. Heading east leaves toward the pin.
        data = dump(
            routes={"e1": {"sourcePoint": point(-2.54, 0), "bendPoints": [point(0.0, 0), point(0.0, 5.08)],
                           "targetPoint": point(10.16, 5.08)}},
            endpoints={"A.1": {"portal": [-2.54, 0.0], "side": "WEST"}, "B.1": {"portal": [10.16, 5.08], "side": "EAST"}},
            edges=[{"id": "e1", "source": "portal::A.1", "target": "portal::B.1"}],
        )
        row = self.rows(data)["e1"]
        self.assertTrue(row["source_backtracks"])
        self.assertTrue(row["target_backtracks"])  # arrives heading east, away from the east-facing pin

    def test_leaving_outward_and_arriving_from_outside_is_clean(self):
        data = dump(
            routes={"e1": {"sourcePoint": point(-2.54, 0), "bendPoints": [point(-7.62, 0), point(-7.62, 5.08)],
                           "targetPoint": point(10.16, 5.08)}},
            endpoints={"A.1": {"portal": [-2.54, 0.0], "side": "WEST"}, "B.1": {"portal": [10.16, 5.08], "side": "EAST"}},
            edges=[{"id": "e1", "source": "portal::A.1", "target": "portal::B.1"}],
        )
        row = self.rows(data)["e1"]
        self.assertFalse(row["source_backtracks"])
        # last leg runs from x=-7.62 to the east portal: heading east = away from the east-facing pin
        self.assertTrue(row["target_backtracks"])

    def test_endpoint_displacement_and_nonorthogonal_segments(self):
        data = dump(
            routes={"e1": {"sourcePoint": point(0.1, 0.0), "bendPoints": [], "targetPoint": point(3.0, 4.0)}},
            endpoints={"A.1": {"portal": [0.0, 0.0], "side": "EAST"}, "B.1": {"portal": [3.0, 4.0], "side": "WEST"}},
            edges=[{"id": "e1", "source": "portal::A.1", "target": "portal::B.1"}],
        )
        row = self.rows(data)["e1"]
        self.assertEqual(row["source_displacement_mm"], 0.1)
        self.assertEqual(row["target_displacement_mm"], 0.0)
        self.assertEqual(row["nonorthogonal_segments"], 1)

    def test_half_grid_vertices_are_counted(self):
        data = dump(
            routes={"e1": {"sourcePoint": point(0.0, 0.0), "bendPoints": [point(1.905, 0.0)], "targetPoint": point(1.905, 2.54)}},
            endpoints={}, edges=[{"id": "e1", "source": "portal::A.1", "target": "portal::B.1"}],
        )
        row = self.rows(data)["e1"]
        self.assertEqual(row["half_grid_vertices"], 2)  # 1.905 = 1.5 grid, twice
        self.assertEqual(row["vertex_coordinates"], 6)

    def test_directory_analysis_aggregates_files(self):
        data = dump(
            routes={"e1": {"sourcePoint": point(0, 0), "bendPoints": [], "targetPoint": point(5.08, 0)}},
            endpoints={"A.1": {"portal": [0.0, 0.0], "side": "EAST"}, "B.1": {"portal": [5.08, 0.0], "side": "WEST"}},
            edges=[{"id": "e1", "source": "portal::A.1", "target": "portal::B.1"}],
        )
        with tempfile.TemporaryDirectory() as directory:
            for index in range(2):
                (Path(directory) / f"libavoid-{index}.json").write_text(json.dumps(data), encoding="utf-8")
            result = dumps.analyze(Path(directory), GRID)
        self.assertEqual(result["summary"]["dump_files"], 2)
        self.assertEqual(result["summary"]["routes"], 2)
        self.assertEqual(result["summary"]["backtracking_ends"], 0)


class ObstacleSignalTests(unittest.TestCase):
    """Obstacle B spans x 5..9, y -2..2; with the 1.27 mm buffer its keep-out spans x 3.73..10.27."""

    def data(self, source, target, bends=()):
        def portal(token, xy):
            return {"id": f"portal::{token}", "x": xy[0] - 0.01, "y": xy[1] - 0.01, "width": 0.02, "height": 0.02}

        return {
            "protect_wires": True,
            "payload": {
                "options": {"shapeBufferDistance": 1.27},
                "graph": {
                    "children": [
                        {"id": "obstacle::B", "x": 5.0, "y": -2.0, "width": 4.0, "height": 4.0},
                        {"id": "foreign_wire::0", "x": 20.0, "y": -1.0, "width": 0.5, "height": 6.0},
                        portal("A.1", source), portal("C.1", target),
                    ],
                    "edges": [{"id": "e1", "source": "portal::A.1", "target": "portal::C.1"}],
                },
            },
            "raw_routes": {"e1": {"sourcePoint": point(*source), "bendPoints": [point(*b) for b in bends],
                                  "targetPoint": point(*target)}},
            "endpoints": {"A.1": {"portal": list(source), "side": "EAST"}, "C.1": {"portal": list(target), "side": "WEST"}},
        }

    def row(self, data):
        return dumps.analyze_dump(data, GRID)[0]

    def test_a_portal_inside_the_buffered_keep_out_is_trapped(self):
        self.assertTrue(self.row(self.data((4.0, 0.0), (14.0, 0.0)))["source_trapped"])
        self.assertFalse(self.row(self.data((3.0, 0.0), (14.0, 0.0)))["source_trapped"])

    def test_a_route_through_an_obstacle_interior_is_counted_but_one_along_its_edge_is_not(self):
        self.assertEqual(self.row(self.data((4.0, 0.0), (12.0, 0.0)))["obstacle_crossings"], 1)
        self.assertEqual(self.row(self.data((3.0, -4.0), (12.0, -4.0)))["obstacle_crossings"], 0)
        self.assertEqual(self.row(self.data((3.0, -2.0), (12.0, -2.0)))["obstacle_crossings"], 0)  # grazes the edge

    def test_foreign_wire_boxes_are_counted_separately_from_obstacles(self):
        row = self.row(self.data((18.0, 0.0), (22.0, 0.0)))
        self.assertEqual((row["foreign_wire_crossings"], row["obstacle_crossings"]), (1, 0))

    def test_summary_counts_trapped_ends_and_routes_through_obstacles(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "libavoid-1.json").write_text(json.dumps(self.data((4.0, 0.0), (12.0, 0.0))), encoding="utf-8")
            summary = dumps.analyze(Path(directory), GRID)["summary"]
        self.assertEqual((summary["trapped_portal_ends"], summary["routes_through_obstacles"]), (1, 1))


class TerminalDumpTests(unittest.TestCase):
    def make_run(self, root: Path, windows):
        """windows: [(fixture, log_mtime, [terminal dict ...])] in execution order."""
        import os

        (root / "logs").mkdir()
        (root / "dumps").mkdir()
        (root / "manifest.json").write_text(json.dumps({"fixtures": [w[0] for w in windows]}), encoding="utf-8")
        for index, (fixture, end, terminals) in enumerate(windows):
            log = root / "logs" / f"{fixture}.log"
            log.write_text("", encoding="utf-8")
            os.utime(log, (end, end))
            if terminals:
                dump_file = root / "dumps" / f"power-terminals-{index}.json"
                dump_file.write_text(json.dumps({"clearance_mm": 3.81, "terminals": terminals}), encoding="utf-8")
                os.utime(dump_file, (end - 1, end - 1))

    @staticmethod
    def terminal(pin, net, strategy, segments, fallback=False):
        reference, number = pin.split(".")
        return {"reference": reference, "pin": number, "net": net, "strategy": strategy,
                "fallback": fallback, "segments": segments}

    def test_terminals_are_grouped_by_fixture_window_and_measured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_run(root, [
                ("rc_filter", 1000.0, [self.terminal("J1.2", "GND", "dogleg_2.54_10.16",
                                                    [[0, 0, 0, 2.54], [0, 2.54, 10.16, 2.54]])]),
                ("linear_regulator", 2000.0, [
                    self.terminal("U1.1", "GND", "fallback_direct_20.32", [[0, 0, 0, 20.32]], fallback=True),
                    self.terminal("C2.1", "+3V3", "direct_5.08", [[0, 0, 0, 5.08]]),
                ]),
            ])
            grouped = dumps.terminals(root)
        self.assertEqual([t["length_mm"] for t in grouped["rc_filter"]], [12.7])
        self.assertEqual([t["pin"] for t in grouped["linear_regulator"]], ["U1.1", "C2.1"])
        self.assertEqual([t["fallback"] for t in grouped["linear_regulator"]], [True, False])

    def test_report_counts_unchecked_fallbacks_and_tolerates_empty_runs(self):
        grouped = {"linear_regulator": [
            {"pin": "U1.1", "net": "GND", "strategy": "fallback_direct_20.32", "fallback": True, "length_mm": 20.32},
            {"pin": "C2.1", "net": "+3V3", "strategy": "direct_5.08", "fallback": False, "length_mm": 5.08},
        ]}
        text = dumps.format_terminals(grouped)
        self.assertIn("2 power terminals, 1 on the unchecked fallback, mean stub 12.70 mm, longest 20.32 mm", text)
        self.assertIn("no power-terminal dumps", dumps.format_terminals({"rc_filter": []}))


if __name__ == "__main__":
    unittest.main()
