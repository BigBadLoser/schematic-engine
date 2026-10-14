"""KPI maths on hand-computable geometries (no KiCad, no network)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import kpi  # noqa: E402


def geometry(wires=(), junctions=(), pins=None, bodies=()):
    return kpi.Geometry(list(wires), list(junctions), dict(pins or {}), list(bodies))


class MergeTests(unittest.TestCase):
    def test_overlapping_and_duplicate_segments_count_once(self):
        merged = kpi.merge_segments([((0, 0), (10, 0)), ((5, 0), (15, 0)), ((10, 0), (0, 0))])
        self.assertEqual(merged, [((0.0, 0.0), (15.0, 0.0))])

    def test_touching_segments_join_but_gaps_do_not(self):
        self.assertEqual(len(kpi.merge_segments([((0, 0), (5, 0)), ((5, 0), (9, 0))])), 1)
        self.assertEqual(len(kpi.merge_segments([((0, 0), (5, 0)), ((6, 0), (9, 0))])), 2)

    def test_non_orthogonal_segment_is_rejected(self):
        with self.assertRaises(ValueError):
            kpi.merge_segments([((0, 0), (3, 4))])


class ConnectivityTests(unittest.TestCase):
    def test_l_shaped_two_pin_net_is_wired_with_unit_excess(self):
        g = geometry(
            wires=[((0, 0), (10, 0)), ((10, 0), (10, 5))],
            pins={"A.1": ((0, 0), "A"), "B.1": ((10, 5), "B")},
        )
        result = kpi.compute(g, {"N": ["A.1", "B.1"]})
        self.assertEqual(result["wire_length_mm"], 15.0)
        self.assertEqual(result["corners"], 1)
        self.assertEqual(result["nets"]["wired"], 1)
        self.assertEqual(result["excess_ratio"], 1.0)

    def test_duplicate_wires_are_reported_but_not_counted_twice(self):
        g = geometry(
            wires=[((0, 0), (10, 0)), ((5, 0), (15, 0)), ((0, 0), (10, 0))],
            pins={"A.1": ((0, 0), "A"), "B.1": ((15, 0), "B")},
        )
        result = kpi.compute(g, {"N": ["A.1", "B.1"]})
        self.assertEqual(result["wire_length_mm"], 15.0)
        self.assertEqual(result["duplicate_wire_mm"], 15.0)

    def test_crossing_without_junction_is_not_a_connection(self):
        g = geometry(wires=[((0, 5), (10, 5)), ((5, 0), (5, 10))])
        result = kpi.compute(g, {})
        self.assertEqual(result["crossings"], 1)
        self.assertEqual(result["crossings_between_components"], 1)
        self.assertEqual(result["t_contacts"], 0)

    def test_junction_turns_a_crossing_into_a_connection(self):
        g = geometry(wires=[((0, 5), (10, 5)), ((5, 0), (5, 10))], junctions=[(5, 5)])
        self.assertEqual(kpi.compute(g, {})["crossings"], 0)

    def test_three_pin_tree_with_a_tee_has_excess_one_when_optimal(self):
        g = geometry(
            wires=[((0, 0), (10, 0)), ((5, 0), (5, 5))],
            pins={"A.1": ((0, 0), "A"), "B.1": ((10, 0), "B"), "C.1": ((5, 5), "C")},
        )
        result = kpi.compute(g, {"N": ["A.1", "B.1", "C.1"]})
        self.assertEqual(result["t_contacts"], 1)
        self.assertEqual(result["nets"]["wired"], 1)
        self.assertEqual(result["excess_ratio"], 1.0)

    def test_pins_with_separate_stubs_are_symbolic(self):
        g = geometry(
            wires=[((0, 0), (0, 3)), ((20, 0), (20, 3))],
            pins={"A.1": ((0, 0), "A"), "B.1": ((20, 0), "B")},
        )
        result = kpi.compute(g, {"GND": ["A.1", "B.1"]})
        self.assertEqual(result["nets"]["symbolic"], 1)
        self.assertEqual(result["excess_ratio"], None)

    def test_unwired_net_is_unrouted(self):
        g = geometry(pins={"A.1": ((0, 0), "A"), "B.1": ((20, 0), "B")})
        self.assertEqual(kpi.compute(g, {"N": ["A.1", "B.1"]})["nets"]["unrouted"], 1)

    def test_component_holding_pins_of_two_nets_is_a_short(self):
        g = geometry(
            wires=[((0, 0), (10, 0))],
            pins={"A.1": ((0, 0), "A"), "B.1": ((10, 0), "B")},
        )
        result = kpi.compute(g, {"ONE": ["A.1"], "TWO": ["B.1"]})
        self.assertEqual(result["shorted_nets"], [["ONE", "TWO"]])


class StubTests(unittest.TestCase):
    def test_wire_components_touching_exactly_one_declared_pin_are_stubs(self):
        g = geometry(
            wires=[((0, 0), (0, 5.08)), ((20, 0), (30, 0)), ((30, 0), (30, 8)), ((50, 0), (50, 3))],
            pins={
                "A.1": ((0, 0), "A"), "#PWR1.1": ((0, 5.08), "#PWR1"),   # stub: one declared pin + a power symbol
                "B.1": ((20, 0), "B"), "C.1": ((30, 8), "C"),            # a two-pin net: not a stub
                "#PWR2.1": ((50, 3), "#PWR2"),                          # no declared pin: not a stub
            },
        )
        stubs = kpi.compute(g, {"GND": ["A.1"], "N": ["B.1", "C.1"]})["stubs"]
        self.assertEqual(stubs, {"count": 1, "length_mm": 5.08, "per_pin_mm": 5.08, "max_mm": 5.08})

    def test_no_stubs_reports_none_not_zero_division(self):
        stubs = kpi.compute(geometry(), {})["stubs"]
        self.assertEqual((stubs["count"], stubs["per_pin_mm"], stubs["max_mm"]), (0, None, None))


class CollisionAndSpacingTests(unittest.TestCase):
    def test_wire_through_a_foreign_body_is_a_foreign_hit(self):
        g = geometry(
            wires=[((0, 0), (10, 0))],
            pins={"A.1": ((0, 0), "A"), "B.1": ((10, 0), "B")},
            bodies=[("X", (4, -1, 6, 1))],
        )
        result = kpi.compute(g, {"N": ["A.1", "B.1"]})
        self.assertEqual((result["body_hits"], result["body_hits_foreign"]), (1, 1))

    def test_wire_through_its_own_symbol_is_a_hit_but_not_foreign(self):
        g = geometry(
            wires=[((0, 0), (10, 0))],
            pins={"A.1": ((0, 0), "A"), "B.1": ((10, 0), "B")},
            bodies=[("A", (4, -1, 6, 1))],
        )
        result = kpi.compute(g, {"N": ["A.1", "B.1"]})
        self.assertEqual((result["body_hits"], result["body_hits_foreign"]), (1, 0))

    def test_wire_along_a_body_edge_is_not_a_hit(self):
        g = geometry(wires=[((0, 1), (10, 1))], bodies=[("X", (4, -1, 6, 1))])
        self.assertEqual(kpi.compute(g, {})["body_hits"], 0)

    def test_overlapping_bodies_are_counted_once_per_pair(self):
        g = geometry(bodies=[("A", (0, 0, 4, 4)), ("B", (3, 3, 7, 7)), ("C", (10, 10, 12, 12))])
        self.assertEqual(kpi.compute(g, {})["body_overlaps"], 1)

    def test_passive_spacing_reproduces_the_inflated_footprint_pitch(self):
        # Two vertical capacitors 22.86 mm apart (2 x 10.16 + 2 x 1.27), 4.06 mm wide bodies.
        def part(ref, cx):
            return (
                {f"{ref}.1": ((cx, -3.81), ref), f"{ref}.2": ((cx, 3.81), ref)},
                (ref, (cx - 2.03, -2.0, cx + 2.03, 2.0)),
            )

        pins_a, body_a = part("C3", 0.0)
        pins_b, body_b = part("C4", 22.86)
        g = geometry(pins={**pins_a, **pins_b}, bodies=[body_a, body_b])
        passives = kpi.compute(g, {})["passives"]
        self.assertEqual(passives["count"], 2)
        self.assertEqual(passives["nn_center_median_mm"], 22.86)
        self.assertEqual(passives["nn_gap_median_mm"], 18.8)

    def test_connectors_and_power_symbols_are_not_passives(self):
        g = geometry(
            pins={"J1.1": ((0, 0), "J1"), "J1.2": ((0, 2.54), "J1"), "#PWR1.1": ((9, 0), "#PWR1"), "#PWR1.2": ((9, 9), "#PWR1")},
            bodies=[("J1", (0, 0, 2, 2)), ("#PWR1", (9, 0, 10, 1))],
        )
        self.assertEqual(kpi.compute(g, {})["passives"]["count"], 0)


class JogTests(unittest.TestCase):
    def jogs(self, *wires):
        return kpi.compute(geometry(wires=list(wires)), {})["jogs"]

    def test_a_short_sideways_step_is_a_jog(self):
        # right 10, down 1.27, right 10: a Z with a 1.27 mm middle leg
        self.assertEqual(self.jogs(((0, 0), (10, 0)), ((10, 0), (10, 1.27)), ((10, 1.27), (20, 1.27))), 1)

    def test_a_long_middle_leg_is_a_dogleg_not_a_jog(self):
        self.assertEqual(self.jogs(((0, 0), (10, 0)), ((10, 0), (10, 5.08)), ((10, 5.08), (20, 5.08))), 0)

    def test_an_l_shape_with_a_short_leg_is_not_a_jog(self):
        self.assertEqual(self.jogs(((0, 0), (10, 0)), ((10, 0), (10, 1.27))), 0)

    def test_a_straight_wire_has_no_jogs(self):
        self.assertEqual(self.jogs(((0, 0), (20, 0))), 0)


class PlacementKpiTests(unittest.TestCase):
    SCENE = {"items": [
        {"id": "A.1:terminal", "kind": "pin_endpoint", "owner": "A", "bounds": {"left": 0, "top": 0, "right": 0, "bottom": 0}},
        {"id": "B.1:terminal", "kind": "pin_endpoint", "owner": "B", "bounds": {"left": 10, "top": 5, "right": 10, "bottom": 5}},
        {"id": "C.1:terminal", "kind": "pin_endpoint", "owner": "C", "bounds": {"left": 4, "top": 20, "right": 4, "bottom": 20}},
        {"id": "A:graphic", "kind": "body", "owner": "A", "bounds": {"left": -1, "top": -1, "right": 1, "bottom": 1}},
        {"id": "wire:0:0", "kind": "wire", "owner": "wire:0", "segment": [[0, 0], [9, 0]],
         "bounds": {"left": 0, "top": 0, "right": 9, "bottom": 0}},
    ]}

    def test_scene_dict_adapter_reads_pins_bodies_and_wires(self):
        g = kpi.geometry_from_scene_dict(self.SCENE)
        self.assertEqual(g.pins["B.1"], ((10, 5), "B"))
        self.assertEqual(g.bodies, [("A", (-1, -1, 1, 1))])
        self.assertEqual(len(g.wires), 1)

    def test_hpwl_splits_signal_from_all_nets_and_ignores_wires(self):
        g = kpi.geometry_from_scene_dict(self.SCENE)
        nets = {"SIG": ["A.1", "B.1"], "GND": ["A.1", "C.1"]}
        result = kpi.placement_kpis(g, nets, {"SIG"})
        self.assertEqual(result["hpwl_signal_mm"], 15.0)        # 10 + 5
        self.assertEqual(result["hpwl_all_mm"], 15.0 + 24.0)    # plus GND: 4 + 20
        self.assertEqual(result["body_overlaps"], 0)


class RouterClearanceTests(unittest.TestCase):
    """The router needs 2.54 (portal) + 1.905 (keep-out) = 4.445 mm between a pin tip and a body it points at."""

    @staticmethod
    def facing(body_left, direction=(1.0, 0.0), body_owner="B"):
        return kpi.Geometry(
            pins={"A.1": ((0.0, 0.0), "A")},
            bodies=[(body_owner, (body_left, -2.0, body_left + 2.0, 2.0))],
            pin_dirs={"A.1": direction},
        )

    def test_a_portal_inside_a_neighbours_keep_out_is_trapped(self):
        result = kpi.router_clearance(self.facing(4.0))   # keep-out starts at 2.095 < portal at 2.54
        self.assertEqual((result["trapped"], result["trapped_pins"]), (1, ["A.1"]))
        self.assertEqual(result["min_outward_run_mm"], 4.0)
        self.assertEqual(result["needed_run_mm"], 4.445)

    def test_enough_room_is_not_trapped(self):
        result = kpi.router_clearance(self.facing(4.5))
        self.assertEqual(result["trapped"], 0)
        self.assertEqual(result["min_outward_run_mm"], 4.5)

    def test_a_pin_pointing_away_from_the_body_is_not_trapped(self):
        self.assertEqual(kpi.router_clearance(self.facing(4.0, direction=(-1.0, 0.0)))["trapped"], 0)

    def test_a_pins_own_body_never_traps_it(self):
        self.assertEqual(kpi.router_clearance(self.facing(1.0, body_owner="A"))["trapped"], 0)

    def test_pins_without_a_known_direction_are_skipped_not_guessed(self):
        geometry = self.facing(4.0)
        geometry.pin_dirs.clear()
        result = kpi.router_clearance(geometry)
        self.assertEqual((result["trapped"], result["pins_with_direction"]), (0, 0))

    def test_outward_direction_comes_from_which_end_of_the_stroke_is_the_tip(self):
        self.assertEqual(kpi.outward_direction((0.0, 5.0), (0.0, 0.0, 0.0, 5.0)), (0.0, 1.0))   # tip at the bottom
        self.assertEqual(kpi.outward_direction((0.0, 0.0), (0.0, 0.0, 0.0, 5.0)), (0.0, -1.0))  # tip at the top
        self.assertEqual(kpi.outward_direction((0.0, 3.0), (0.0, 3.0, 4.0, 3.0)), (-1.0, 0.0))  # tip at the left
        self.assertEqual(kpi.outward_direction((4.0, 3.0), (0.0, 3.0, 4.0, 3.0)), (1.0, 0.0))   # tip at the right

    def test_scene_dict_adapter_reads_pin_directions_from_strokes(self):
        scene = {"items": [
            {"id": "A.1:terminal", "kind": "pin_endpoint", "owner": "A", "bounds": {"left": 0, "top": 0, "right": 0, "bottom": 0}},
            {"id": "A.1:stroke", "kind": "pin_stroke", "owner": "A", "bounds": {"left": 0, "top": 0, "right": 3.81, "bottom": 0}},
            {"id": "B:graphic", "kind": "body", "owner": "B", "bounds": {"left": -6, "top": -2, "right": -4, "bottom": 2}},
        ]}
        geometry = kpi.geometry_from_scene_dict(scene)
        self.assertEqual(geometry.pin_dirs, {"A.1": (-1.0, 0.0)})   # the tip is at the left end: outward is west
        self.assertEqual(kpi.router_clearance(geometry)["trapped"], 1)  # and the body at x=-4 is 4 mm away


class SceneAdapterTests(unittest.TestCase):
    def test_real_scene_reader_feeds_the_kpis(self):
        sheet = (
            '(kicad_sch (version 20250114) (generator "eeschema") (paper "A4") (lib_symbols)\n'
            ' (wire (pts (xy 10 10) (xy 20 10)) (stroke (width 0) (type default)) (uuid "a"))\n'
            ' (wire (pts (xy 20 10) (xy 20 15)) (stroke (width 0) (type default)) (uuid "b"))\n'
            ' (junction (at 20 10) (diameter 0) (color 0 0 0 0) (uuid "c"))\n'
            ')\n'
        )
        from backend.schematic.kicad_geometry import measured_scene_from_kicad

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.kicad_sch"
            path.write_text(sheet, encoding="utf-8")
            scene, _ = measured_scene_from_kicad(path)
        result = kpi.compute(kpi.geometry_from_scene(scene), {})
        self.assertEqual(result["wire_length_mm"], 15.0)
        self.assertEqual(result["corners"], 1)


if __name__ == "__main__":
    unittest.main()
