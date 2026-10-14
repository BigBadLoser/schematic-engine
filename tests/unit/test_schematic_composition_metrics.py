from __future__ import annotations
import unittest
from backend.schematic.composition_metrics import composition_metrics
from backend.schematic.presentation_geometry import (
    Bounds, GeometryItem, GeometryKind as K, GeometryScene,
)

class CompositionMetricsTests(unittest.TestCase):
    def test_measures_real_wire_length_and_extent_not_cosmetic_score(self):
        scene = GeometryScene.from_items((
            GeometryItem("body", K.BODY, "R1", Bounds(0, 0, 10, 10)),
            GeometryItem("wire1", K.WIRE, "edge1", Bounds(10, 5, 25, 5),
                         segment=((10,5),(25,5))),
            GeometryItem("wire2", K.WIRE, "edge2", Bounds(25,5,25,15),
                         segment=((25,5),(25,15))),
        ))
        metrics = composition_metrics(scene)
        self.assertEqual(metrics["wire_length_mm"], 25.0)
        self.assertEqual(metrics["wire_segment_count"], 2)
        self.assertEqual(metrics["drawing_width_mm"], 25.0)
        self.assertEqual(metrics["drawing_height_mm"], 15.0)
        self.assertEqual(metrics["drawing_bounding_area_mm2"], 375.0)
        self.assertEqual(metrics["assessment"], "diagnostic_only")

    def test_corner_metric_counts_doglegs_not_three_way_junctions(self):
        from backend.schematic.presentation_geometry import (
            Bounds, GeometryItem, GeometryKind as K, GeometryScene,
        )
        def line(name, a, b):
            return GeometryItem(
                name,K.WIRE,"signal",
                Bounds(min(a[0],b[0]),min(a[1],b[1]),
                       max(a[0],b[0]),max(a[1],b[1])),
                segment=(a,b),
            )
        bent=GeometryScene.from_items((
            line("1",(0,0),(5,0)),
            line("2",(5,0),(5,5)),
        ))
        branched=GeometryScene.from_items((
            line("1",(0,0),(5,0)),
            line("2",(5,0),(5,5)),
            line("3",(5,0),(10,0)),
        ))
        self.assertEqual(composition_metrics(bent)["wire_corner_count"],1)
        self.assertEqual(composition_metrics(branched)["wire_corner_count"],0)

    def test_empty_scene_does_not_fabricate_extent(self):
        metrics = composition_metrics(GeometryScene())
        self.assertEqual(metrics["wire_length_mm"], 0.0)
        self.assertEqual(metrics["drawing_bounding_area_mm2"], 0.0)

if __name__ == "__main__":
    unittest.main()
