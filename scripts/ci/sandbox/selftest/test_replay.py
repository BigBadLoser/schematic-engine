"""Replay lab: payload variants and the engine's accept/reject checks (no router needed)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
if str(SANDBOX) not in sys.path:
    sys.path.insert(0, str(SANDBOX))

from sbx import env, replay  # noqa: E402


def box(node_id, left, top, right, bottom):
    return {"id": node_id, "x": left, "y": top, "width": right - left, "height": bottom - top}


def dump(protect_wires=True):
    """Symbols A (left) and B (right) with pins facing each other, a text keep-out and one foreign wire."""
    inflate = replay.BODY_INFLATION
    return {
        "protect_wires": protect_wires,
        "payload": {
            "options": {"shapeBufferDistance": 1.27, "nudgeOrthogonalSegmentsConnectedToShapes": True,
                        "crossingPenalty": 101.6},
            "graph": {
                "id": "root",
                "children": [
                    box("obstacle::A", 0 - inflate, 0 - inflate, 5 + inflate, 5 + inflate),
                    box("obstacle::B", 20 - inflate, 0 - inflate, 25 + inflate, 5 + inflate),
                    box("keepout::A:field:Reference", 0, -4, 5, -1),
                    box("foreign_wire::0", 10 - 0.508, -10 - 0.508, 10 + 0.508, -5 + 0.508),
                    box("portal::A.1", 7.54 - 0.01, 2.5 - 0.01, 7.54 + 0.01, 2.5 + 0.01),
                    box("portal::B.1", 17.46 - 0.01, 2.5 - 0.01, 17.46 + 0.01, 2.5 + 0.01),
                ],
                "edges": [{"id": "e1", "source": "portal::A.1", "target": "portal::B.1"}],
            },
        },
        "raw_routes": {"e1": {"sourcePoint": {"x": 7.54, "y": 2.5}, "bendPoints": [],
                              "targetPoint": {"x": 17.46, "y": 2.5}}},
        "endpoints": {
            "A.1": {"reference": "A", "point": [5.0, 2.5], "side": "EAST", "portal": [7.54, 2.5]},
            "B.1": {"reference": "B", "point": [20.0, 2.5], "side": "WEST", "portal": [17.46, 2.5]},
        },
    }


class GeometryRecoveryTests(unittest.TestCase):
    def test_bodies_are_the_obstacles_shrunk_back_by_the_engine_inflation(self):
        self.assertEqual(replay.bodies_of(dump())["A"], (0.0, 0.0, 5.0, 5.0))

    def test_foreign_wires_are_recovered_from_their_boxes(self):
        (a, b), = replay.wires_of(dump())
        self.assertAlmostEqual(a[0], 10.0)
        self.assertAlmostEqual(b[0], 10.0)
        self.assertEqual((round(a[1], 6), round(b[1], 6)), (-10.0, -5.0))

    def test_compact_points_drops_repeats_and_collinear_middles(self):
        points = replay.compact_points([(0, 0), (0, 0), (5, 0), (10, 0), (10, 5)])
        self.assertEqual(points, [(0, 0), (10, 0), (10, 5)])

    def test_a_proper_crossing_is_not_a_connection_but_a_t_or_an_overlap_is(self):
        horizontal = ((0, 5), (10, 5))
        self.assertTrue(replay.segments_intersect(horizontal, ((5, 0), (5, 10))))
        self.assertFalse(replay.connects_or_overlaps(horizontal, ((5, 0), (5, 10))))     # crosses in both interiors
        self.assertTrue(replay.connects_or_overlaps(horizontal, ((5, 5), (5, 10))))      # T: a wire end on the interior
        self.assertTrue(replay.connects_or_overlaps(horizontal, ((10, 0), (10, 10))))    # lands on the end point
        self.assertTrue(replay.connects_or_overlaps(horizontal, ((4, 5), (14, 5))))      # collinear overlap
        self.assertTrue(replay.connects_or_overlaps(horizontal, ((10, 5), (14, 5))))     # end to end
        self.assertFalse(replay.connects_or_overlaps(horizontal, ((11, 0), (11, 10))))   # not touching at all

    def test_closed_segment_intersection_includes_touching(self):
        self.assertTrue(replay.segments_intersect(((0, 0), (10, 0)), ((10, 0), (10, 5))))
        self.assertFalse(replay.segments_intersect(((0, 0), (10, 0)), ((11, -1), (11, 5))))


class PrepareTests(unittest.TestCase):
    def test_asis_leaves_the_payload_alone_and_does_not_alias_the_dump(self):
        data = dump()
        prepared = replay.prepare(data, replay.VARIANTS["asis"], "k")
        self.assertEqual(prepared.graph, data["payload"]["graph"])
        prepared.graph["children"].clear()
        self.assertTrue(data["payload"]["graph"]["children"])

    def test_legal_crossings_drop_the_foreign_wire_obstacles(self):
        prepared = replay.prepare(dump(), replay.VARIANTS["cross_ok"], "k")
        self.assertFalse([c for c in prepared.graph["children"] if c["id"].startswith("foreign_wire::")])
        self.assertTrue(prepared.legal_crossings)

    def test_options_and_text_removal_apply(self):
        prepared = replay.prepare(dump(), replay.VARIANTS["plain"], "k")
        self.assertIs(prepared.options["nudgeOrthogonalSegmentsConnectedToShapes"], False)
        self.assertEqual(prepared.options["crossingPenalty"], 0)
        no_text = replay.prepare(dump(), replay.VARIANTS["no_text"], "k")
        self.assertFalse([c for c in no_text.graph["children"] if c["id"].startswith("keepout::")])

    def test_portal_offset_moves_the_portal_along_the_outward_side(self):
        prepared = replay.prepare(dump(), replay.VARIANTS["portal1.27"], "k")
        self.assertEqual(prepared.attach["A.1"], (6.27, 2.5))
        node = next(c for c in prepared.graph["children"] if c["id"] == "portal::A.1")
        self.assertAlmostEqual(node["x"] + node["width"] / 2, 6.27)

    def test_ports_replace_obstacle_and_portal_with_a_symbol_node_and_port_edges(self):
        prepared = replay.prepare(dump(), replay.VARIANTS["ports"], "k")
        ids = [c["id"] for c in prepared.graph["children"]]
        self.assertIn("symbol::A", ids)
        self.assertNotIn("obstacle::A", ids)
        self.assertNotIn("portal::A.1", ids)
        symbol = next(c for c in prepared.graph["children"] if c["id"] == "symbol::A")
        self.assertEqual((symbol["x"], symbol["width"]), (0.0, 5.0))          # the engine's body bounds, uninflated
        self.assertEqual(symbol["ports"][0]["x"], 5.0)                         # on the boundary, at the tip
        self.assertEqual(symbol["ports"][0]["properties"], {"elk.port.side": "EAST"})
        edge = prepared.graph["edges"][0]
        self.assertEqual((edge["source"], edge["sourcePort"]), ("symbol::A", "port::A.1"))
        self.assertFalse(prepared.portal_mode)
        self.assertEqual(prepared.attach["A.1"], (5.0, 2.5))

    def test_buffer_baking_zeroes_the_global_buffer_and_grows_real_obstacles_only(self):
        prepared = replay.prepare(dump(), replay.VARIANTS["buf0"], "k")
        self.assertEqual(prepared.options["shapeBufferDistance"], 0.0)
        obstacle = next(c for c in prepared.graph["children"] if c["id"] == "obstacle::A")
        self.assertAlmostEqual(obstacle["width"], 5 + 2 * replay.BODY_INFLATION + 2 * 1.27)
        portal = next(c for c in prepared.graph["children"] if c["id"] == "portal::A.1")
        self.assertAlmostEqual(portal["width"], 0.02)                           # portals stay tiny and unbuffered


class EngineVerdictTests(unittest.TestCase):
    BODIES = {"A": (0.0, 0.0, 5.0, 5.0), "B": (20.0, 0.0, 25.0, 5.0), "X": (10.0, 1.0, 12.0, 4.0)}
    TIPS_HIGH = ((5.0, 2.54), (20.0, 2.54))
    TIPS_LOW = ((5.0, 6.35), (20.0, 6.35))

    def verdict(self, raw, start, end, tips, bodies=None, wires=(), **kwargs):
        options = dict(portal_mode=True, protect_wires=True)
        options.update(kwargs)
        return replay.engine_verdict(raw, start, end, tips, ("A", "B"),
                                     self.BODIES if bodies is None else bodies, list(wires), **options)

    def test_a_clear_route_is_accepted_and_gets_its_pin_stubs(self):
        # Below every body, so only the escape stubs touch A and B.
        verdict, points = self.verdict([(7.62, 6.35), (17.78, 6.35)], (7.62, 6.35), (17.78, 6.35), self.TIPS_LOW)
        self.assertEqual(verdict, "ok")
        self.assertEqual(points, [(5.0, 6.35), (20.0, 6.35)])   # collinear stub, route and stub merge into one segment

    def test_a_diagonal_after_snapping_is_nonorthogonal(self):
        # The engine overwrites the first and last points with the attach points, so the diagonal has to
        # come from an interior vertex that is off both axes.
        verdict, _ = self.verdict([(7.62, 6.35), (12.7, 7.62), (17.78, 6.35)], (7.62, 6.35), (17.78, 6.35),
                                  self.TIPS_LOW, simplify=False)
        self.assertEqual(verdict, "nonorthogonal")

    def test_simplification_rescues_a_zigzag_on_a_clear_path(self):
        # Five points once the pin stubs are added, so simplify_route runs, as in the engine, before the checks.
        verdict, points = self.verdict([(7.62, 6.35), (12.7, 7.62), (17.78, 6.35)], (7.62, 6.35), (17.78, 6.35),
                                       self.TIPS_LOW, bodies={})
        self.assertEqual((verdict, points), ("ok", [(5.0, 6.35), (20.0, 6.35)]))

    def test_an_axis_aligned_clear_pin_to_junction_pair_gets_the_direct_straight_route(self):
        verdict, points = self.verdict([(0.0, 0.0), (1.0, 1.0)], (7.62, 6.35), (17.78, 6.35), self.TIPS_LOW,
                                       bodies={}, synthetic=(False, True))
        self.assertEqual((verdict, points), ("ok", [(5.0, 6.35), (20.0, 6.35)]))

    def test_two_distinct_endpoints_joined_by_a_straight_diagonal_are_nonorthogonal(self):
        # The recorded MCU failure: portal and junction differ in x and y, and libavoid returned its
        # no-path straight line between them.
        verdict, _ = self.verdict([(116.84, 55.88), (121.92, 57.15)], (116.84, 55.88), (121.92, 57.15),
                                  ((114.3, 55.88), (121.92, 57.15)), bodies={})
        self.assertEqual(verdict, "nonorthogonal")

    def test_a_route_through_a_foreign_body_is_rejected(self):
        # X sits between the portals at y 1..4 and is neither endpoint symbol.
        verdict, _ = self.verdict([(7.62, 2.54), (17.78, 2.54)], (7.62, 2.54), (17.78, 2.54), self.TIPS_HIGH)
        self.assertEqual(verdict, "body")

    def test_first_and_last_segments_may_cross_their_own_symbol(self):
        bodies = {"A": (0.0, 0.0, 9.0, 5.0), "B": (20.0, 0.0, 25.0, 5.0)}   # A reaches past its own portal
        verdict, _ = self.verdict([(7.62, 2.54), (17.78, 2.54)], (7.62, 2.54), (17.78, 2.54), self.TIPS_HIGH,
                                  bodies=bodies)
        self.assertEqual(verdict, "ok")

    def test_every_coordinate_is_snapped_to_the_grid_before_the_checks(self):
        _, points = self.verdict([(7.5, 0.1), (7.5, 6.4), (17.9, 6.4)], (7.62, 7.62), (17.78, 7.62),
                                 self.TIPS_LOW, bodies={}, portal_mode=False, protect_wires=False)
        interior = points[1:-1]
        self.assertTrue(interior)
        self.assertTrue(all(abs(round(c / 1.27) * 1.27 - c) < 1e-9 for point in interior for c in point))

    def test_touching_a_pre_existing_wire_is_rejected_only_when_wires_are_protected(self):
        wires = [((12.7, -5.0), (12.7, 10.0))]
        raw = [(7.62, 6.35), (17.78, 6.35)]
        protected = self.verdict(raw, (7.62, 6.35), (17.78, 6.35), self.TIPS_LOW, bodies={}, wires=wires)
        relaxed = self.verdict(raw, (7.62, 6.35), (17.78, 6.35), self.TIPS_LOW, bodies={}, wires=wires,
                               protect_wires=False)
        self.assertEqual((protected[0], relaxed[0]), ("foreign_wire", "ok"))

    def test_legal_crossings_accept_a_proper_crossing_only(self):
        crossing = [((12.7, 0.0), (12.7, 10.0))]
        touching = [((12.7, 6.35), (12.7, 10.0))]       # ends on the route
        raw = [(7.62, 6.35), (17.78, 6.35)]
        kwargs = dict(bodies={}, legal_crossings=True)
        self.assertEqual(self.verdict(raw, (7.62, 6.35), (17.78, 6.35), self.TIPS_LOW, wires=crossing, **kwargs)[0], "ok")
        self.assertEqual(self.verdict(raw, (7.62, 6.35), (17.78, 6.35), self.TIPS_LOW, wires=touching, **kwargs)[0],
                         "foreign_wire")


class EvaluateAndReportTests(unittest.TestCase):
    def test_a_missing_route_and_a_router_error_are_no_route(self):
        data = dump()
        prepared = replay.prepare(data, replay.VARIANTS["asis"], "k")
        self.assertEqual(replay.evaluate(data, prepared, {"ok": True, "routes": {}})[0]["verdict"], "no_route")
        row = replay.evaluate(data, prepared, {"ok": False, "error": "boom"})[0]
        self.assertEqual((row["verdict"], row["reason"]), ("no_route", "boom"))

    def test_a_port_route_that_does_not_start_on_its_pin_is_no_route(self):
        data = dump()
        prepared = replay.prepare(data, replay.VARIANTS["ports"], "k")
        centre_to_centre = {"ok": True, "routes": {"e1": {
            "sourcePoint": {"x": 2.5, "y": 2.5}, "bendPoints": [], "targetPoint": {"x": 22.5, "y": 2.5}}}}
        row = replay.evaluate(data, prepared, centre_to_centre)[0]
        self.assertEqual(row["verdict"], "no_route")
        self.assertIn("does not reach", row["reason"])

    def test_the_recorded_route_is_flagged_as_reproduced(self):
        data = dump()
        prepared = replay.prepare(data, replay.VARIANTS["asis"], "k")
        same = {"ok": True, "routes": {"e1": data["raw_routes"]["e1"]}}
        self.assertTrue(replay.evaluate(data, prepared, same)[0]["same_as_recorded"])

    def test_summary_and_common_comparison(self):
        rows = {
            "asis": {"c1": [{"edge": "e1", "verdict": "ok", "length_mm": 10.0, "bends": 2},
                            {"edge": "e2", "verdict": "body", "length_mm": 9.0, "bends": 1}]},
            "ports": {"c1": [{"edge": "e1", "verdict": "ok", "length_mm": 6.0, "bends": 0},
                             {"edge": "e2", "verdict": "ok", "length_mm": 4.0, "bends": 1}]},
        }
        summary = replay.summarize(rows["asis"])
        self.assertEqual((summary["edges"], summary["accepted"], summary["verdicts"]), (2, 1, {"ok": 1, "body": 1}))
        comparison = replay.common_comparison(rows, "asis")["ports"]
        self.assertEqual((comparison["common_edges"], comparison["length_asis_mm"], comparison["length_variant_mm"]),
                         (1, 10.0, 6.0))
        text = replay.format_report({"calls": 1, "variants": {n: replay.summarize(r) for n, r in rows.items()},
                                     "rows": rows})
        self.assertIn("On the edges both 'asis' and the variant accept", text)

    def write_call(self, directory: Path, accepted_lines):
        (directory / "libavoid-1.json").write_text(json.dumps(dump()), encoding="utf-8")
        if accepted_lines is not None:
            (directory / "libavoid-1.accepted.jsonl").write_text("\n".join(accepted_lines), encoding="utf-8")

    def test_calibration_agrees_when_the_engine_accepted_the_same_route(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_call(Path(directory), [json.dumps({"edge": "e1", "points": [[5.0, 2.5], [20.0, 2.5]]})])
            counts = replay.calibrate(Path(directory))["counts"]
        self.assertEqual((counts["agree_accepted"], counts["same_points"], counts["lab_stricter"]), (1, 1, 0))

    def test_calibration_flags_a_lab_that_accepts_what_the_engine_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_call(Path(directory), [])            # the call ran, but the engine recorded nothing: e1 failed
            result = replay.calibrate(Path(directory))
        self.assertEqual(result["counts"]["lab_more_permissive"], 1)
        self.assertEqual(result["disagreements"][0]["kind"], "lab_more_permissive")

    def test_calibration_skips_calls_without_an_engine_record_and_reports_them(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_call(Path(directory), None)
            counts = replay.calibrate(Path(directory))["counts"]
        self.assertEqual((counts["calls"], counts["calls_without_record"]), (0, 1))
        self.assertIn("1 without", replay.format_calibration({"counts": counts, "disagreements": []}))

    def test_unknown_variant_and_missing_dumps_fail_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(replay.ReplayError):
                replay.replay(Path(directory), ["nope"])
            with self.assertRaises(replay.ReplayError):
                replay.replay(Path(directory), ["asis"])


@unittest.skipUnless(shutil.which("node") and (env.NODE_MODULES / "@mr_mint" / "elkjs-libavoid").is_dir(),
                     "node or the installed router is not available")
class RouterIntegrationTests(unittest.TestCase):
    def test_asis_reproduces_itself_and_facing_ports_connect_in_a_straight_line(self):
        data = dump()
        # Record the router's own answer for the payload, then require a second replay to match it exactly.
        first = replay.run_router([replay.prepare(data, replay.VARIANTS["asis"], "k")])["k"]
        data["raw_routes"] = first["routes"]
        prepared = replay.prepare(data, replay.VARIANTS["asis"], "k")
        again = replay.run_router([prepared])["k"]
        self.assertTrue(replay.evaluate(data, prepared, again)[0]["same_as_recorded"])

        ports = replay.prepare(data, replay.VARIANTS["ports"], "p")
        route = replay.run_router([ports])["p"]["routes"]["e1"]
        self.assertEqual((route["sourcePoint"]["x"], route["sourcePoint"]["y"]), (5.0, 2.5))
        self.assertEqual((route["targetPoint"]["x"], route["targetPoint"]["y"]), (20.0, 2.5))
        self.assertEqual(route["bendPoints"], [])


if __name__ == "__main__":
    unittest.main()
