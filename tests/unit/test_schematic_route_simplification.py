from __future__ import annotations

import unittest

from backend.schematic.route_simplification import (
    connects_or_overlaps, leaves_pin_outward, router_origin, segments_intersect, segments_of, separate_from_foreign,
    simplify_route, repair_diagonals, DiagonalRepairError,
)

GRID = 1.27


class DoglegSimplificationTests(unittest.TestCase):
    def test_removes_height_jumps_when_unobstructed(self):
        noisy=((0.,0.),(4.,0.),(4.,2.),(9.,2.),(9.,0.),(12.,0.))
        fixed=simplify_route(
            noisy,grid_mm=1.27,route_is_safe=lambda path: True,
        )
        self.assertEqual(fixed,((0.,0.),(12.,0.)))

    def test_shortcut_does_not_cross_foreign_wire(self):
        noisy=((0.,0.),(0.,-5.),(10.,-5.),(10.,0.))
        foreign=((5.,-2.),(5.,2.))
        def is_safe(path):
            return not any(
                segments_intersect(segment,foreign)
                for segment in segments_of(path)
            )
        fixed=simplify_route(noisy,grid_mm=1.27,route_is_safe=is_safe)
        self.assertEqual(fixed,noisy)

    def test_collinear_and_t_junction_intersections_detected(self):
        self.assertTrue(segments_intersect(((0,0),(10,0)),((5,-3),(5,3))))
        self.assertTrue(segments_intersect(((0,0),(10,0)),((5,0),(15,0))))
        self.assertFalse(segments_intersect(((0,0),(10,0)),((5,2),(5,5))))

    def test_a_crossing_in_both_interiors_is_not_a_connection(self):
        horizontal=((0,5),(10,5))
        self.assertTrue(segments_intersect(horizontal,((5,0),(5,10))))
        self.assertFalse(connects_or_overlaps(horizontal,((5,0),(5,10))))

    def test_touches_tees_overlaps_and_end_contacts_do_connect(self):
        horizontal=((0,5),(10,5))
        self.assertTrue(connects_or_overlaps(horizontal,((5,5),(5,10))))     # wire end on the interior
        self.assertTrue(connects_or_overlaps(horizontal,((10,0),(10,10))))   # lands on an end point
        self.assertTrue(connects_or_overlaps(horizontal,((0,0),(0,10))))     # other end point
        self.assertTrue(connects_or_overlaps(horizontal,((4,5),(14,5))))     # collinear overlap
        self.assertTrue(connects_or_overlaps(horizontal,((10,5),(14,5))))    # end to end
        self.assertTrue(connects_or_overlaps(((5,0),(5,10)),((5,10),(5,20))))  # vertical, end to end

    def test_segments_that_do_not_meet_are_not_in_conflict(self):
        self.assertFalse(connects_or_overlaps(((0,5),(10,5)),((11,0),(11,10))))
        self.assertFalse(connects_or_overlaps(((0,5),(10,5)),((0,6),(10,6))))   # parallel, one apart

    def test_the_predicate_is_symmetric(self):
        a,b=((0,5),(10,5)),((5,0),(5,10))
        self.assertEqual(connects_or_overlaps(a,b),connects_or_overlaps(b,a))
        c=((5,5),(5,10))
        self.assertEqual(connects_or_overlaps(a,c),connects_or_overlaps(c,a))

    def test_strict_safe_callback_controls_every_change(self):
        original=((0.,0.),(2.,0.),(2.,3.),(7.,3.))
        self.assertEqual(
            simplify_route(original,grid_mm=1.27,route_is_safe=lambda p:False),
            original,
        )


class OwnSymbolEscapeTests(unittest.TestCase):
    """The first and last segment of a route touch their own symbol, so they are let off the body check for it. They are
    only an escape when they leave the way the pin points; a shortcut straight back across the part is not
    (`led_switch`: the connector's CTRL wire was drawn through the connector)."""

    def test_a_segment_that_leaves_the_way_the_pin_points_is_an_escape(self):
        self.assertTrue(leaves_pin_outward((10.0, 5.0), (25.0, 5.0), (1.0, 0.0)))
        self.assertTrue(leaves_pin_outward((10.0, 5.0), (10.0, -4.0), (0.0, -1.0)))

    def test_a_segment_that_runs_back_through_the_part_is_not(self):
        self.assertFalse(leaves_pin_outward((10.0, 5.0), (-8.0, 5.0), (1.0, 0.0)))
        self.assertFalse(leaves_pin_outward((10.0, 5.0), (10.0, 20.0), (0.0, -1.0)))

    def test_one_that_runs_along_the_edge_leaves_nothing_to_excuse(self):
        self.assertFalse(leaves_pin_outward((10.0, 5.0), (10.0, 20.0), (1.0, 0.0)))


