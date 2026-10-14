"""The plan seam, tested with geometry rather than circuit-specific fixtures."""
import unittest
from types import SimpleNamespace as Data
from unittest.mock import patch

from backend.schematic.ladder_solve import (LadderInfeasible, LadderSolveError, _close, _interior_crossing, _on_segment,
                                            _overlap, _point_off_segment, legalise, legalise_with_status)
from backend.schematic.ladder_plan import Plan, Row, Column
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Connection, OrientedGeometry, PinGeometry, PlacedSymbol, Placement

O = Orientation(0)


def symbol(ref, vertical=False, extra=None):
    pins = ({"1": PinGeometry((0, -3.81), (0, -1)), "2": PinGeometry((0, 3.81), (0, 1))}
            if vertical else
            {"1": PinGeometry((-3.81, 0), (-1, 0)), "2": PinGeometry((3.81, 0), (1, 0))})
    pins.update(extra or {})
    box = (-1.27, -3.81, 1.27, 3.81) if vertical else (-3.81, -1.27, 3.81, 1.27)
    return PlacedSymbol(ref, (100, 100), O, {O: OrientedGeometry(pins, box)})


def row(name, order, above=None, kind="chain"):
    return Data(name=name, kind=kind, above=above, order=tuple(order))


def plan(symbols, rows=(), hangs=(), spans=(), columns=(), pins=None):
    return Data(rows=tuple(rows), hangs=tuple(hangs), spans=tuple(spans), columns=tuple(columns),
                orientations={s.reference: O for s in symbols}, pins=pins or {})


def tip(symbols, placements, ref, number):
    s = next(s for s in symbols if s.reference == ref)
    p = placements[ref]
    local = s.geometry[p.orientation].pins[number].point
    return tuple(p.position[i] + local[i] for i in (0, 1))


