from __future__ import annotations

import unittest

from backend.schematic.presentation_geometry import (
    Bounds, GeometryItem, GeometryKind as K, GeometryScene,
)


def item(identifier, kind, owner, bounds, *, net=None, attached=(), estimated=False, segment=None):
    return GeometryItem(
        identifier, kind, owner, Bounds(*bounds), net,
        frozenset(attached), "estimated" if estimated else "measured", segment,
    )


class PresentationGeometryTests(unittest.TestCase):
    def test_body_graphics_allow_own_pins_but_not_foreign_bodies(self) -> None:
        scene = GeometryScene.from_items([
            item("body:a", K.BODY, "part-a", (0, 0, 10, 10)),
            item("pin:a", K.PIN_STROKE, "part-a", (8, 5, 12, 5), net="N"),
            item("body:b", K.BODY, "part-b", (9, 2, 13, 9)),
        ])
        self.assertEqual(
            {tuple(sorted((c.first, c.second))) for c in scene.conflicts()},
            {("body:a", "body:b"), ("body:b", "pin:a")},
        )

    def test_field_crossing_own_graphic_is_not_exempt(self) -> None:
        scene = GeometryScene.from_items([
            item("body", K.BODY, "R7", (0, 0, 4, 6)),
            item("reference", K.REFERENCE, "R7", (1, 1, 3, 2)),
        ])
        self.assertEqual(scene.conflicts()[0].reason, "text_clearance")

    def test_wire_can_only_attach_to_explicit_matching_net_pin(self) -> None:
        pin = item("R7.1", K.PIN_ENDPOINT, "R7", (9.9, 4.9, 10.1, 5.1), net="N")
        wire = item("wire", K.WIRE, "wire-1", (10, 5, 20, 5), net="N",
                    attached=("R7.1",), segment=((10, 5), (20, 5)))
        self.assertEqual(GeometryScene((pin, wire)).conflicts(), ())
        wrong = item("unbound", K.WIRE, "wire-2", (10, 5, 20, 5), net="N",
                     segment=((10, 5), (20, 5)))
        self.assertEqual(
            len(GeometryScene((pin, wrong)).conflicts()), 1
        )
        with self.assertRaises(ValueError):
            GeometryScene((pin, item(
                "foreign", K.WIRE, "wire-3", (10, 5, 20, 5),
                net="DIFFERENT", attached=("R7.1",),
            )))

    def test_same_net_crossings_require_common_explicit_junction(self) -> None:
        junction = item("j", K.JUNCTION, "junction", (4.9, 4.9, 5.1, 5.1), net="N")
        a = item("a", K.WIRE, "a", (0, 5, 10, 5), net="N",
                 attached=("j",), segment=((0, 5), (10, 5)))
        b = item("b", K.WIRE, "b", (5, 0, 5, 10), net="N",
                 attached=("j",), segment=((5, 0), (5, 10)))
        self.assertEqual(GeometryScene((junction, a, b)).conflicts(), ())
        b_unattached = item("b", K.WIRE, "b", (5, 0, 5, 10),
                            net="N", segment=((5, 0), (5, 10)))
        self.assertTrue(any(
            issue.reason == "wire_crossing_without_shared_junction"
            for issue in GeometryScene((junction, a, b_unattached)).conflicts()
        ))

    def test_crossing_bbox_without_wire_segment_intersection_is_ignored(self) -> None:
        a = item("a", K.WIRE, "wire-a", (0, 0, 10, 0), net="N",
                 segment=((0, 0), (10, 0)))
        b = item("b", K.WIRE, "wire-b", (8, -4, 8, -1), net="M",
                 segment=((8, -4), (8, -1)))
        self.assertEqual(GeometryScene((a, b)).conflicts(), ())

    def test_uncertain_text_metrics_are_reported_not_misrepresented(self) -> None:
        scene = GeometryScene.from_items([
            item("pin-name", K.PIN_NAME, "U9", (4, 2, 12, 3), estimated=True),
            item("line", K.WIRE, "wire", (5, 2.5, 10, 2.5)),
        ])
        conflicts = scene.conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].certainty, "estimated")
        self.assertEqual(len(scene.to_dict()["conflicts"]), 1)

    def test_invalid_bounds_and_attachments_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            Bounds(8, 0, 4, 2)
        with self.assertRaises(ValueError):
            GeometryScene.from_items([
                item("line", K.WIRE, "w", (0, 0, 3, 0), net="N", attached=("missing",)),
            ])


if __name__ == "__main__":
    unittest.main()
