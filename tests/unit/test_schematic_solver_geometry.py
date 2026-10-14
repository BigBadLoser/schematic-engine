"""Regression checks for the shared CP-SAT/libavoid presentation keepouts."""
from __future__ import annotations

import unittest
from pathlib import Path

from backend.schematic.geometry_constraints import (
    external_text_reservations,
    overlap_pairs,
    relocated_reservations,
    reservations_for_owners,
    segment_intersects_reservation,
)
from backend.schematic.presentation_geometry import (
    Bounds, GeometryItem, GeometryKind as K, GeometryScene,
)


def geom(identity: str, kind: K, owner: str, box: tuple[float, float, float, float]):
    return GeometryItem(identity, kind, owner, Bounds(*box))


class SharedSolverGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scene = GeometryScene.from_items([
            geom("filter:body", K.BODY, "A", (0, 0, 4, 4)),
            geom("filter:ref", K.REFERENCE, "A", (4, 1, 8, 3)),
            geom("neighbor:body", K.BODY, "B", (11, 0, 15, 4)),
            geom("neighbor:value", K.VALUE, "B", (8.5, 1, 10.5, 3)),
            geom("signal:label", K.NET_LABEL, "wire-label", (18, 4, 22, 6)),
            geom("wire:1", K.WIRE, "wire", (0, 8, 20, 8)),
        ])

    def test_cp_sat_candidate_reserves_translated_text_as_well_as_body(self) -> None:
        current = reservations_for_owners(self.scene, ["A"])
        self.assertEqual({r.kind for r in current}, {K.BODY, K.REFERENCE})
        target = relocated_reservations(
            current, old_origin=(0, 0), new_origin=(2, 0),
        )
        b = reservations_for_owners(self.scene, ["B"])
        # The body clears B, but the moved "A" reference meets B's value.
        self.assertFalse(overlap_pairs(target[:1], b))
        self.assertTrue(overlap_pairs(target, b))
        distant = relocated_reservations(
            current, old_origin=(0, 0), new_origin=(-20, 0),
        )
        self.assertFalse(overlap_pairs(distant, b))

    def test_rotated_reservations_follow_page_coordinate_convention(self) -> None:
        box = relocated_reservations(
            reservations_for_owners(self.scene, ["A"]),
            old_origin=(0, 0), new_origin=(0, 0),
            rotation_delta_degrees=90,
        )[0].bounds
        self.assertEqual(box, Bounds(0, -4, 4, 0))
        with self.assertRaises(ValueError):
            relocated_reservations(
                reservations_for_owners(self.scene, ["A"]),
                old_origin=(0, 0), new_origin=(0, 0),
                rotation_delta_degrees=15,
            )

    def test_libavoid_obstacles_include_external_fields_and_labels(self) -> None:
        extra = external_text_reservations(
            self.scene, ("A", "B"), {
                "A": Bounds(0, 0, 4, 4),
                "B": Bounds(11, 0, 15, 4),
            },
        )
        self.assertEqual(
            {r.identity for r in extra},
            {"filter:ref", "neighbor:value", "signal:label"},
        )
        self.assertEqual(len([r for r in extra if r.kind == K.WIRE]), 0)
        self.assertTrue(all(float(node["width"]) > 0 for node in (
            r.as_router_node(1.27) for r in extra
        )))

    def test_route_postcondition_detects_field_crossing(self) -> None:
        fields = external_text_reservations(
            self.scene, ("A", "B"), {"A": Bounds(0, 0, 4, 4)},
        )
        reference = next(x for x in fields if x.identity == "filter:ref")
        self.assertTrue(segment_intersects_reservation(
            (6, -1), (6, 10), reference,
        ))
        self.assertFalse(segment_intersects_reservation(
            (9, -1), (9, 10), reference,
        ))
        with self.assertRaises(ValueError):
            segment_intersects_reservation((1, 1), (2, 2), reference)

    def test_solver_paths_import_shared_geometry(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (
            root / "backend" / "kicad" / "mcp_extended_server.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "from backend.schematic.kicad_geometry import measured_scene_from_kicad",
            source,
        )
        self.assertIn("from backend.schematic.geometry_constraints import", source)
        self.assertIn("presentation_constraints", source)

    def test_no_circuit_fixture_name_in_the_workflow(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for path in ("backend/schematic/direct_workflow.py",):
            source = (root / path).read_text(encoding="utf-8")
            for name in ('"mcu_core"', '"mcu_cluster"', '"power_entry"'):
                self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