class LadderSolveTests(unittest.TestCase):
    def chain(self):
        symbols = [symbol("A"), symbol("B"), symbol("H1", True), symbol("H2", True)]
        net = Connection("signal", (("A", "2"), ("B", "1"), ("H1", "1"), ("H2", "1")))
        hangs = [Data(reference=f"H{i + 1}", wire="signal", host="B.1", side="below", orientation=O, rank=i)
                 for i in range(2)]
        p = plan(symbols, [row("main", ["A", "B"])], hangs, pins={"A": "A.2", "B": "B.1"})
        return p, symbols, [net]

    def test_chain_hangs_on_wire_in_rank_order(self):
        p, symbols, nets = self.chain()
        result = legalise(p, symbols, nets)
        ax, ay = tip(symbols, result, "A", "2")
        bx, by = tip(symbols, result, "B", "1")
        h1x, h1y = tip(symbols, result, "H1", "1")
        h2x, h2y = tip(symbols, result, "H2", "1")
        self.assertEqual((ay, by, h1y, h2y), (0, 0, 0, 0))
        self.assertLessEqual(ax, h2x)
        self.assertLess(h2x, h1x)
        self.assertLessEqual(h1x, bx)
        self.assertGreater(result["H1"].position[1], 0)
        self.assertEqual(result.status, "cp-sat")
        for placement in result.values():
            for coordinate in placement.position:
                self.assertAlmostEqual(coordinate / 1.27, round(coordinate / 1.27))

    def test_power_exit_reserves_full_reach(self):
        symbols = [symbol("A", True), symbol("B", True)]
        p = plan(symbols, [row("main", ["A"])],
                 columns=[Data(pin="A.2", order=("B",), orientations=(O,))], pins={"A": "1"})
        rail = Connection("rail", (("A", "2"), ("B", "1")), style="power")
        result = legalise(p, symbols, [rail])
        exit_end = tip(symbols, result, "A", "2")[1] + 7.62
        body_top = result["B"].position[1] - 2.54
        self.assertGreaterEqual(body_top, exit_end - 1e-6)

    def test_power_exit_has_width_against_foreign_body(self):
        # A's rail reservation is 2.54 mm wide and, like any conductor, keeps one gap from B's body.
        a = symbol("A", extra={"3": PinGeometry((8.89, 0), (0, 1))})
        symbols = [a, symbol("B")]
        p = plan(symbols, [row("main", ["A", "B"])], pins={"A": "1", "B": "1"})
        result = legalise(p, symbols, [Connection("rail", (("A", "3"),), style="power")])
        bleft = result["B"].position[0] - 2.54
        self.assertTrue(bleft >= 8.89 + 1.27 + 2.54 - 1e-6 or
                        result["B"].position[0] + 2.54 <= 8.89 - 1.27 - 2.54 + 1e-6)

    def test_interior_crossings_are_soft_but_endpoints_are_not_crossings(self):
        from ortools.sat.python import cp_model
        RANK_CROSSING_MM = 8.0       # any weight: the objective is this times the number of crossings
        for vx, vy0, vy1, expected in ((5, -5, 5, 1), (0, -5, 5, 0),
                                      (5, 0, 5, 0), (5, 1, 5, 0), (5, 0, 0, 0)):
            with self.subTest(vx=vx, vy0=vy0, vy1=vy1):
                model = cp_model.CpModel()
                crossing = _interior_crossing(model, (0, 0), (10, 0), (vx, vy0), (vx, vy1))
                model.Minimize(round(RANK_CROSSING_MM * 1000) * crossing)
                solver = cp_model.CpSolver()
                solver.parameters.num_search_workers = 1
                self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
                self.assertEqual(solver.Value(crossing), expected)
                self.assertEqual(solver.ObjectiveValue(), 8000 * expected)

    def test_harness_adapter_returns_placements_and_status(self):
        p, symbols, nets = self.chain()
        result = legalise_with_status(p, symbols, nets)
        self.assertEqual(set(result.placements), set(p.orientations))
        self.assertEqual(result.status, result.placements.status)

    def test_foreign_pin_or_wire_endpoint_on_conductor_is_hard(self):
        from ortools.sat.python import cp_model
        for horizontal in (True, False):
            for coordinate, legal in ((-1, True), (0, False), (5, False), (10, False), (11, True)):
                with self.subTest(horizontal=horizontal, coordinate=coordinate):
                    model = cp_model.CpModel()
                    assumption = model.NewBoolVar("short clearance")
                    model.AddAssumption(assumption)
                    point = (coordinate, 0) if horizontal else (0, coordinate)
                    end = (10, 0) if horizontal else (0, 10)
                    _point_off_segment(model, point, (0, 0), end, horizontal, assumption, "short")
                    solver = cp_model.CpSolver()
                    solver.parameters.num_search_workers = 1
                    expected = cp_model.OPTIMAL if legal else cp_model.INFEASIBLE
                    self.assertEqual(solver.Solve(model), expected)

    def test_empty_orientation_map_does_not_hide_unplanned_structure(self):
        p = plan([], [row("main", ["missing"])])
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, [], [])
        self.assertEqual(caught.exception.groups, ("plan membership",))
        self.assertEqual(caught.exception.parts, ("missing",))

    def test_lane_keeps_its_hub_pin_height_and_side(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                a = symbol("A", extra={"3": PinGeometry((direction * 3.81, 7.62), (direction, 0))})
                symbols = [a, symbol("L")]
                near = "1" if direction > 0 else "2"
                p = plan(symbols, [row("main", ["A"]), row("lane", ["L"], "A.3", "lane")],
                         pins={"A": "1", "L": near})
                result = legalise(p, symbols, [Connection("lane", (("A", "3"), ("L", near)))])
                host, signal = tip(symbols, result, "A", "3"), tip(symbols, result, "L", near)
                self.assertAlmostEqual(signal[1], host[1])
                self.assertGreaterEqual(direction * (signal[0] - host[0]), 2.54 - 1e-6)

    def test_stacked_pair_keeps_row_order(self):
        symbols = [symbol("A"), symbol("B")]
        p = plan(symbols, [row("upper", ["A"]), row("lower", ["B"], "upper")],
                 pins={"A": "A.1", "B": "B.1"})
        result = legalise(p, symbols, [])
        # Two 2.54 mm bodies one gap (2.54 mm) apart: the gap is measured once, not grown on both bodies.
        self.assertAlmostEqual(result["B"].position[1] - result["A"].position[1], 5.08)

    def test_rail_pin_reserves_pitch(self):
        symbols = [symbol("A", extra={"3": PinGeometry((3.81, 2.54), (1, 0))}), symbol("B")]
        p = plan(symbols, [row("main", ["A", "B"])], pins={"A": "A.2", "B": "B.1"})
        signal = Connection("signal", (("A", "2"), ("B", "1")))
        power = Connection("rail", (("A", "3"),), style="power")
        plain = legalise(p, symbols, [signal])
        reserved = legalise(p, symbols, [signal, power])
        # The rail pin's stub (7.62 mm out from its tip) keeps one gap from B's body, which it also clears
        # by the slot of one pitch beyond A's body and a gap.
        self.assertGreater(reserved["B"].position[0], plain["B"].position[0])
        self.assertGreaterEqual(reserved["B"].position[0] - 2.54, 3.81 + 7.62 + 2.54 - 1e-7)
        wider = legalise(p, symbols, [signal, power], pitch_mm=10.16)
        self.assertGreaterEqual(wider["B"].position[0] - 2.54, 2.54 + 2.54 + 10.16 - 1e-7)
        self.assertGreater(wider["B"].position[0], reserved["B"].position[0])

    def test_infeasible_hang_names_group_and_part(self):
        p, symbols, nets = self.chain()
        # Pin 2 is below this body's centre; it cannot hang below its wire.
        p.hangs = (Data(reference="H1", wire="signal", host="B.1", side="below", orientation=O, rank=0),)
        del p.orientations["H2"]
        nets = [Connection("signal", (("A", "2"), ("B", "1"), ("H1", "2")))]
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, symbols, nets)
        self.assertIn("hang side", caught.exception.groups)
        self.assertIn("H1", caught.exception.parts)
        self.assertIn("infeasible", caught.exception.status)

    def test_two_solves_and_shuffled_inputs_are_identical(self):
        p, symbols, nets = self.chain()
        first = legalise(p, symbols, nets)
        self.assertEqual(first, legalise(p, symbols, nets))
        self.assertEqual(first, legalise(p, list(reversed(symbols)), list(reversed(nets))))
        p.orientations = dict(reversed(list(p.orientations.items())))
        p.hangs = tuple(reversed(p.hangs))
        self.assertEqual(first, legalise(p, symbols, nets))

    def test_vertical_span_allows_lower_drop(self):
        symbols = [symbol("A"), symbol("B"), symbol("R", True)]
        p = plan(symbols, [row("top", ["A"]), row("bottom", ["B"], "top", "trunk")],
                 spans=[Data(reference="R", from_wire="top", to_wire="bottom", orientation=O)],
                 pins={"A": "A.2", "B": "B.2"})
        nets = [Connection("top", (("A", "2"), ("R", "1"))),
                Connection("bottom", (("B", "2"), ("R", "2")))]
        result = legalise(p, symbols, nets)
        self.assertEqual(tip(symbols, result, "R", "1")[1], 0)
        self.assertLessEqual(tip(symbols, result, "R", "2")[1], tip(symbols, result, "B", "2")[1])

    def test_column_is_below_host_and_in_order(self):
        symbols = [symbol("A", extra={"3": PinGeometry((0, 3.81), (0, 1))}),
                   symbol("C", True), symbol("D", True)]
        p = plan(symbols, [row("main", ["A"])],
                 columns=[Data(pin="A.3", order=("C", "D"), orientations=(O, O))], pins={"A": "A.1"})
        result = legalise(p, symbols, [Connection("drive", (("A", "3"), ("C", "1"))),
                                           Connection("next", (("C", "2"), ("D", "1")))])
        self.assertEqual(result["C"].position[0], result["D"].position[0])
        self.assertGreater(tip(symbols, result, "C", "1")[1], tip(symbols, result, "A", "3")[1])
        self.assertGreater(tip(symbols, result, "D", "1")[1], tip(symbols, result, "C", "2")[1])

    def test_empty_plan_and_missing_orientation(self):
        self.assertEqual(legalise(plan([]), [], []), {})
        p = plan([symbol("A")])
        p.orientations["A"] = Orientation(90)
        with self.assertRaisesRegex(LadderInfeasible, "orientation: A"):
            legalise(p, [symbol("A")], [])

    def test_wire_cannot_run_through_unjoined_row_part(self):
        symbols = [symbol("A"), symbol("N"), symbol("B")]
        p = plan(symbols, [row("main", ["A", "N", "B"])],
                 pins={"A": "A.2", "N": "N.1", "B": "B.1"})
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, symbols, [Connection("signal", (("A", "2"), ("B", "1")))])
        self.assertTrue({"wire-body:signal", "wire-pin:signal"} & set(caught.exception.groups))
        self.assertIn("N", caught.exception.parts)

    def test_feedback_span_is_above_both_wires(self):
        symbols = [symbol("A"), symbol("B"), symbol("F")]
        p = plan(symbols, [row("main", ["A", "B"])],
                 spans=[Data(reference="F", from_wire="input", to_wire="output", orientation=O)],
                 pins={"A": "A.2", "B": "B.1"})
        result = legalise(p, symbols, [Connection("input", (("A", "2"), ("F", "1"))),
                                           Connection("output", (("B", "1"), ("F", "2")))])
        self.assertLess(result["F"].position[1], 0)
        self.assertLessEqual(tip(symbols, result, "A", "2")[0], tip(symbols, result, "F", "1")[0])
        self.assertGreaterEqual(tip(symbols, result, "B", "1")[0], tip(symbols, result, "F", "2")[0])

    def test_standalone_member_has_fixed_orientation_and_legal_anchor(self):
        result = legalise(plan([symbol("A")]), [symbol("A")], [])
        self.assertEqual(result["A"].position, (0, 0))
        self.assertEqual(result["A"].orientation, O)

    def test_budget_exhaustion_is_not_reported_as_infeasibility(self):
        p, symbols, nets = self.chain()
        with patch("backend.schematic.ladder_solve._SOLVER_DETERMINISTIC_TIME", 0):
            with self.assertRaisesRegex(LadderSolveError, "deterministic limit, no placement"):
                legalise(p, symbols, nets)

    def test_a_hint_layout_is_a_hint_not_a_constraint(self):
        p, symbols, nets = self.chain()
        plain = legalise(p, symbols, nets)
        # a layout that is illegal (everything on one spot, far off) and one that is the answer: the same parts, legal
        crowded = {s.reference: Placement((500.0, 500.0), O) for s in symbols}
        for hint in (crowded, dict(plain)):
            result = legalise(p, symbols, nets, hint=hint)
            self.assertEqual(set(result), set(plain))
            self.assertEqual({r: v.position for r, v in result.items()}, {r: v.position for r, v in plain.items()})

    def test_several_seeds_solve_in_processes_and_the_cheapest_is_returned(self):
        p, symbols, nets = self.chain()
        single = legalise(p, symbols, nets)
        many = legalise(p, symbols, nets, seeds=(7, 11, 13))
        self.assertEqual(set(many), set(single))
        self.assertEqual([seed for seed, _price in many.stats["seeds"]], [7, 11, 13])
        self.assertIn(many.stats["seed"], (7, 11, 13))
        # the answer depends on the seeds listed and nothing else
        again = legalise(p, symbols, nets, seeds=(7, 11, 13))
        self.assertEqual({r: v.position for r, v in again.items()}, {r: v.position for r, v in many.items()})
        self.assertEqual(again.stats["seed"], many.stats["seed"])

    def test_every_seed_failing_raises_the_error_of_the_first(self):
        symbols = [symbol("A"), symbol("N"), symbol("B")]
        p = plan(symbols, [row("main", ["A", "N", "B"])], pins={"A": "A.2", "N": "N.1", "B": "B.1"})
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, symbols, [Connection("signal", (("A", "2"), ("B", "1")))], seeds=(7, 11))
        self.assertIn("N", caught.exception.parts)

    def test_the_wall_clock_guard_returns_the_best_clean_incumbent_marked_in_the_status(self):
        p, symbols, nets = self.chain()
        plain = legalise(p, symbols, nets)
        with patch("backend.schematic.ladder_solve._SOLVER_SECONDS", 1.0),                 patch("backend.schematic.ladder_solve.cp_model.CpSolver.WallTime", return_value=0.4):
            limited = legalise(p, symbols, nets)
        self.assertEqual(limited.status, "cp-sat (wall-clock limit)")
        self.assertEqual(set(limited), set(plain))

    def test_fractional_grid_pin_offsets_do_not_require_off_grid_anchors(self):
        symbols = [symbol("A"), symbol("B")]
        for s in symbols:
            g = s.geometry[O]
            s.geometry[O] = OrientedGeometry(
                {n: PinGeometry((p.point[0], .635), p.normal) for n, p in g.pins.items()}, g.box)
        p = plan(symbols, [row("main", ["A", "B"])], pins={"A": "A.2", "B": "B.1"})
        result = legalise(p, symbols, [Connection("signal", (("A", "2"), ("B", "1")))])
        self.assertEqual(tip(symbols, result, "A", "2")[1], .635)
        self.assertEqual(tip(symbols, result, "B", "1")[1], .635)
        for placement in result.values():
            self.assertEqual(placement.position[1], 0)

    def test_pin_facing_hang_feeds_free_trunk_by_drop(self):
        a = symbol("A", extra={"3": PinGeometry((0, 6.35), (0, 1))})
        a.geometry[O] = OrientedGeometry(a.geometry[O].pins, (-3.81, -6.35, 3.81, 6.35))
        symbols = [a, symbol("H", True)]
        p = plan(symbols, [row("main", ["A"])],
                 hangs=[Data(reference="H", wire="support", host="A.3", side="below", orientation=O, rank=0)],
                 pins={"A": "1"})
        result = legalise(p, symbols, [Connection("support", (("A", "3"), ("H", "1")))])
        host = tip(symbols, result, "A", "3")
        hung = tip(symbols, result, "H", "1")
        # The hang's tip is on a trunk the pin reaches by a straight drop: directly below it, or at the
        # pin's own height (no drop). Extent precedes the alignment preference, so the compact one wins.
        self.assertTrue(abs(host[0] - hung[0]) < 1e-6 or abs(host[1] - hung[1]) < 1e-6)
        self.assertGreaterEqual(hung[1] - host[1], -1e-6)
        self.assertGreaterEqual(abs(hung[0] - host[0]) + abs(hung[1] - host[1]), 2.54 - 1e-6)
        self.assertAlmostEqual(result["H"].position[1] - 3.81, hung[1])

    def test_rank_is_order_not_pitch_or_slot_index(self):
        p, symbols, nets = self.chain()
        result = legalise(p, symbols, nets, pitch_mm=25.4)
        first = tip(symbols, result, "H1", "1")[0]
        second = tip(symbols, result, "H2", "1")[0]
        self.assertGreater(first, second)
        self.assertLess(first - second, 25.4)

    def test_any_chain_feeder_can_supply_the_hangs_free_trunk(self):
        host = symbol("A", extra={"3": PinGeometry((-3.81, 7.62), (-1, 0))})
        host.geometry[O] = OrientedGeometry(host.geometry[O].pins, (-3.81, -1.27, 3.81, 8.89))
        symbols = [symbol("S", True), host, symbol("H", True)]
        p = plan(symbols, [row("main", ["S", "A"])],
                 hangs=[Data(reference="H", wire="support", host="A.3", side="below", orientation=O, rank=0)],
                 pins={"S": "1", "A": "1"})
        result = legalise(p, symbols, [Connection("support", (("S", "2"), ("A", "3"), ("H", "1"))),
                                           Connection("chain", (("S", "1"), ("A", "1")))])
        signal = tip(symbols, result, "H", "1")
        feeder = tip(symbols, result, "S", "2")
        self.assertEqual(signal[0], feeder[0])
        self.assertGreater(signal[1], feeder[1])
        self.assertGreater(signal[1], tip(symbols, result, "A", "3")[1])

    def test_feedback_accepts_non_grid_gap(self):
        symbols = [symbol("A"), symbol("B"), symbol("F")]
        p = plan(symbols, [row("main", ["A", "B"])],
                 spans=[Data(reference="F", from_wire="input", to_wire="output", orientation=O)],
                 pins={"A": "1", "B": "1"})
        result = legalise(p, symbols, [Connection("input", (("A", "2"), ("F", "1"))),
                                           Connection("output", (("B", "1"), ("F", "2")))], gap_mm=1.05)
        for placement in result.values():
            for coordinate in placement.position:
                self.assertAlmostEqual(coordinate / 1.27, round(coordinate / 1.27))

    def test_landed_plan_supports_bare_and_empty_row_pin_values(self):
        symbols = [symbol("A"), symbol("B")]
        p = Plan((Row("main", "chain", None, ("A", "B")),), (), (), (),
                 {"A": O, "B": O}, {"A": "2", "B": ""})
        result = legalise(p, symbols, [])
        self.assertEqual(tip(symbols, result, "A", "2")[1], result["B"].position[1])

    def test_free_margin_column_is_above_and_left_of_rows(self):
        symbols = [symbol("A"), symbol("V", True)]
        p = Plan((Row("main", "chain", None, ("A",)),), (), (),
                 (Column("", ("V",), (O,)),), {"A": O, "V": O}, {"A": "1"})
        result = legalise(p, symbols, [])
        self.assertLess(result["V"].position[0], result["A"].position[0])
        self.assertLess(tip(symbols, result, "V", "2")[1], result["A"].position[1])

    def test_sequence_keeps_order_across_row_steps(self):
        symbols = [symbol("A"), symbol("B")]
        p = Plan((Row("top", "chain", None, ("A",)), Row("lower", "chain", "top", ("B",))),
                 (), (), (), {"A": O, "B": O}, {"A": "1", "B": "1"}, (("A", "B"),))
        result = legalise(p, symbols, [])
        self.assertGreater(result["B"].position[0], result["A"].position[0])
        self.assertGreater(result["B"].position[1], result["A"].position[1])

    def test_unplanned_hang_host_is_a_named_plan_finding(self):
        symbols = [symbol("H", True), symbol("HOST")]
        p = plan(symbols[:1], hangs=[Data(reference="H", wire="n", host="HOST.2", side="below", orientation=O, rank=0)])
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, symbols, [Connection("n", (("HOST", "2"), ("H", "1")))])
        self.assertEqual(caught.exception.groups, ("hang host",))
        self.assertEqual(caught.exception.parts, ("H", "HOST"))

    def test_hang_box_reaching_past_its_signal_tip_is_still_on_its_side(self):
        # A symbol's box can reach past the tip (padding): the pin's direction, not the box, puts the part
        # on its declared side of the wire.
        p, symbols, nets = self.chain()
        for s in symbols:
            if s.reference.startswith("H"):
                g = s.geometry[O]
                s.geometry[O] = OrientedGeometry(g.pins, (-1.27, -6.35, 1.27, 6.35))
        result = legalise(p, symbols, nets)
        self.assertEqual(tip(symbols, result, "H1", "1")[1], tip(symbols, result, "A", "2")[1])
        self.assertGreater(result["H1"].position[1], result["A"].position[1])

    def test_lane_part_recorded_as_a_span_is_placed_as_the_span(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                a = symbol("A", extra={"3": PinGeometry((direction * 3.81, 7.62), (direction, 0))})
                far = "2" if direction > 0 else "1"
                near = "1" if direction > 0 else "2"
                symbols = [a, symbol("S"), symbol("T", True)]
                p = plan(symbols, [row("main", ["A"]), row("lane", ["S"], "A.3", "lane")], pins={"A": "1"},
                         spans=[Data(reference="S", from_wire="lane", to_wire="next", orientation=O),
                                Data(reference="T", from_wire="next", to_wire="floor", orientation=O)])
                nets = [Connection("lane", (("A", "3"), ("S", near))), Connection("next", (("S", far), ("T", "1"))),
                        Connection("floor", (("T", "2"),))]
                result = legalise(p, symbols, nets)
                host = tip(symbols, result, "A", "3")
                for number in ("1", "2"):
                    self.assertAlmostEqual(tip(symbols, result, "S", number)[1], host[1])
                self.assertGreaterEqual(direction * (tip(symbols, result, "S", near)[0] - host[0]), 2.54 - 1e-6)
                self.assertGreaterEqual(direction * (tip(symbols, result, "S", far)[0]
                                                     - tip(symbols, result, "S", near)[0]), 0)
                self.assertGreater(direction * (tip(symbols, result, "T", "1")[0] - tip(symbols, result, "S", near)[0]), 0)

    def test_wire_between_rows_with_facing_away_pins_gets_a_free_trunk(self):
        # Both pins point down and the first chain pin is the upper one: a trunk at its height would need the
        # lower pin to return through its own body, so the trunk is free, below both.
        a = symbol("A", extra={"3": PinGeometry((0, 3.81), (0, 1))})
        b = symbol("B", extra={"3": PinGeometry((0, 3.81), (0, 1))})
        symbols = [a, b]
        p = plan(symbols, [row("upper", ["A"]), row("lower", ["B"], "upper")], pins={"A": "1", "B": "1"})
        result = legalise(p, symbols, [Connection("n", (("A", "3"), ("B", "3")))])
        self.assertGreaterEqual(tip(symbols, result, "B", "3")[1], tip(symbols, result, "A", "3")[1])

    def test_arrival_lead_runs_past_a_rail_stub_it_would_cross(self):
        # Two sideways pins, a rail pin between them: the wire's drop cannot stand at the usual lead length.
        a = symbol("A", extra={"3": PinGeometry((3.81, 2.54), (1, 0)), "4": PinGeometry((3.81, -2.54), (1, 0))})
        b = symbol("B")
        symbols = [a, b]
        p = plan(symbols, [row("upper", ["A"]), row("lower", ["B"], "upper")], pins={"A": "1", "B": "1"})
        nets = [Connection("rail", (("A", "3"),), style="power"), Connection("n", (("A", "4"), ("B", "2")))]
        result = legalise(p, symbols, nets)
        self.assertEqual(set(result), {"A", "B"})

    def test_infeasibility_survives_copying_and_pickling(self):
        import pickle
        error = LadderInfeasible(["hang side"], ["B", "A"], "why")
        for copy in (pickle.loads(pickle.dumps(error)), __import__("copy").copy(error)):
            self.assertEqual((copy.groups, copy.parts, copy.detail), (("hang side",), ("A", "B"), "why"))
            self.assertEqual(str(copy), str(error))

    def test_result_reports_phase_times_and_infeasibility_still_names_its_group(self):
        p, symbols, nets = self.chain()
        result = legalise(p, symbols, nets)
        self.assertEqual(set(result.timing), {"build", "feasibility", "optimisation"})
        p.hangs = (Data(reference="H1", wire="signal", host="B.1", side="above", orientation=O, rank=0),
                   *p.hangs[1:])
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, symbols, nets)
        self.assertIn("hang side", caught.exception.groups)

    def rail_toy(self):
        symbols = [symbol("A", extra={"3": PinGeometry((3.81, 2.54), (1, 0))}), symbol("B")]
        p = plan(symbols, [row("main", ["A", "B"])], pins={"A": "A.2", "B": "B.1"})
        nets = [Connection("signal", (("A", "2"), ("B", "1"))), Connection("rail", (("A", "3"),), style="power")]
        return p, symbols, nets

    def test_exclusion_left_out_of_the_model_is_restored_when_a_solution_breaks_it(self):
        # With a one-grid slot the rail pin's 7.62 mm stub reservation, not the slot, keeps B away from A. Left
        # out of the first model, the exclusion is what the first solution (B as near as the slot allows) breaks.
        p, symbols, nets = self.rail_toy()
        with patch("backend.schematic.ladder_solve._close", return_value=True):
            full = legalise(p, symbols, nets, pitch_mm=2.54)
        with patch("backend.schematic.ladder_solve._close", return_value=False):
            lazy = legalise(p, symbols, nets, pitch_mm=2.54)
        self.assertGreaterEqual(lazy["B"].position[0] - 2.54, 3.81 + 7.62 + 2.54 - 1e-7)
        self.assertGreaterEqual(lazy.stats["rounds"], 1)
        self.assertEqual(full.stats["rounds"], 0)
        self.assertEqual(full, lazy)
        self.assertGreater(lazy.stats["left_out"], 0)

    def test_round_limit_writes_in_everything_left_out(self):
        p, symbols, nets = self.rail_toy()
        with patch("backend.schematic.ladder_solve._close", return_value=False), \
                patch("backend.schematic.ladder_solve._ROUND_LIMIT", 1):
            lazy = legalise(p, symbols, nets, pitch_mm=2.54)
        self.assertEqual(lazy.stats["left_out"], 0)
        self.assertGreaterEqual(lazy["B"].position[0] - 2.54, 3.81 + 7.62 + 2.54 - 1e-7)

    def test_pruning_does_not_change_the_placement(self):
        for build in (self.chain, self.rail_toy):
            p, symbols, nets = build()
            with patch("backend.schematic.ladder_solve._close", return_value=True):
                full = legalise(p, symbols, nets)
            with patch("backend.schematic.ladder_solve._close", return_value=False):
                lazy = legalise(p, symbols, nets)
            self.assertEqual(full, lazy)

    def test_infeasibility_is_diagnosed_from_what_was_restored(self):
        symbols = [symbol("A"), symbol("N"), symbol("B")]
        p = plan(symbols, [row("main", ["A", "N", "B"])], pins={"A": "A.2", "N": "N.1", "B": "B.1"})
        with patch("backend.schematic.ladder_solve._close", return_value=False):
            with self.assertRaises(LadderInfeasible) as caught:
                legalise(p, symbols, [Connection("signal", (("A", "2"), ("B", "1")))])
        self.assertTrue({"wire-body:signal", "wire-pin:signal"} & set(caught.exception.groups))
        self.assertIn("N", caught.exception.parts)

    def test_row_order_separates_parts_that_are_not_neighbours(self):
        # No pairwise exclusion is written for A and C (the chain order implies one), yet they stay apart.
        symbols = [symbol("A"), symbol("B"), symbol("C")]
        p = plan(symbols, [row("main", ["A", "B", "C"])], pins={"A": "1", "B": "1", "C": "1"})
        result = legalise(p, symbols, [])
        a, b, c = (result[r].position[0] for r in "ABC")
        self.assertGreaterEqual(b - a, 5.08 + 2.54 - 1e-7)
        self.assertGreaterEqual(c - a, 2 * (5.08 + 2.54) - 1e-7)

    def test_adjacent_row_parts_that_would_overlap_stay_apart(self):
        # Tall bodies on adjacent rows, rows only a gap apart: nothing but the pairwise exclusion separates them.
        tall = [symbol("A", True), symbol("B", True)]
        for s in tall:
            g = s.geometry[O]
            s.geometry[O] = OrientedGeometry(g.pins, (-1.27, -12.7, 1.27, 12.7))
        p = plan(tall, [row("upper", ["A"]), row("lower", ["B"], "upper")], pins={"A": "1", "B": "1"})
        result = legalise(p, tall, [])
        a, b = result["A"].position, result["B"].position
        self.assertTrue(abs(a[0] - b[0]) >= 2.54 + 2.54 - 1e-7 or abs(a[1] - b[1]) >= 25.4 + 2.54 - 1e-7)

    def test_bulk_check_agrees_with_the_exclusions_it_restates(self):
        import random
        from ortools.sat.python import cp_model
        from backend.schematic.ladder_solve import _Checker, _affine, evaluator
        rng = random.Random(3)
        total = 0
        for trial in range(30):
            model = cp_model.CpModel()
            variables = [model.NewIntVar(-20, 20, f"v{i}") for i in range(12)]
            values = [rng.randint(-8, 8) for _ in variables]
            for variable, value in zip(variables, values):
                model.Add(variable == value)
            solver = cp_model.CpSolver()
            self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)

            def coordinate():
                return rng.choice(variables) * rng.choice((1, 2, 3)) + rng.randint(-4, 4) if rng.random() < .8 else rng.randint(-4, 4)

            entries = []
            for _ in range(40):
                if rng.random() < .5:
                    box_a = tuple(coordinate() for _ in range(4))
                    box_b = tuple(coordinate() for _ in range(4))
                    entries.append((("o", len(entries)), None, lambda v, a=box_a, b=box_b: _overlap(v(a), v(b)),
                                    (box_a, box_b), ("overlap",)))
                else:
                    horizontal = rng.random() < .5
                    segment = tuple(coordinate() for _ in range(4))
                    point = (coordinate(), coordinate())
                    entries.append((("p", len(entries)), None,
                                    lambda v, s=segment, p=point, h=horizontal: _on_segment(v(p), v(s[:2]), v(s[2:]), h),
                                    (segment, (*point, *point)), ("point", horizontal)))
            checker = _Checker(entries, lambda e: _affine(model, e))
            assert not checker.slow
            value = evaluator(solver)
            expected = [entry for entry in entries if entry[2](value)]
            got = checker.broken([solver.Value(variables[i]) for i in checker.needed])
            self.assertEqual([e[0] for e in got], [e[0] for e in expected])
            total += len(expected)
        self.assertGreater(total, 10)      # the comparison is not of empty lists

    def stack_toy(self, first="J1", second="J2"):
        symbols = [symbol("U1", True), symbol("L1", True), symbol("U2", True), symbol("L2", True),
                   symbol("H"), symbol("S")]
        columns = [Data(pin=f"net:{first}", order=("U1", "L1"), orientations=(O, O)),
                   Data(pin=f"net:{second}", order=("U2", "L2"), orientations=(O, O))]
        hangs = [Data(reference="H", wire=first, host="U1.2", side="left", orientation=O, rank=0)]
        spans = [Data(reference="S", from_wire=first, to_wire=second, orientation=O)]
        nets = [Connection(first, (("U1", "2"), ("L1", "1"), ("H", "2"), ("S", "1"))),
                Connection(second, (("U2", "2"), ("L2", "1"), ("S", "2")))]
        return plan(symbols, columns=columns, hangs=hangs, spans=spans), symbols, nets

    def test_stacks_stand_left_to_right_in_the_plans_order_whatever_their_names_sort_to(self):
        p, symbols, nets = self.stack_toy("Z", "A")          # "Z" first: sorted by name it would come second
        result = legalise(p, symbols, nets)
        self.assertLess(result["U1"].position[0], result["U2"].position[0])
        self.assertLess(result["L1"].position[0], result["L2"].position[0])
        # the plan lists the stacks the other way round from the span: the span's from_wire stack is the left one
        p, symbols, nets = self.stack_toy()
        p.columns = tuple(reversed(p.columns))
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, symbols, nets)
        self.assertTrue({"stack column order", "stack span order"} & set(caught.exception.groups), caught.exception.groups)

    def test_stacked_pairs_share_a_junction_wire_with_a_hang_and_a_span_landing_on_it(self):
        p, symbols, nets = self.stack_toy()
        result = legalise(p, symbols, nets)
        tips = {(r, n): tip(symbols, result, r, n) for r, n in (("U1", "2"), ("L1", "1"), ("U2", "2"), ("L2", "1"),
                                                                ("H", "2"), ("S", "1"), ("S", "2"))}
        self.assertEqual(tips["U1", "2"][0], tips["L1", "1"][0])
        self.assertEqual(tips["U2", "2"][0], tips["L2", "1"][0])
        self.assertLess(tips["U1", "2"][1], tips["L1", "1"][1])
        for ref, number in (("H", "2"), ("S", "1")):                       # on the first junction, within its span
            self.assertEqual(tips[ref, number][0], tips["U1", "2"][0])
            self.assertTrue(tips["U1", "2"][1] <= tips[ref, number][1] <= tips["L1", "1"][1])
        self.assertEqual(tips["S", "2"][0], tips["U2", "2"][0])
        self.assertEqual(tips["S", "1"][1], tips["S", "2"][1])
        self.assertLess(result["H"].position[0], tips["U1", "2"][0])        # the hang's body is on its left

    def multi_pin_stack(self, extra):
        """A stacked pair whose upper part has more pins on the junction (a power FET's source pins), all facing down."""
        symbols = [symbol("U", True, extra), symbol("L", True)]
        columns = [Data(pin="net:J", order=("U", "L"), orientations=(O, O))]
        nets = [Connection("J", (("U", "2"), *(("U", n) for n in sorted(extra)), ("L", "1")))]
        return plan(symbols, columns=columns), symbols, nets

    def test_a_stacked_part_with_several_pins_at_one_tip_is_one_wire_end(self):
        p, symbols, nets = self.multi_pin_stack({"3": PinGeometry((0, 3.81), (0, 1)), "4": PinGeometry((0, 3.81), (0, 1))})
        result = legalise(p, symbols, nets)
        routes = result.wires["J"]
        self.assertEqual(len(routes), 1)                            # no run: the tips coincide
        self.assertEqual(set(routes[0].pins), {0, 1, 2, 3})          # the one vertical wire reaches all four pins
        self.assertEqual(tip(symbols, result, "U", "3"), tip(symbols, result, "L", "1")[:1] + tip(symbols, result, "U", "3")[1:])

    def test_a_stacked_part_with_pins_side_by_side_on_the_junction_has_a_run_joining_them(self):
        p, symbols, nets = self.multi_pin_stack({"3": PinGeometry((2.54, 3.81), (0, 1)), "4": PinGeometry((-2.54, 3.81), (0, 1))})
        result = legalise(p, symbols, nets)
        vertical, run = result.wires["J"]
        left, right = tip(symbols, result, "U", "4"), tip(symbols, result, "U", "3")
        self.assertEqual(left[1], right[1])
        self.assertEqual(run.points, ((left[0], left[1]), (right[0], right[1])))        # a run at the tips' height
        self.assertEqual(vertical.points[0][0], tip(symbols, result, "L", "1")[0])       # the vertical wire meets the run
        self.assertTrue(left[0] <= vertical.points[0][0] <= right[0])
        self.assertEqual(set(run.pins), {0, 1, 2})        # the upper part's three pins; the lower part's is on the vertical wire

    def test_a_stack_whose_pins_do_not_face_is_named(self):
        p, symbols, nets = self.stack_toy()
        p.columns = (Data(pin="net:J1", order=("L1", "U1"), orientations=(O, O)),) + p.columns[1:]
        with self.assertRaises(LadderInfeasible) as caught:
            legalise(p, symbols, nets)
        self.assertIn("stack pin", caught.exception.groups)

    def test_the_result_carries_the_wires_it_drew_and_they_survive_a_placement_file(self):
        from backend.schematic import ladder_fixture
        p, symbols, nets = self.chain()
        result = legalise(p, symbols, nets)
        trunk = next(r for r in result.wires["signal"] if not r.pins)
        ax, ay = tip(symbols, result, "A", "2")
        bx, by = tip(symbols, result, "B", "1")
        self.assertEqual(trunk.points, ((ax, ay), (bx, by)))                 # along the row, tip to tip
        drops = [r for r in result.wires["signal"] if r.pins]
        self.assertEqual(sum(r.length for r in result.wires["signal"]) - trunk.length, sum(r.length for r in drops))
        again = ladder_fixture.wires_from_json({"wires": ladder_fixture.wires_to_json(result.wires)})
        self.assertEqual({k: [(r.points, r.bends, r.pins) for r in v] for k, v in again.items()},
                         {k: [(r.points, r.bends, r.pins) for r in v] for k, v in result.wires.items()})

    def through(self, result, ref, box):
        """How many of the drawn segments pass through the interior of ``ref``'s body (``box`` about its anchor)."""
        x0, y0 = result[ref].position
        left, top, right, bottom = x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]
        found = 0
        for routes in result.wires.values():
            for route in routes:
                for (ax, ay), (bx, by) in zip(route.points, route.points[1:]):
                    if min(ax, bx) < right - 1e-6 and max(ax, bx) > left + 1e-6 and \
                            min(ay, by) < bottom - 1e-6 and max(ay, by) > top + 1e-6:
                        found += 1
        return found

    def test_a_wire_through_the_body_of_a_part_it_joins_is_priced_not_forbidden(self):
        # N joins A.2, B.2 and C.1 with B between A and C on the row: the trunk would run through B's body to reach B.2.
        # No drawing of the plan avoids it, so it is drawn, and counted
        box = (-3.81, -1.27, 3.81, 1.27)
        lone = PlacedSymbol("B", (100, 100), O, {O: OrientedGeometry({"2": PinGeometry((3.81, 0), (1, 0))}, box)})
        symbols = [symbol("A"), lone, symbol("C")]
        nets = [Connection("N", (("A", "2"), ("B", "2"), ("C", "1")))]
        p = plan(symbols, [row("main", ["A", "B", "C"])], pins={"A": "A.2", "B": "B.2", "C": "C.1"})
        result = legalise(p, symbols, nets)
        self.assertGreaterEqual(self.through(result, "B", box), 1)
        self.assertEqual(result.joined, "priced")       # excluded first, priced once the exclusion alone is infeasible

    def test_a_wire_reaches_a_joined_part_by_the_exit_of_its_pin_without_going_through_it(self):
        box = (-3.81, -1.27, 3.81, 1.27)
        facing = PlacedSymbol("B", (100, 100), O, {O: OrientedGeometry({"1": PinGeometry((-3.81, 0), (-1, 0))}, box)})
        symbols = [symbol("A"), facing, symbol("C")]
        result = legalise(plan(symbols, [row("main", ["A", "B", "C"])], pins={"A": "A.2", "B": "B.1", "C": "C.1"}),
                          symbols, [Connection("N", (("A", "2"), ("B", "1")))])
        self.assertEqual(set(result), {"A", "B", "C"})
        self.assertEqual(self.through(result, "B", box), 0)
        self.assertEqual(result.joined, "excluded")

    def margin_case(self, same_net, offset=10):
        """A drop that passes 1.27 mm from a hung part's body, on the part's net or on another net."""
        symbols = [symbol("A"), symbol("Y"), symbol("H", True), symbol("V", True)]
        hang = Data(reference="H", wire="N", host="A.2", side="below", orientation=O, rank=0)
        p = plan(symbols, [row("main", ["A", "Y"])], hangs=[hang], pins={"A": "A.2", "Y": "Y.1"})
        if same_net:
            nets = [Connection("N", (("A", "2"), ("H", "1"), ("V", "1")))]
        else:
            nets = [Connection("N", (("A", "2"), ("H", "1"))), Connection("M", (("V", "1"), ("Y", "1")))]
        grid = 1.27
        # A on its row; H hangs from the row at x = 8 grids; V stands 19 grids below, its drop at x = 10 grids, 1.27 mm
        # off H's body (a body edge is 1 grid from its anchor), far enough down not to touch H's body or tips
        anchors = {"A": (0, 0), "Y": (47, 0), "H": (8, 3), "V": (offset, 19)}
        from backend.schematic.ladder_solve import _solve
        return _solve(p, symbols, nets, 2.54, 7.62, True, anchors=anchors)

    def test_a_drop_may_pass_closer_than_the_gap_to_a_body_of_its_own_net_but_not_to_another_nets(self):
        with self.assertRaisesRegex(LadderSolveError, "diagnosis OPTIMAL"):       # feasible: nothing rejects the anchors
            self.margin_case(True)
        with self.assertRaises(LadderInfeasible) as caught:
            self.margin_case(False)
        self.assertTrue(any(group.startswith("wire-body") for group in caught.exception.groups), caught.exception.groups)
        # it still keeps off the body: the checker's 0.5 mm, so a drop on the body's very edge is refused
        with self.assertRaises(LadderInfeasible) as touching:
            self.margin_case(True, offset=9)
        self.assertTrue(any(group.startswith("wire-body") for group in touching.exception.groups), touching.exception.groups)

    def test_checks_that_restate_the_exclusions(self):
        self.assertTrue(_overlap((0, 0, 10, 10), (5, 5, 15, 15)))
        self.assertFalse(_overlap((0, 0, 10, 10), (10, 0, 20, 10)))        # touching is allowed
        self.assertFalse(_overlap((0, 0, 10, 0), (5, -5, 6, 5)))           # a rectangle with no area is no conductor
        self.assertTrue(_on_segment((5, 0), (0, 0), (10, 0), True))
        self.assertTrue(_on_segment((10, 0), (0, 0), (10, 0), True))       # the end counts: a short
        self.assertFalse(_on_segment((11, 0), (0, 0), (10, 0), True))
        self.assertFalse(_on_segment((5, 1), (0, 0), (10, 0), True))
        self.assertTrue(_on_segment((0, 5), (0, 0), (0, 10), False))
        value = lambda e: e
        self.assertTrue(_close(value, (0, 0, 1000, 1000), (10000, 0, 11000, 1000)))
        self.assertFalse(_close(value, (0, 0, 1000, 1000), (50000, 0, 51000, 1000)))


if __name__ == "__main__":
    unittest.main()