class RouterOriginTests(unittest.TestCase):
    """The router adds up lengths, so a problem is posed in a frame that does not depend on where it stands."""

    NODES = ({"x": 106.68, "y": 44.45}, {"x": 120.015, "y": 49.53}, {"x": 99.06, "y": 61.595})

    @staticmethod
    def framed(nodes):
        ox, oy = router_origin(nodes, GRID)
        return sorted((round(node["x"] - ox, 6), round(node["y"] - oy, 6)) for node in nodes)

    def test_the_origin_is_a_grid_corner_at_or_below_every_node(self):
        ox, oy = router_origin(self.NODES, GRID)
        self.assertLessEqual(ox, 99.06 + 1e-9)
        self.assertLessEqual(oy, 44.45 + 1e-9)
        self.assertAlmostEqual(ox / GRID, round(ox / GRID), places=6)
        self.assertAlmostEqual(oy / GRID, round(oy / GRID), places=6)

    def test_a_problem_moved_by_whole_grids_is_posed_alike(self):
        base = self.framed(self.NODES)
        for dx_cells, dy_cells in ((24, 18), (-7, 40), (140, 54), (1, 1), (-30, -20)):
            moved = [{"x": node["x"] + dx_cells * GRID, "y": node["y"] + dy_cells * GRID} for node in self.NODES]
            self.assertEqual(self.framed(moved), base)

    def test_a_node_on_a_grid_line_does_not_move_the_origin_by_float_noise(self):
        nodes = ({"x": 25 * GRID, "y": 17 * GRID}, {"x": 31 * GRID, "y": 30 * GRID})
        base = router_origin(nodes, GRID)
        for cells in range(-60, 220):
            moved = [{"x": node["x"] + cells * GRID, "y": node["y"] + cells * GRID} for node in nodes]
            ox, oy = router_origin(moved, GRID)
            self.assertAlmostEqual(ox, base[0] + cells * GRID, places=6)
            self.assertAlmostEqual(oy, base[1] + cells * GRID, places=6)


if __name__=="__main__":
    unittest.main()


class SnappedOntoForeignWireTests(unittest.TestCase):
    """demo_opamp: the emitter's run snapped onto x=331.47, the line of the collector's R5 wire, and the two nets shorted."""

    EMITTER=((327.66,212.09),(327.66,214.63),(331.47,214.63),(331.47,182.88),(307.34,182.88),(307.34,185.42))
    COLLECTOR=((331.47,177.8),(331.47,196.85))

    def touches(self,segment):
        return connects_or_overlaps(segment,self.COLLECTOR)

    def test_the_collinear_overlap_is_a_connection(self):
        self.assertTrue(any(self.touches(segment) for segment in segments_of(self.EMITTER)))

    def test_slides_the_run_to_the_nearest_free_line(self):
        fixed=separate_from_foreign(
            self.EMITTER,touches_foreign=self.touches,route_is_safe=lambda path: True,grid_mm=GRID,
        )
        self.assertFalse(any(self.touches(segment) for segment in segments_of(fixed)))
        self.assertEqual((fixed[0],fixed[-1]),(self.EMITTER[0],self.EMITTER[-1]))
        self.assertEqual(len(fixed),len(self.EMITTER))
        self.assertAlmostEqual(abs(fixed[2][0]-331.47),GRID)

    def test_respects_the_safety_predicate_and_leaves_a_blocked_route_alone(self):
        self.assertEqual(
            separate_from_foreign(
                self.EMITTER,touches_foreign=self.touches,route_is_safe=lambda path: False,grid_mm=GRID,
            ),
            self.EMITTER,
        )

    def test_a_route_touching_nothing_is_returned_as_it_came(self):
        route=((0.,0.),(0.,5.),(10.,5.),(10.,0.))
        self.assertEqual(
            separate_from_foreign(route,touches_foreign=lambda s: False,route_is_safe=lambda p: True,grid_mm=GRID),
            route,
        )


class DiagonalRepairTests(unittest.TestCase):
    ROUTE=((0.,0.),(6.35,0.),(6.35,3.81),(12.7,7.62))

    def test_first_corner_used_when_safe(self):
        fixed,count=repair_diagonals(self.ROUTE,route_is_safe=lambda path: True,grid_mm=GRID)
        self.assertEqual(count,1)
        self.assertEqual(fixed,((0.,0.),(6.35,0.),(6.35,3.81),(12.7,3.81),(12.7,7.62)))

    def test_second_corner_when_first_unsafe(self):
        first=(12.7,3.81)
        fixed,count=repair_diagonals(self.ROUTE,route_is_safe=lambda path: first not in path,grid_mm=GRID)
        self.assertEqual(count,1)
        self.assertEqual(fixed,((0.,0.),(6.35,0.),(6.35,7.62),(12.7,7.62)))

    def test_error_when_neither_corner_safe(self):
        with self.assertRaises(DiagonalRepairError) as caught:
            repair_diagonals(self.ROUTE,route_is_safe=lambda path: False,grid_mm=GRID)
        self.assertEqual(caught.exception.segment,((6.35,3.81),(12.7,7.62)))

    def test_orthogonal_route_unchanged(self):
        route=((0.,0.),(5.,0.),(5.,5.))
        self.assertEqual(repair_diagonals(route,route_is_safe=lambda path: False,grid_mm=GRID),(route,0))


class SimplifyBeforeRepairTests(unittest.TestCase):
    def test_diagonal_removed_by_shortcut_needs_no_repair(self):
        route=((0.,0.),(5.08,0.),(7.62,2.54),(12.7,2.54),(12.7,0.),(20.32,0.))
        fixed=simplify_route(route,grid_mm=GRID,route_is_safe=lambda path: True)
        has_diag=any(a[0]!=b[0] and a[1]!=b[1] for a,b in zip(fixed,fixed[1:]))
        if has_diag:
            fixed,_=repair_diagonals(fixed,route_is_safe=lambda path: True,grid_mm=GRID)
        self.assertFalse(any(a[0]!=b[0] and a[1]!=b[1] for a,b in zip(fixed,fixed[1:])))
