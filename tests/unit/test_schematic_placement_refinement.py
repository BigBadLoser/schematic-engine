"""Where the symbols no relation places go and which way they face, from the pins they connect to."""
from __future__ import annotations

import unittest
from types import SimpleNamespace as Data

from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import (
    SIDEWAYS_BENDS, Connection, OrientedGeometry, PinGeometry, PlacedSymbol, Route, Weights, _Model, align_groups,
    count_crossings, count_jogs, estimate_net, estimate_route, refine_placement, rotate_geometry, route_charge, seed_placement,
)

UP, DOWN, LEFT, RIGHT = (0.0, -1.0), (0.0, 1.0), (-1.0, 0.0), (1.0, 0.0)
FLAT = Orientation(0)


class RouteEstimateTests(unittest.TestCase):
    """The route the router will draw: leaving each pin along its direction, as few bends as the geometry allows."""

    def test_pins_that_face_each_other_on_one_line_are_a_straight_wire(self):
        route = estimate_route((0.0, 0.0), RIGHT, (20.32, 0.0), LEFT)
        self.assertEqual((route.bends, round(route.length, 2)), (0, 20.32))

    def test_the_same_pins_a_row_apart_need_two_bends(self):
        route = estimate_route((0.0, 0.0), RIGHT, (20.32, 5.08), LEFT)
        self.assertEqual(route.bends, 2)
        self.assertAlmostEqual(route.length, 25.4)

    def test_perpendicular_pins_that_meet_are_an_L(self):
        # a pin pointing right and one pointing down, the second ahead of the first and above where it points to
        route = estimate_route((0.0, 0.0), RIGHT, (15.24, 10.16), UP)
        self.assertEqual(route.bends, 1)
        self.assertAlmostEqual(route.length, 25.4)

    def test_perpendicular_pins_that_do_not_meet_pay_for_going_round(self):
        # the second pin points away: the wire has to leave it, turn and come back
        route = estimate_route((0.0, 0.0), RIGHT, (15.24, 10.16), DOWN)
        self.assertGreaterEqual(route.bends, 3)

    def test_pins_that_point_the_same_way_make_a_U(self):
        route = estimate_route((0.0, 0.0), RIGHT, (0.0, 12.7), RIGHT)
        self.assertEqual(route.bends, 2)
        self.assertGreater(route.length, 12.7)

    def test_pins_that_face_away_from_each_other_go_all_the_way_round(self):
        away = estimate_route((0.0, 0.0), LEFT, (20.32, 0.0), RIGHT)
        toward = estimate_route((0.0, 0.0), RIGHT, (20.32, 0.0), LEFT)
        self.assertGreaterEqual(away.bends, 4)
        self.assertGreater(away.length, toward.length)

    def test_the_route_is_an_orthogonal_polyline_from_pin_to_pin(self):
        for np_, nq, q in ((RIGHT, LEFT, (20.32, 5.08)), (RIGHT, UP, (15.24, 10.16)), (RIGHT, RIGHT, (0.0, 12.7)),
                           (LEFT, RIGHT, (20.32, 0.0)), (RIGHT, DOWN, (15.24, 10.16))):
            route = estimate_route((0.0, 0.0), np_, q, nq)
            self.assertEqual(route.points[0], (0.0, 0.0))
            self.assertEqual(route.points[-1], q)
            for (ax, ay), (bx, by) in zip(route.points, route.points[1:]):
                self.assertTrue(abs(ax - bx) < 1e-9 or abs(ay - by) < 1e-9, route.points)


class SidewaysAndNetTests(unittest.TestCase):
    """What the router was seen to draw (`auto-v22`): a wire may meet a pin tip from the side, and a net of three pins is
    one run with a T, not three pin-to-pin wires."""

    def test_a_wire_may_leave_a_pin_sideways_along_the_row_the_tip_is_on(self):
        # the BOOT0 resistor: its pin points up, the chip's pin points left, and both tips are on one row
        route = estimate_route((0.0, 0.0), UP, (11.43, 0.0), LEFT)
        self.assertEqual(route.bends, SIDEWAYS_BENDS)   # a straight wire, but not for free: a person meets a pin along its axis
        self.assertAlmostEqual(route.length, 11.43)

    def test_a_pin_above_a_straight_run_joins_it_with_a_T(self):
        # SDA: the chip's pin and the connector's pin face each other on one row, a pull-up stands above the wire
        routes = estimate_net([
            ((0.0, 0.0), RIGHT), ((30.48, 0.0), LEFT), ((15.24, -10.16), DOWN),
        ])
        self.assertEqual(sum(route.bends for route in routes), 0)
        self.assertEqual(len(routes), 2)

    def test_the_run_is_found_whichever_pin_is_listed_first(self):
        pins = [((0.0, 0.0), RIGHT), ((30.48, 0.0), LEFT), ((15.24, -10.16), DOWN)]
        for order in ((0, 1, 2), (2, 0, 1), (1, 2, 0)):
            routes = estimate_net([pins[i] for i in order])
            self.assertEqual(sum(route.bends for route in routes), 0, order)

    def test_a_pin_pointing_away_from_the_run_pays_for_going_round(self):
        routes = estimate_net([((0.0, 0.0), RIGHT), ((30.48, 0.0), LEFT), ((15.24, -10.16), UP)])
        self.assertGreaterEqual(sum(route.bends for route in routes), 2)

    def test_meeting_a_pin_from_the_side_costs_more_than_a_bend(self):
        side = estimate_route((0.0, 0.0), UP, (11.43, 0.0), LEFT)
        self.assertGreater(side.bends, 1)               # more than a bend, and less than the Z of a route that goes round
        self.assertLess(side.bends, 2)

    def test_two_pins_are_one_route_as_before(self):
        (route,) = estimate_net([((0.0, 0.0), RIGHT), ((20.32, 5.08), LEFT)])
        self.assertEqual(route.bends, 2)

    def test_one_pin_has_nothing_to_join(self):
        self.assertEqual(estimate_net([((0.0, 0.0), RIGHT)]), [])


class TrunkTests(unittest.TestCase):
    """A net of three or more pins may be one trunk with a straight drop from each pin, as a rail is drawn."""

    RAIL = [((0.0, 0.0), RIGHT), ((10.0, 5.08), UP), ((20.0, 5.08), UP), ((30.0, 5.08), UP)]

    @staticmethod
    def segments(routes):
        return {frozenset(pair) for r in routes for pair in zip(r.points, r.points[1:])}

    @staticmethod
    def totals(routes):
        return sum(r.bends for r in routes), sum(r.length for r in routes)

    def test_pins_that_hang_from_a_row_are_one_trunk_with_drops(self):
        routes = estimate_net(self.RAIL)
        bends, length = self.totals(routes)
        self.assertEqual(bends, 1)
        self.assertAlmostEqual(length, 30 + 3 * 5.08)

    def test_a_pin_pointing_away_costs_more(self):
        away = [self.RAIL[0], self.RAIL[1], ((20.0, 5.08), DOWN), self.RAIL[3]]
        cost = lambda routes: sum(r.length + 5.0 * r.bends for r in routes)
        self.assertGreater(cost(estimate_net(away)), cost(estimate_net(self.RAIL)))

    def test_a_two_pin_net_is_the_same_route(self):
        (route,) = estimate_net([((0.0, 0.0), RIGHT), ((20.32, 5.08), LEFT)])
        self.assertEqual((route.bends, round(route.length, 2)), (2, 25.4))

    def test_the_pin_order_does_not_change_the_result(self):
        reference = self.segments(estimate_net(self.RAIL))
        for order in ((3, 1, 0, 2), (2, 3, 1, 0), (1, 0, 3, 2)):
            routes = estimate_net([self.RAIL[i] for i in order])
            self.assertEqual(self.segments(routes), reference)


class TipsOnTheTrunkTests(unittest.TestCase):
    """A rail drawn along the tips of the parts hung from it: pins whose tips are on the trunk, normals across it."""

    HUNG = [((0.0, 0.0), RIGHT), ((8.0, 0.0), UP), ((16.0, 0.0), UP), ((24.0, 0.0), UP), ((32.0, 0.0), LEFT)]

    def test_tips_on_the_trunk_are_a_trunk_with_drops_of_no_length(self):
        routes = estimate_net(self.HUNG)
        self.assertEqual(len(routes), 1)
        (trunk,) = routes
        self.assertEqual(trunk.points, ((0.0, 0.0), (32.0, 0.0)))
        self.assertEqual((trunk.bends, trunk.length), (0, 32.0))

    def test_no_short_and_no_through_when_it_is_laid_out(self):
        def part(ref, point, normal):
            # The tip is the anchor; the body lies behind it, away from the way the pin points.
            box = tuple(sorted((0.0, -5.0 * normal[0])) if axis == 0 else (-1.0, 1.0) for axis in (0, 1)) if normal[0] else (
                (-1.0, 1.0), tuple(sorted((0.0, -5.0 * normal[1]))))
            geometry = OrientedGeometry({"1": PinGeometry((0.0, 0.0), normal)}, (box[0][0], box[1][0], box[0][1], box[1][1]))
            return PlacedSymbol(ref, point, FLAT, {FLAT: geometry})
        symbols = [part(f"U{i}", p, n) for i, (p, n) in enumerate(self.HUNG)]
        net = Connection("rail", tuple((s.reference, "1") for s in symbols))
        model = _Model(symbols, [net], {}, Weights(), None)
        found = {}
        model.cost({s.reference: (s.position, s.orientation) for s in symbols}, found=found)
        self.assertFalse(found.get("short"))
        self.assertFalse(found.get("through"))

    def test_a_pin_along_the_trunk_in_the_middle_is_still_refused(self):
        middle = [self.HUNG[0], ((16.0, 0.0), RIGHT), self.HUNG[4]]
        self.assertGreater(len(estimate_net(middle)), 1)     # no trunk: a tree of routes
        (first, *_) = estimate_net(self.HUNG[:1] + [((16.0, 0.0), RIGHT), ((32.0, 0.0), LEFT)])
        self.assertIsNotNone(first)


class ChargedChoiceTests(unittest.TestCase):
    """The estimate prices what a route runs over (a pin tip that is not the net's, a body) before it chooses a tree or a
    trunk, as the cost does afterwards, so it does not prefer a route the cost then punishes."""

    @staticmethod
    def scene():
        # A.1 and B.1 face each other on one row; a part F of another net hangs from the row between them, its tip on it;
        # C.1 joins from below. The straight run between A and B is the cheap tree, over F's tip.
        def part(ref, point, normal, box):
            return PlacedSymbol(ref, point, FLAT, {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), normal)}, box)})
        symbols = [
            part("A", (0.0, 0.0), RIGHT, (-5.0, -1.0, 0.0, 1.0)), part("B", (30.0, 0.0), LEFT, (0.0, -1.0, 5.0, 1.0)),
            part("C", (22.0, 6.0), UP, (-1.0, 0.0, 1.0, 5.0)), part("F", (10.0, 0.0), UP, (-2.0, 0.0, 2.0, 8.0)),
        ]
        nets = [Connection("N", (("A", "1"), ("B", "1"), ("C", "1")), "wire"), Connection("M", (("F", "1"),), "wire")]
        return symbols, nets

    def test_the_trunk_is_chosen_over_a_tree_that_runs_over_a_foreign_tip(self):
        symbols, nets = self.scene()
        pose = {s.reference: (s.position, s.orientation) for s in symbols}
        found: dict = {}
        _Model(symbols, nets, {}, Weights(), None).cost(pose, found=found)
        self.assertFalse(found.get("short"))
        self.assertFalse(found.get("through"))
        # without the charge the same net is the straight tree, and the cost then counts the short
        plain = [((0.0, 0.0), RIGHT), ((30.0, 0.0), LEFT), ((22.0, 6.0), UP)]
        self.assertTrue(all(route.pins for route in estimate_net(plain)))

    def test_a_trunk_along_the_tips_wins_only_when_it_avoids_a_charge(self):
        pins = [((0.0, 0.0), RIGHT), ((10.0, 5.0), UP), ((20.0, 5.0), UP), ((30.0, 5.0), UP)]
        trunk_rows = lambda routes: {round(a[1], 2) for r in routes for a, b in zip(r.points, r.points[1:]) if a[1] == b[1] and abs(a[0] - b[0]) >= 25}
        self.assertEqual(trunk_rows(estimate_net(pins)), {0.0})            # as before, with nothing in the way
        free = estimate_net(pins, (), 60.0, 30.0)
        self.assertEqual(trunk_rows(free), {0.0})
        # a body across the rows in front of the tips (and the row of the chain pins) but not the row the tips are on
        blocker = (((), (4.0, -3.0, 6.0, 4.0), (), 0.5),)
        around = estimate_net(pins, blocker, 60.0, 30.0)
        self.assertEqual(trunk_rows(around), {5.0})
        self.assertFalse(route_charge(around, blocker, 60.0, 30.0))


class PlanRouteTests(unittest.TestCase):
    """A plan's own wires are scored, not a re-estimate of them (``ladder_plan.plan_routes`` and ``cost(routes=...)``)."""

    @staticmethod
    def part(ref, point, normal, box):
        return PlacedSymbol(ref, point, FLAT, {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), normal)}, box)})

    def scene(self):
        # A.1 and B.1 face each other on one row with a foreign tip F.1 on it between them; C hangs below a trunk that the
        # plan puts at the height of C's tip, under that row
        symbols = [
            self.part("A", (0.0, 0.0), RIGHT, (-5.0, -1.0, 0.0, 1.0)), self.part("B", (30.0, 0.0), LEFT, (0.0, -1.0, 5.0, 1.0)),
            self.part("C", (22.0, 6.0), UP, (-1.0, 0.0, 1.0, 5.0)), self.part("F", (10.0, 0.0), UP, (-2.0, 0.0, 2.0, 3.0)),
        ]
        nets = [Connection("N", (("A", "1"), ("B", "1"), ("C", "1")), "wire"), Connection("M", (("F", "1"),), "wire")]
        plan = Data(orientations={s.reference: FLAT for s in symbols}, columns=(), rows=(),
                    hangs=(Data(reference="C", wire="N", host="A.1", side="below"),))
        return symbols, nets, plan

    def test_the_plans_trunk_scores_no_short_where_the_estimate_runs_over_the_tip(self):
        from backend.schematic.ladder_plan import plan_routes
        symbols, nets, plan = self.scene()
        pose = {s.reference: (s.position, s.orientation) for s in symbols}
        blind = Weights(short=0.0, through=0.0)               # an estimate that does not price what it runs over
        estimated: dict = {}
        _Model(symbols, nets, {}, blind, None).cost(pose, found=estimated)
        self.assertEqual(len(estimated.get("short", [])), 1)
        routes = plan_routes(plan, symbols, nets)
        self.assertEqual({r.points[0][1] for r in routes["N"] if not r.pins}, {6.0})
        planned: dict = {}
        _Model(symbols, nets, {}, blind, None).cost(pose, found=planned, routes=routes)
        self.assertFalse(planned.get("short"))
        self.assertFalse(planned.get("through"))

    def test_both_scorings_agree_where_the_estimate_already_draws_the_plans_trunk(self):
        from backend.schematic.ladder_plan import plan_routes
        pins = [((0.0, 0.0), RIGHT), ((8.0, 0.0), UP), ((16.0, 0.0), UP), ((24.0, 0.0), UP), ((32.0, 0.0), LEFT)]
        symbols = [self.part(f"U{i}", p, n, (-1.0, 0.0, 1.0, 5.0) if n == UP else (-5.0, -1.0, 0.0, 1.0) if n == RIGHT
                             else (0.0, -1.0, 5.0, 1.0)) for i, (p, n) in enumerate(pins)]
        nets = [Connection("RAIL", tuple((s.reference, "1") for s in symbols), "wire")]
        plan = Data(orientations={s.reference: FLAT for s in symbols}, columns=(), rows=(),
                    hangs=tuple(Data(reference=f"U{i}", wire="RAIL", host="U0.1", side="below") for i in (1, 2, 3)))
        pose = {s.reference: (s.position, s.orientation) for s in symbols}
        routes = plan_routes(plan, symbols, nets)
        model = _Model(symbols, nets, {}, Weights(), None)
        cost = lambda **kw: model.cost(pose, **kw)
        self.assertEqual(cost(routes=routes), cost())

    def test_a_lead_is_as_long_as_it_takes_to_clear_a_foreign_tip_in_the_way_of_its_drop(self):
        from backend.schematic.ladder_plan import plan_routes
        symbols = [
            self.part("A", (0.0, 0.0), RIGHT, (-5.0, -1.0, 0.0, 1.0)), self.part("C", (20.0, 5.0), UP, (-1.0, 0.0, 1.0, 5.0)),
            self.part("D", (30.0, 5.0), UP, (-1.0, 0.0, 1.0, 5.0)), self.part("F", (2.54, 3.0), LEFT, (0.0, 2.5, 1.0, 3.5)),
        ]
        nets = [Connection("N", (("A", "1"), ("C", "1"), ("D", "1")), "wire"), Connection("M", (("F", "1"),), "wire")]
        plan = Data(orientations={s.reference: FLAT for s in symbols}, columns=(), rows=(), spans=(),
                    hangs=tuple(Data(reference=r, wire="N", host="A.1", side="below") for r in "CD"))
        routes = plan_routes(plan, symbols, nets)
        drop = next(r for r in routes["N"] if r.pins == (0,))
        self.assertGreater(drop.points[1][0], 2.54 + 1e-6)               # the lead goes past F's tip before it turns
        pose = {s.reference: (s.position, s.orientation) for s in symbols}
        found: dict = {}
        _Model(symbols, nets, {}, Weights(), None).cost(pose, found=found, routes=routes)
        self.assertFalse(found.get("short"))

    def test_a_pin_on_a_part_the_plan_leaves_out_is_joined_to_the_wire_the_plan_draws(self):
        from backend.schematic.ladder_plan import attach_unplanned, plan_routes
        symbols = [
            self.part("A", (0.0, 0.0), RIGHT, (-5.0, -1.0, 0.0, 1.0)), self.part("C", (20.0, 5.0), UP, (-1.0, 0.0, 1.0, 5.0)),
            self.part("D", (30.0, 5.0), UP, (-1.0, 0.0, 1.0, 5.0)), self.part("Z", (25.0, 30.0), UP, (-1.0, 0.0, 1.0, 4.0)),
        ]
        nets = [Connection("N", (("A", "1"), ("C", "1"), ("D", "1"), ("Z", "1")), "wire")]
        plan = Data(orientations={r: FLAT for r in "ACD"}, columns=(), rows=(), spans=(),     # Z is not in the plan
                    hangs=tuple(Data(reference=r, wire="N", host="A.1", side="below") for r in "CD"))
        found = plan_routes(plan, symbols, nets)
        self.assertEqual(sorted(i for r in found["N"] for i in r.pins), [0])               # A's lead; the hung tips are on the trunk
        joined = attach_unplanned(plan, symbols, nets, found)
        extra = [r for r in joined["N"] if r.pins == (3,)]
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0].points[0], (25.0, 30.0))                                  # from the unplanned pin's tip
        self.assertEqual(extra[0].points[-1][1], 5.0)                                       # up to the trunk, a T

    def test_nets_the_plan_does_not_wire_keep_the_estimate(self):
        from backend.schematic.ladder_plan import plan_routes
        symbols, nets, plan = self.scene()
        self.assertNotIn("M", plan_routes(plan, symbols, nets))
        plan.orientations.pop("C")
        self.assertEqual(plan_routes(plan, symbols, nets), {})


class CrossingTests(unittest.TestCase):
    def test_wires_that_cross_in_the_interior_of_both_are_counted(self):
        self.assertEqual(count_crossings([("a", [(0.0, 5.0), (10.0, 5.0)]), ("b", [(5.0, 0.0), (5.0, 10.0)])]), 1)

    def test_wires_of_one_net_do_not_cross_themselves(self):
        self.assertEqual(count_crossings([("a", [(0.0, 5.0), (10.0, 5.0)]), ("a", [(5.0, 0.0), (5.0, 10.0)])]), 0)

    def test_a_wire_that_only_touches_the_end_of_another_is_not_a_crossing(self):
        self.assertEqual(count_crossings([("a", [(0.0, 5.0), (10.0, 5.0)]), ("b", [(10.0, 0.0), (10.0, 10.0)])]), 0)


def chip() -> dict[Orientation, OrientedGeometry]:
    """A chip with SDA above SCL on its right edge, GND at the bottom."""
    return {FLAT: OrientedGeometry(
        pins={
            "1": PinGeometry((10.16, -2.54), RIGHT), "2": PinGeometry((10.16, 0.0), RIGHT),
            "4": PinGeometry((0.0, 7.62), DOWN),
        },
        box=(-10.16, -7.62, 10.16, 7.62),
    )}


def connector(pin_count: int = 4) -> dict[Orientation, OrientedGeometry]:
    """A header whose pins are a column on its left, pointing left, pin 1 on top: the library's drawing, in all twelve
    orientations."""
    upright = {str(n): (-5.08, 2.54 * (n - 1) - 2.54) for n in range(1, pin_count + 1)}
    box = (-5.08, -3.81, 1.27, 2.54 * (pin_count - 1))
    candidates: dict[Orientation, OrientedGeometry] = {}
    for rotation in (0, 90, 180, 270):
        for mirror in ("", "x", "y"):
            candidates[Orientation(rotation, mirror)] = rotate_geometry(
                OrientedGeometry({k: PinGeometry(v, LEFT) for k, v in upright.items()}, box), rotation, mirror,
            )
    return candidates


def symbol(reference, geometry, position, *, orientation=FLAT, movable=False, ordered=False) -> PlacedSymbol:
    return PlacedSymbol(reference, position, orientation, geometry, movable=movable, ordered=ordered)


class RotateGeometryTests(unittest.TestCase):
    """The same turns and mirrors KiCad draws: turn counter-clockwise on the page, then mirror about the anchor."""

    def test_a_quarter_turn_takes_a_pin_on_the_left_to_the_bottom(self):
        turned = rotate_geometry(OrientedGeometry({"1": PinGeometry((-5.08, 0.0), LEFT)}, (-5.08, -1.0, 1.0, 1.0)), 90, "")
        pin = turned.pins["1"]
        self.assertEqual((round(pin.point[0], 2), round(pin.point[1], 2)), (0.0, 5.08))
        self.assertEqual(pin.normal, DOWN)

    def test_a_mirror_about_y_swaps_left_and_right(self):
        flipped = rotate_geometry(OrientedGeometry({"1": PinGeometry((-5.08, 2.54), LEFT)}, (-5.08, -1.0, 1.0, 3.0)), 0, "y")
        self.assertEqual(flipped.pins["1"].point, (5.08, 2.54))
        self.assertEqual(flipped.pins["1"].normal, RIGHT)
        self.assertEqual(flipped.box, (-1.0, -1.0, 5.08, 3.0))


class BusConnectorTests(unittest.TestCase):
    """`i2c_sensor`: the connector sat below the chip with its pins up, which put the two bus wires in the opposite order
    to the chip's pins, so they crossed."""

    NETS = [
        Connection("SDA", (("J1", "3"), ("U1", "1")), "wire"),
        Connection("SCL", (("J1", "4"), ("U1", "2")), "wire"),
    ]

    def run_refinement(self, start, orientation):
        symbols = [
            symbol("U1", chip(), (50.8, 50.8)),
            symbol("J1", connector(), start, orientation=orientation, movable=True, ordered=True),
        ]
        return refine_placement(symbols, self.NETS)

    def test_the_connector_ends_up_where_its_wires_do_not_cross(self):
        # below and to the right, pins up and in order: the layout the engine drew
        start = (76.2, 83.82)
        result = self.run_refinement(start, Orientation(90, "x"))
        after = result.placements["J1"]
        self.assertEqual(result.after.crossings, 0)
        self.assertLess(result.after.total, result.before.total)
        self.assertEqual(result.before.crossings, 1)
        # the pins face the chip's pins, in order: they point left, from the right of the chip
        geometry = connector()[after.orientation]
        self.assertEqual(geometry.pins["3"].normal, LEFT)
        self.assertGreater(after.position[0], 50.8 + 10.16)

    def test_its_bus_wires_are_straight_where_the_pins_can_line_up(self):
        result = self.run_refinement((76.2, 83.82), Orientation(90, "x"))
        self.assertEqual(result.after.bends, 0)

    def test_a_connector_whose_wires_already_run_clear_stays_where_it_is(self):
        placed = refine_placement(
            [symbol("U1", chip(), (50.8, 50.8)), symbol("J1", connector(), (76.2, 50.8), orientation=Orientation(0), movable=True,
                                                         ordered=True)],
            self.NETS,
        )
        self.assertEqual(placed.after.crossings, 0)
        self.assertLessEqual(placed.after.total, placed.before.total + 1e-6)


def resistor() -> dict[Orientation, OrientedGeometry]:
    upright = OrientedGeometry({"1": PinGeometry((0.0, -3.81), UP), "2": PinGeometry((0.0, 3.81), DOWN)}, (-1.02, -3.81, 1.02, 3.81))
    return {Orientation(r): rotate_geometry(upright, r, "") for r in (0, 90, 180, 270)}


def transistor_base() -> dict[Orientation, OrientedGeometry]:
    """A part with one pin pointing left, at its middle: the base of a transistor."""
    return {FLAT: OrientedGeometry({"2": PinGeometry((-5.08, 0.0), LEFT)}, (-5.08, -5.08, 4.09, 5.08))}


class SeriesPartTests(unittest.TestCase):
    """`led_switch`: the base resistor between the connector and the transistor was left off the line between them, so
    both wires jogged."""

    def test_a_series_resistor_lies_along_the_line_between_the_pins_it_joins(self):
        symbols = [
            symbol("J1", connector(3), (20.32, 40.64), orientation=Orientation(180, "x")),
            symbol("Q1", transistor_base(), (80.01, 50.8)),
            symbol("R1", resistor(), (45.72, 66.04), movable=True),
        ]
        nets = [
            Connection("CTRL", (("J1", "3"), ("R1", "1")), "wire"),
            Connection("BASE", (("R1", "2"), ("Q1", "2")), "wire"),
        ]
        result = refine_placement(symbols, nets)
        self.assertLess(result.after.total, result.before.total)
        self.assertLessEqual(result.after.bends, result.before.bends)
        placed = result.placements["R1"]
        self.assertIn(placed.orientation.rotation, (90, 270))     # lying along the row, not standing across it


class ClearanceTests(unittest.TestCase):
    """`mcu`: no exit for a label from the connector's middle pin because the chip stood too close in front of it."""

    def test_a_symbol_is_kept_out_of_the_room_a_pin_needs_in_front_of_it(self):
        blocker = {FLAT: OrientedGeometry({}, (-3.0, -3.0, 3.0, 3.0))}
        label_pin = {FLAT: OrientedGeometry({"1": PinGeometry((-5.08, 0.0), LEFT)}, (-5.08, -3.81, 1.27, 3.81))}
        symbols = [
            symbol("U1", label_pin, (50.8, 50.8)),                    # its pin points left, to a label
            symbol("X1", blocker, (38.1, 50.8), movable=True),        # 7.6 mm in front of the pin tip: inside the room
        ]
        reach = {("U1", "1"): 15.24}
        result = refine_placement(symbols, [], reach=reach)
        moved = result.placements["X1"].position
        self.assertGreater(result.before.clearance, 0)
        self.assertEqual(result.after.clearance, 0)
        # out of the strip in front of the pin, to either side or farther along it
        pin_tip = (45.72, 50.8)
        in_strip = pin_tip[0] - 15.24 < moved[0] + 3.0 and moved[0] - 3.0 < pin_tip[0] and abs(moved[1] - pin_tip[1]) < 3.0 + 1.27
        self.assertFalse(in_strip, moved)

    def test_room_is_not_reserved_in_front_of_a_pin_that_has_no_attachment(self):
        blocker = {FLAT: OrientedGeometry({}, (-3.0, -3.0, 3.0, 3.0))}
        pin = {FLAT: OrientedGeometry({"1": PinGeometry((-5.08, 0.0), LEFT)}, (-5.08, -3.81, 1.27, 3.81))}
        result = refine_placement(
            [symbol("U1", pin, (50.8, 50.8)), symbol("X1", blocker, (38.1, 50.8), movable=True)], [],
        )
        self.assertEqual(result.before.clearance, 0)


class BodyBoxTests(unittest.TestCase):
    """The room in front of a pin is kept clear of other symbols' real bodies, not of the padding the layout policy gives
    them (MCP Pro's boxes are at least 20 x 15 mm)."""

    def test_a_padded_box_does_not_count_where_the_body_is_clear(self):
        padded = {FLAT: OrientedGeometry({}, (-10.0, -10.0, 10.0, 10.0), body=(-2.0, -2.0, 2.0, 2.0))}
        label_pin = {FLAT: OrientedGeometry({"1": PinGeometry((-5.08, 0.0), LEFT)}, (-5.08, -3.81, 1.27, 3.81))}
        symbols = [
            symbol("U1", label_pin, (50.8, 50.8)),
            symbol("X1", padded, (26.0, 50.8)),          # box reaches into the room in front of the pin, body does not
        ]
        result = refine_placement(symbols, [], reach={("U1", "1"): 15.24})
        self.assertEqual(result.before.clearance, 0)

    def test_the_body_does_count_when_it_stands_in_the_room(self):
        padded = {FLAT: OrientedGeometry({}, (-10.0, -10.0, 10.0, 10.0), body=(-2.0, -2.0, 2.0, 2.0))}
        label_pin = {FLAT: OrientedGeometry({"1": PinGeometry((-5.08, 0.0), LEFT)}, (-5.08, -3.81, 1.27, 3.81))}
        symbols = [symbol("U1", label_pin, (50.8, 50.8)), symbol("X1", padded, (38.1, 50.8), movable=True)]
        result = refine_placement(symbols, [], reach={("U1", "1"): 15.24})
        self.assertGreater(result.before.clearance, 0)

    def test_rotating_a_geometry_turns_its_body_too(self):
        turned = rotate_geometry(OrientedGeometry({}, (-4.0, -2.0, 4.0, 2.0), body=(-3.0, -1.0, 3.0, 1.0)), 90, "")
        self.assertEqual(turned.box, (-2.0, -4.0, 2.0, 4.0))
        self.assertEqual(turned.body, (-1.0, -3.0, 1.0, 3.0))


class OverlapTests(unittest.TestCase):
    def test_a_movable_symbol_never_ends_on_another(self):
        box = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, -3.81), UP)}, (-3.0, -3.81, 3.0, 3.81))}
        symbols = [
            symbol("U1", box, (50.8, 50.8)), symbol("U2", box, (80.0, 50.8)),
            symbol("R1", resistor(), (66.04, 60.96), movable=True),
        ]
        nets = [Connection("A", (("U1", "1"), ("R1", "1")), "wire"), Connection("B", (("U2", "1"), ("R1", "2")), "wire")]
        result = refine_placement(symbols, nets)
        self.assertEqual(result.after.overlaps, 0)


def pin_up() -> dict[Orientation, OrientedGeometry]:
    """A part with one pin at its anchor pointing up."""
    return {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), UP)}, (-3.0, 0.0, 3.0, 5.0))}


class PinTipTests(unittest.TestCase):
    """`auto-v23b`, i2c_sensor: the estimate drew the connector's SDA wire straight up the column of the chip's pin tips,
    because the connector's pin stood under the first of them. No router draws that: the wire would touch every pin it
    passes. It is a short, and costs like one."""

    def test_a_wire_up_the_column_of_a_chips_pin_tips_is_a_short(self):
        # the chip's pins 1 and 2 are on its right edge, at x = 60.96; the connector's pin points up from under pin 1
        symbols = [symbol("U1", chip(), (50.8, 50.8)), symbol("J1", pin_up(), (60.96, 76.2))]
        nets = [Connection("SDA", (("J1", "1"), ("U1", "2")), "wire")]
        result = refine_placement(symbols, nets)
        self.assertEqual(result.before.shorts, 0)             # to pin 2 itself: its own pin
        nets = [Connection("SDA", (("J1", "1"), ("U1", "1")), "wire")]
        result = refine_placement(symbols, nets)
        self.assertEqual(result.before.shorts, 1)             # to pin 1, past pin 2
        self.assertEqual(result.details["before"]["short"], [["SDA", "U1", "2"]])

    def test_a_pin_that_joins_a_net_up_the_column_of_another_pins_tips_makes_a_short_not_just_a_crossing(self):
        # `auto-v23d`, i2c_sensor: SDA is the chip's pin 1, a pull-up and the connector; the connector's pin stood under
        # pin 1 and joined the run from there, up the column the chip's other pins end on
        pull_up = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), DOWN)}, (-3.0, -5.0, 3.0, 0.0))}
        symbols = [
            symbol("U1", chip(), (50.8, 50.8)), symbol("R1", pull_up, (73.66, 48.26)), symbol("J1", pin_up(), (60.96, 76.2)),
        ]
        nets = [Connection("SDA", (("U1", "1"), ("R1", "1"), ("J1", "1")), "wire")]
        result = refine_placement(symbols, nets)
        # The estimate once drew the run up the column and the cost counted the short. It now prices a short before it
        # chooses, so it takes the cheaper charge, a pass through the pull-up (30) over the short (60).
        self.assertEqual(result.before.shorts, 0)
        self.assertEqual(result.before.through, 1)

    def test_a_short_costs_more_than_the_detour_that_avoids_it(self):
        weights = Weights()
        self.assertGreater(weights.short, 4 * weights.bend)

    def test_a_wire_along_the_edge_of_another_symbol_runs_through_it(self):
        left, right = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), RIGHT)}, (-3.0, -3.0, 0.0, 3.0))}, {
            FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT)}, (0.0, -3.0, 3.0, 3.0))}
        nets = [Connection("A", (("L", "1"), ("R", "1")), "wire")]
        edge = {FLAT: OrientedGeometry({}, (-4.0, 0.0, 4.0, 8.0))}          # its top edge lies on the wire's row
        clear = {FLAT: OrientedGeometry({}, (-4.0, 2.0, 4.0, 10.0))}         # a couple of millimetres below it
        touching = refine_placement(
            [symbol("L", left, (10.0, 20.0)), symbol("R", right, (40.0, 20.0)), symbol("X", edge, (25.0, 20.0))], nets,
        )
        apart = refine_placement(
            [symbol("L", left, (10.0, 20.0)), symbol("R", right, (40.0, 20.0)), symbol("X", clear, (25.0, 20.0))], nets,
        )
        self.assertEqual(touching.before.through, 1)
        self.assertEqual(apart.before.through, 0)

    def test_a_trunk_along_the_tips_of_the_parts_hung_from_it_is_not_through_them(self):
        # a rail drawn along the pin tips of two parts that hang from it: the box includes the leads, so its edge is the
        # tip's row, and the trunk touches it. A wire 1 mm inside a body is still through.
        source = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), RIGHT)}, (-3.0, -3.0, 0.0, 3.0))}
        hung = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), UP)}, (-3.0, 0.0, 3.0, 8.0))}
        nets = [Connection("RAIL", (("S", "1"), ("C1", "1"), ("C2", "1")), "wire")]
        symbols = [
            symbol("S", source, (10.0, 20.0)), symbol("C1", hung, (25.0, 20.0)), symbol("C2", hung, (40.0, 20.0)),
        ]
        model = _Model(symbols, nets, {}, Weights(), None)
        self.assertEqual(model.cost({s.reference: (s.position, s.orientation) for s in symbols}).through, 0)
        inside = {FLAT: OrientedGeometry({}, (-2.0, -40.0, 2.0, 40.0))}     # across the trunk and every row it could take
        crossed = symbols + [symbol("X", inside, (17.5, 20.0))]
        model = _Model(crossed, nets, {}, Weights(), None)
        self.assertEqual(model.cost({s.reference: (s.position, s.orientation) for s in crossed}).through, 1)

    def test_a_wire_does_not_run_through_the_symbols_it_joins(self):
        symbols = [symbol("U1", chip(), (50.8, 50.8)), symbol("J1", pin_up(), (80.0, 90.0))]
        nets = [Connection("SDA", (("J1", "1"), ("U1", "1")), "wire")]
        result = refine_placement(symbols, nets)
        self.assertEqual((result.before.through, result.before.shorts), (0, 0))

    def test_each_route_of_a_net_knows_the_pins_it_joins(self):
        routes = estimate_net([((0.0, 0.0), RIGHT), ((30.48, 0.0), LEFT), ((15.24, -10.16), DOWN)])
        self.assertEqual(sorted(routes[0].pins), [0, 1])              # the run between the first two
        self.assertEqual(routes[1].pins, (2,))                         # a T that ends on the run, not on a pin


class StubTests(unittest.TestCase):
    """A pin whose net is drawn as a label or a power symbol has a stub, and a wire across it is a crossing."""

    def test_a_wire_across_a_label_stub_is_a_crossing(self):
        wire_ends = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), RIGHT)}, (-3.0, -3.0, 0.0, 3.0))}
        other_end = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT)}, (0.0, -3.0, 3.0, 3.0))}
        labelled = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), UP)}, (-3.0, 0.0, 3.0, 5.0))}
        symbols = [
            symbol("A", wire_ends, (10.0, 20.0)), symbol("B", other_end, (40.0, 20.0)),
            symbol("P", labelled, (25.0, 24.0)),                      # its stub runs up from y = 24 to y = 18.9: across the wire
        ]
        nets = [Connection("W", (("A", "1"), ("B", "1")), "wire"), Connection("GND", (("P", "1"),), "power")]
        result = refine_placement(symbols, nets)
        self.assertEqual(result.before.crossings, 1)
        self.assertEqual(result.details["before"]["crossing"], [["W", "GND"]])

    def test_a_stub_that_points_away_from_the_wire_does_not_cross_it(self):
        wire_ends = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), RIGHT)}, (-3.0, -3.0, 0.0, 3.0))}
        other_end = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT)}, (0.0, -3.0, 3.0, 3.0))}
        labelled = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), DOWN)}, (-3.0, -5.0, 3.0, 0.0))}
        symbols = [
            symbol("A", wire_ends, (10.0, 20.0)), symbol("B", other_end, (40.0, 20.0)), symbol("P", labelled, (25.0, 24.0)),
        ]
        nets = [Connection("W", (("A", "1"), ("B", "1")), "wire"), Connection("GND", (("P", "1"),), "power")]
        self.assertEqual(refine_placement(symbols, nets).before.crossings, 0)


def wire_ends():
    left = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), RIGHT)}, (-3.0, -3.0, 0.0, 3.0))}
    right = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT)}, (0.0, -3.0, 3.0, 3.0))}
    return left, right


class RoomForWiresTests(unittest.TestCase):
    """`auto-v23b`, rc_filter: the output connector's ground pin pointed at the filter's output wire six millimetres in
    front of it, so the ground symbol had to be drawn beyond the wire and its stub crossed it."""

    def pin_facing_left(self):
        return {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT)}, (0.0, -3.0, 3.0, 3.0))}

    def wire(self, x):
        """A vertical wire of net W at ``x``, from a part above to a part below."""
        top = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), DOWN)}, (-3.0, -5.0, 3.0, 0.0))}
        bottom = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), UP)}, (-3.0, 0.0, 3.0, 5.0))}
        return [symbol("T", top, (x, 10.0)), symbol("B", bottom, (x, 40.0))]

    def test_a_wire_of_another_net_in_front_of_a_power_pin_takes_its_room(self):
        symbols = [symbol("P", self.pin_facing_left(), (50.0, 25.0)), *self.wire(44.0)]
        nets = [Connection("W", (("T", "1"), ("B", "1")), "wire"), Connection("GND", (("P", "1"),), "power")]
        result = refine_placement(symbols, nets, reach={("P", "1"): POWER_REACH})
        self.assertEqual(result.details["before"]["clearance"], [["P.1", "wire W"]])

    def test_a_wire_beyond_the_room_does_not(self):
        symbols = [symbol("P", self.pin_facing_left(), (50.0, 25.0)), *self.wire(38.0)]
        nets = [Connection("W", (("T", "1"), ("B", "1")), "wire"), Connection("GND", (("P", "1"),), "power")]
        result = refine_placement(symbols, nets, reach={("P", "1"): POWER_REACH})
        self.assertNotIn("clearance", result.details["before"])

    def test_the_wire_a_pin_is_joined_to_is_meant_to_be_in_front_of_it(self):
        symbols = [symbol("P", self.pin_facing_left(), (50.0, 25.0)), *self.wire(44.0)]
        nets = [Connection("W", (("T", "1"), ("B", "1"), ("P", "1")), "wire")]
        result = refine_placement(symbols, nets, reach={("P", "1"): WIRE_REACH})
        self.assertNotIn("clearance", result.details["before"])

    def test_a_power_symbol_is_as_wide_as_the_name_it_shows(self):
        # a body two millimetres to one side of the stub: clear of a wire's width, inside a symbol's
        blocker = {FLAT: OrientedGeometry({}, (-1.0, -1.0, 1.0, 1.0))}
        symbols = [symbol("P", self.pin_facing_left(), (50.0, 25.0)), symbol("X", blocker, (44.0, 27.5))]
        nets = [Connection("GND", (("P", "1"),), "power")]
        reach = {("P", "1"): POWER_REACH}
        narrow = refine_placement(symbols, nets, reach=reach)
        wide = refine_placement(symbols, nets, reach=reach, half_widths={("P", "1"): 3.0})
        self.assertNotIn("clearance", narrow.details["before"])
        self.assertEqual(wide.details["before"]["clearance"], [["P.1", "X"]])


POWER_REACH = 7.62
WIRE_REACH = 4.445


class PowerFacingTests(unittest.TestCase):
    """The terminal planner draws a ground symbol hanging from the wire above it and a supply symbol standing on the wire
    below: a pin that points the way the symbol is entered costs nothing, one that points across costs a turn."""

    def pin_pointing(self, direction):
        return {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), direction)}, (-3.0, -3.0, 3.0, 3.0))}

    def total(self, direction, facing, style="power"):
        result = refine_placement(
            [symbol("P", self.pin_pointing(direction), (50.0, 25.0))],
            [Connection("GND", (("P", "1"),), style, facing=facing)],
        )
        return result.before.total

    def test_a_ground_pin_pointing_down_costs_nothing_and_across_or_up_costs_more(self):
        down = self.total(DOWN, DOWN)
        self.assertLess(down, self.total(LEFT, DOWN))
        self.assertLess(self.total(LEFT, DOWN), self.total(UP, DOWN))
        self.assertAlmostEqual(self.total(LEFT, DOWN) - down, Weights().power_sideways)
        self.assertAlmostEqual(self.total(UP, DOWN) - down, Weights().power_away)

    def test_a_supply_pin_wants_to_point_up(self):
        self.assertLess(self.total(UP, UP), self.total(DOWN, UP))

    def test_no_preference_without_a_direction(self):
        self.assertEqual(self.total(DOWN, None), self.total(UP, None))

    def test_a_two_pin_part_turns_to_put_its_ground_pin_down(self):
        # a resistor standing on a wire from above, its other end to ground: the ground pin ends at the bottom
        top = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), DOWN)}, (-3.0, -5.0, 3.0, 0.0))}
        symbols = [
            symbol("T", top, (50.8, 12.7)), symbol("R", resistor(), (50.8, 26.67), orientation=Orientation(180), movable=True),
        ]
        nets = [
            Connection("A", (("T", "1"), ("R", "1")), "wire"),
            Connection("GND", (("R", "2"),), "power", facing=DOWN),
        ]
        result = refine_placement(symbols, nets)
        turned = result.placements["R"].orientation
        self.assertEqual(resistor()[turned].pins["2"].normal, DOWN)


class VerticalLabelTests(unittest.TestCase):
    def test_a_label_on_a_vertical_stub_costs_a_little_more(self):
        up = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), UP)}, (-3.0, 0.0, 3.0, 3.0))}
        side = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), RIGHT)}, (-3.0, -3.0, 0.0, 3.0))}
        far = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT)}, (0.0, -3.0, 3.0, 3.0))}

        def total(host):
            return refine_placement(
                [symbol("A", host, (10.0, 20.0)), symbol("B", far, (40.0, 20.0))],
                [Connection("SIG", (("A", "1"), ("B", "1")), "label")],
            ).before.total

        # a vertical stub also points away from the other end, so compare with the same pointing away sideways
        away = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, 0.0), LEFT)}, (0.0, -3.0, 3.0, 3.0))}
        self.assertGreater(total(up) - total(side), 0.0)
        self.assertGreater(total(up), total(away) - Weights().label_facing)


class DetailTests(unittest.TestCase):
    def test_the_result_lists_what_each_term_found_before_and_after(self):
        result = refine_placement(
            [symbol("U1", chip(), (50.8, 50.8)), symbol("J1", connector(), (76.2, 83.82), orientation=Orientation(90, "x"),
                                                         movable=True, ordered=True)],
            BusConnectorTests.NETS,
        )
        self.assertEqual(result.details["before"]["crossing"], [["SDA", "SCL"]])
        self.assertNotIn("crossing", result.details["after"])


class WindowTests(unittest.TestCase):
    """`auto-v23d`: the support parts a relation placed beside a pin were fixed, so a decoupling capacitor stacked on the
    pin it serves took the room of that pin's power symbol and nothing could move it. They may be nudged, not moved away."""

    NETS = [Connection("SDA", (("U1", "1"), ("R1", "2")), "wire")]

    def run_with(self, window):
        # a resistor standing upright, as the relation turned it, a few cells off the row its pin should be on
        upright = {FLAT: resistor()[FLAT]}
        part = symbol("R1", upright, (76.2, 41.91), movable=True)
        part = PlacedSymbol(part.reference, part.position, part.orientation, part.geometry, movable=True, window=window)
        return refine_placement([symbol("U1", chip(), (50.8, 50.8)), part], self.NETS)

    def test_a_symbol_with_a_window_never_leaves_it(self):
        for window in (1.27, 2.54, 5.08):
            result = self.run_with(window)
            moved = result.placements.get("R1")
            if moved is not None:
                self.assertLessEqual(abs(moved.position[0] - 76.2), window + 0.7, window)
                self.assertLessEqual(abs(moved.position[1] - 41.91), window + 0.7, window)      # the grid may round it a cell

    def test_a_symbol_with_a_window_keeps_the_orientation_it_has(self):
        result = self.run_with(5.08)
        if "R1" in result.placements:
            self.assertEqual(result.placements["R1"].orientation, FLAT)

    def test_a_bigger_window_can_reach_a_better_place(self):
        small, big = self.run_with(1.27), self.run_with(7.62)
        self.assertLessEqual(big.after.total, small.after.total + 1e-9)

    def test_a_window_is_a_limit_on_movement_not_on_staying(self):
        result = self.run_with(0.0)
        self.assertEqual(result.placements, {})
        self.assertEqual(result.after.total, result.before.total)

    def test_the_window_is_carried_from_the_plan(self):
        from backend.schematic.placement_plan import symbols_from_payload

        entry = {"x": 10.0, "y": 20.0, "orientations": [
            {"rotation": 0, "mirror": "", "pins": {"1": [0.0, -3.81, 0.0, -1.0]}, "box": [-1.0, -3.81, 1.0, 3.81]},
        ]}
        placed = symbols_from_payload(
            {"symbols": {"R1": entry, "U1": entry}}, planned={}, movable={"R1"}, windows={"R1": 6.35},
        )
        windows = {symbol_.reference: symbol_.window for symbol_ in placed}
        self.assertEqual(windows, {"R1": 6.35, "U1": None})


class SeedTests(unittest.TestCase):
    """`auto-demo1`: MCP Pro scatters every symbol over the sheet and a part no relation places stays where it fell, so a
    block of eight parts measured 210 x 142 mm and the floorplan was made of boxes that size."""

    def part(self, reference, position, **kw):
        return symbol(reference, {FLAT: resistor()[FLAT]}, position, movable=True, **kw)

    def test_a_symbol_standing_far_from_what_it_connects_to_is_brought_in_beside_it(self):
        symbols = [symbol("U1", chip(), (50.8, 50.8)), self.part("R1", (300.0, 250.0))]
        result = seed_placement(symbols, [Connection("SDA", (("U1", "1"), ("R1", "2")), "wire")])
        placed = result.placements["R1"].position
        assert abs(placed[0] - 60.96) + abs(placed[1] - 48.26) < 40, placed          # next to pin 1, not 250 mm away
        self.assertLess(result.after.length, result.before.length / 5)

    def test_a_symbol_already_beside_what_it_connects_to_stays(self):
        symbols = [symbol("U1", chip(), (50.8, 50.8)), self.part("R1", (76.2, 45.72))]
        result = seed_placement(symbols, [Connection("SDA", (("U1", "1"), ("R1", "2")), "wire")])
        self.assertEqual(result.placements, {})

    def test_a_chain_comes_in_one_link_at_a_time(self):
        symbols = [
            symbol("U1", chip(), (50.8, 50.8)), self.part("R1", (400.0, 100.0)), self.part("R2", (500.0, 400.0)),
        ]
        nets = [
            Connection("A", (("U1", "1"), ("R1", "1")), "wire"), Connection("B", (("R1", "2"), ("R2", "1")), "wire"),
        ]
        result = seed_placement(symbols, nets)
        r1, r2 = result.placements["R1"].position, result.placements["R2"].position
        assert abs(r1[0] - 60.96) < 60 and abs(r1[1] - 48.26) < 60, r1
        assert abs(r2[0] - r1[0]) < 60 and abs(r2[1] - r1[1]) < 60, (r1, r2)           # R2 near R1, not near U1's old place

    def test_the_symbols_it_brings_in_never_stand_on_each_other_or_on_what_is_there(self):
        symbols = [symbol("U1", chip(), (50.8, 50.8))] + [self.part(f"R{n}", (300.0 + 40 * n, 300.0)) for n in range(1, 5)]
        nets = [Connection(f"N{n}", (("U1", "1"), (f"R{n}", "1")), "wire") for n in range(1, 5)]
        result = seed_placement(symbols, nets)
        self.assertEqual(result.after.overlaps, 0)

    def test_a_block_keeps_to_itself(self):
        # R1 connects to U1 but belongs to another block than U1: it is not brought across to it
        symbols = [symbol("U1", chip(), (50.8, 50.8)), self.part("R1", (300.0, 250.0)), self.part("R2", (310.0, 250.0))]
        nets = [
            Connection("A", (("U1", "1"), ("R1", "1")), "wire"), Connection("B", (("R1", "2"), ("R2", "1")), "wire"),
        ]
        result = seed_placement(symbols, nets, groups={"U1": "chip", "R1": "load", "R2": "load"})
        self.assertNotIn("R1", result.placements)             # nothing of its own block is in place to go to
        self.assertNotIn("R2", result.placements)

    def test_a_symbol_with_nothing_to_connect_to_is_packed_beside_its_block(self):
        symbols = [
            symbol("U1", chip(), (50.8, 50.8)), self.part("R1", (76.2, 45.72)), self.part("V1", (400.0, 300.0)),
        ]
        nets = [Connection("A", (("U1", "1"), ("R1", "2")), "wire")]
        result = seed_placement(symbols, nets, groups={"U1": "g", "R1": "g", "V1": "g"})
        placed = result.placements["V1"].position
        assert abs(placed[0] - 60) < 80 and abs(placed[1] - 50) < 80, placed

    def test_a_support_part_with_a_window_is_left_where_the_relation_put_it(self):
        far = PlacedSymbol("R1", (300.0, 250.0), FLAT, {FLAT: resistor()[FLAT]}, movable=True, window=6.35)
        result = seed_placement(
            [symbol("U1", chip(), (50.8, 50.8)), far], [Connection("SDA", (("U1", "1"), ("R1", "2")), "wire")],
        )
        self.assertEqual(result.placements, {})

    def test_nothing_movable_is_a_no_op(self):
        result = seed_placement([symbol("U1", chip(), (50.8, 50.8))], [])
        self.assertEqual(result.placements, {})

    def test_the_same_design_seeds_the_same_way_every_time(self):
        def run():
            symbols = [symbol("U1", chip(), (50.8, 50.8)), self.part("R1", (300.0, 250.0)), self.part("R2", (320.0, 250.0))]
            nets = [Connection("A", (("U1", "1"), ("R1", "1")), "wire"), Connection("B", (("U1", "2"), ("R2", "1")), "wire")]
            return seed_placement(symbols, nets).placements
        self.assertEqual(run(), run())


class AlignGroupsTests(unittest.TestCase):
    """`auto-demo4` (the buck converter): the floorplan leaves each block where the layered layout dropped it, so the
    wire from one block to the next steps between two pins that could have shared a row."""

    def blocks(self, second_at):
        left = symbol("U1", chip(), (50.8, 50.8))
        right = symbol("R1", {FLAT: resistor()[Orientation(90)]}, second_at)         # lying: its pins point left and right
        other = symbol("R2", {FLAT: resistor()[Orientation(90)]}, (second_at[0] + 25.4, second_at[1]))
        nets = [
            Connection("A", (("U1", "2"), ("R1", "1")), "wire"),
            Connection("B", (("R1", "2"), ("R2", "1")), "wire"),
        ]
        return [left, right, other], nets, {"U1": "chip", "R1": "load", "R2": "load"}

    def test_a_block_slides_until_its_wire_to_the_next_is_straight(self):
        symbols, nets, groups = self.blocks((86.36, 63.5))
        result = align_groups(symbols, nets, groups=groups)
        self.assertGreater(result.before.bends, 0)
        self.assertEqual(result.after.bends, 0)
        self.assertLess(result.after.total, result.before.total)

    def test_the_symbols_of_a_block_move_together(self):
        symbols, nets, groups = self.blocks((86.36, 63.5))
        result = align_groups(symbols, nets, groups=groups)
        before = {s.reference: s.position for s in symbols}
        self.assertTrue(result.placements)                        # one of the two blocks slid
        for name in ("chip", "load"):
            members = [ref for ref, group in groups.items() if group == name]
            slides = {
                tuple(round(v, 4) for v in (
                    (result.placements[ref].position if ref in result.placements else before[ref])[0] - before[ref][0],
                    (result.placements[ref].position if ref in result.placements else before[ref])[1] - before[ref][1],
                ))
                for ref in members
            }
            self.assertEqual(len(slides), 1, (name, slides))      # every part of a block slid the same way (or none did)

    def test_a_block_never_lands_on_another(self):
        symbols, nets, groups = self.blocks((86.36, 63.5))
        result = align_groups(symbols, nets, groups=groups)
        self.assertEqual(result.after.overlaps, 0)

    def test_a_symbol_in_no_group_stays_where_it_is(self):
        symbols, nets, groups = self.blocks((86.36, 63.5))
        result = align_groups(symbols, nets, groups={"R1": "load", "R2": "load"})
        self.assertNotIn("U1", result.placements)

    def test_one_group_or_none_is_a_no_op(self):
        symbols, nets, _groups = self.blocks((86.36, 63.5))
        only = align_groups(symbols, nets, groups={"U1": "a", "R1": "a", "R2": "a"})
        self.assertEqual(only.placements, {})
        self.assertEqual(align_groups(symbols, nets, groups={}).placements, {})

    def test_blocks_already_in_line_stay(self):
        symbols, nets, groups = self.blocks((86.36, 50.8 + 2.54))
        result = align_groups(symbols, nets, groups=groups)
        self.assertLessEqual(result.after.total, result.before.total + 1e-9)
        # the pin of U1 is at y = 50.8, the resistor's pins on its row: already as straight as the geometry allows
        self.assertEqual(result.after.bends, 0)

    def test_a_block_does_not_slide_to_the_far_side_of_the_one_it_feeds(self):
        # R1's block sits to the right of the chip's; sliding it left past the chip would shorten the wire, but the signal
        # flows from the chip to it
        left = symbol("U1", chip(), (100.0, 50.8))
        right = symbol("R1", {FLAT: resistor()[Orientation(90)]}, (130.0, 63.5))
        nets = [Connection("A", (("U1", "2"), ("R1", "1")), "wire")]
        groups = {"U1": "chip", "R1": "load"}
        free = align_groups([left, right], nets, groups=groups)
        kept = align_groups([left, right], nets, groups=groups, order=[("chip", "load")])
        for result in (free, kept):
            self.assertLessEqual(result.after.total, result.before.total + 1e-9)
        placed = {s.reference: s.position for s in (left, right)}
        for ref, place in kept.placements.items():
            placed[ref] = place.position
        self.assertLessEqual(placed["U1"][0], placed["R1"][0])               # still before it

    def test_the_search_is_bounded_and_never_worse_than_the_start(self):
        symbols, nets, groups = self.blocks((86.36, 120.0))
        result = align_groups(symbols, nets, groups=groups, budget=200)
        self.assertLessEqual(result.after.total, result.before.total + 1e-9)
        self.assertLess(result.evaluated, 200 + 60)


class BudgetTests(unittest.TestCase):
    """A sheet of many movable symbols (a netlist with nothing declared) must not take minutes."""

    def many(self, count):
        symbols = [symbol("U1", chip(), (50.8, 50.8))]
        nets = []
        for index in range(count):
            reference = f"R{index + 1}"
            symbols.append(symbol(reference, resistor(), (76.2 + 5.08 * index, 76.2), orientation=Orientation(0), movable=True))
            nets.append(Connection(f"N{index}", ((reference, "1"), ("U1", "1" if index % 2 else "2")), "wire"))
        return symbols, nets

    def test_the_search_stops_when_the_budget_is_spent_and_is_never_worse_than_the_start(self):
        symbols, nets = self.many(8)
        result = refine_placement(symbols, nets, budget=3000)
        self.assertLessEqual(result.after.total, result.before.total + 1e-9)
        self.assertLess(result.evaluated, 3000 + 2 * 4 * 12 * 3 + 200)          # one symbol's lattice past it at most

    def test_the_same_design_gives_the_same_answer_whatever_the_budget_does_not_reach(self):
        symbols, nets = self.many(3)
        small = refine_placement(symbols, nets, budget=10_000_000)
        again = refine_placement(symbols, nets, budget=10_000_000)
        self.assertEqual(small.placements, again.placements)


class StabilityTests(unittest.TestCase):
    def test_nothing_movable_is_a_no_op(self):
        result = refine_placement([symbol("U1", chip(), (50.8, 50.8))], [])
        self.assertEqual(result.placements, {})
        self.assertEqual(result.after.total, result.before.total)

    def test_the_result_is_the_same_every_time(self):
        def run():
            return refine_placement(
                [symbol("U1", chip(), (50.8, 50.8)), symbol("J1", connector(), (76.2, 83.82), orientation=Orientation(90, "x"),
                                                             movable=True, ordered=True)],
                BusConnectorTests.NETS,
            ).placements
        self.assertEqual(run(), run())

    def test_weights_are_tunable(self):
        weights = Weights(bend=0.0, crossing=0.0)
        result = refine_placement(
            [symbol("U1", chip(), (50.8, 50.8)), symbol("J1", connector(), (76.2, 83.82), orientation=Orientation(90, "x"),
                                                         movable=True, ordered=True)],
            BusConnectorTests.NETS, weights=weights,
        )
        self.assertIsNotNone(result.after)


class JogTests(unittest.TestCase):
    """`auto-demo10` (the buck converter): the capacitor and the load stood a grid step below the inductor's row, so the
    wire from the inductor stepped 1.27 mm down into the capacitor and went on along its pin tips: a jog."""

    def test_a_leg_a_grid_step_long_between_two_turns_is_a_jog(self):
        route = estimate_route((0.0, 0.0), RIGHT, (3.81, 1.27), UP)          # right, then down into a pin that points up
        self.assertEqual(route.bends, 1)
        self.assertEqual(count_jogs(route), 1)

    def test_a_leg_of_two_grid_steps_is_not(self):
        route = estimate_route((0.0, 0.0), RIGHT, (6.35, 2.54), UP)
        self.assertEqual(count_jogs(route), 0)

    def test_the_stub_of_a_T_that_short_is_a_jog_and_a_short_wire_between_two_pins_is_not(self):
        stub = Route(((5.08, 1.27), (5.08, 0.0)), 0, 1.27, pins=(2,))
        wire = Route(((0.0, 0.0), (0.0, 1.27)), 0, 1.27, pins=(0, 1))
        self.assertEqual(count_jogs(stub), 1)
        self.assertEqual(count_jogs(wire), 0)

    def test_the_cost_of_a_pose_counts_its_jogs(self):
        # a resistor whose pin tip is one grid step off the row of the chip's pin, and two grid steps
        def cost(rise):
            symbols = [symbol("U1", chip(), (50.8, 50.8)), symbol("R1", {FLAT: resistor()[Orientation(90)]}, (80.0, 50.8 + rise))]
            nets = [Connection("A", (("U1", "2"), ("R1", "1")), "wire")]
            model = _Model(symbols, nets, {}, Weights(), None)
            return model.cost({s.reference: (s.position, s.orientation) for s in symbols})
        near, apart = cost(1.27), cost(2.54)
        self.assertEqual(near.jogs, 1)
        self.assertEqual(apart.jogs, 0)


class TurnPenaltyTests(unittest.TestCase):
    """A symbol turned away from the orientation it was planned to have pays for it, so that it turns for a reason."""

    def pose_cost(self, geometry, orientation, movable=True, **weights):
        part = symbol("P1", geometry, (50.8, 50.8), orientation=Orientation(0), movable=movable)
        model = _Model([part], [], {}, Weights(extent=0.0, move=0.0, **weights), None)
        return model.cost({"P1": ((50.8, 50.8), orientation)}).total

    def test_a_quarter_turn_costs_the_turn_weight_and_a_half_turn_twice_that(self):
        def cost(rotation):
            return self.pose_cost(resistor(), Orientation(rotation), turn=3.0)

        self.assertAlmostEqual(cost(90) - cost(0), 3.0)
        self.assertAlmostEqual(cost(270) - cost(0), 3.0)
        self.assertAlmostEqual(cost(180) - cost(0), 6.0)

    def test_a_mirror_costs_the_flip_weight(self):
        def cost(orientation):
            return self.pose_cost(connector(3), orientation, flip=4.0)

        self.assertAlmostEqual(cost(Orientation(0, "x")) - cost(Orientation(0)), 4.0)

    def test_a_part_that_is_not_movable_is_not_charged(self):
        def cost(rotation):
            return self.pose_cost(resistor(), Orientation(rotation), movable=False, turn=3.0)

        self.assertEqual(cost(90), cost(0))


class TurnForAStraightWireTests(unittest.TestCase):
    """The buck converter's MOSFET: drawn upright its source points down while the inductor it feeds is to the right; turned,
    the wire between them is straight."""

    def transistor(self):
        upright = OrientedGeometry(
            {"2": PinGeometry((0.0, -5.08), UP), "3": PinGeometry((0.0, 5.08), DOWN)}, (-3.0, -5.08, 3.0, 5.08),
        )
        return {Orientation(r, m): rotate_geometry(upright, r, m) for r in (0, 90, 180, 270) for m in ("", "x", "y")}

    def inductor_pin_left(self):
        return {FLAT: OrientedGeometry({"1": PinGeometry((-3.81, 0.0), LEFT)}, (-3.81, -1.0, 3.81, 1.0))}

    def test_the_part_turns_when_that_saves_a_bend(self):
        nets = [Connection("SW", (("Q1", "3"), ("L1", "1")), "wire")]
        parts = [
            symbol("Q1", self.transistor(), (40.64, 50.8), movable=True),
            symbol("L1", self.inductor_pin_left(), (76.2, 38.1)),
        ]
        result = refine_placement(parts, nets)
        self.assertIn("Q1", result.placements)
        turned = result.placements["Q1"].orientation
        self.assertNotEqual(turned, FLAT)
        self.assertEqual(self.transistor()[turned].pins["3"].normal, RIGHT)      # the source now points at the inductor
        self.assertEqual(result.after.bends, 0)

    def test_a_part_with_nothing_to_join_is_not_turned(self):
        result = refine_placement([symbol("Q1", self.transistor(), (40.64, 50.8), movable=True)], [])
        self.assertEqual(result.placements, {})


class RowsMoveTogetherTests(unittest.TestCase):
    """The buck converter's inductor and load: aligned with each other by a straight wire, and off the row of the switch
    node. Sliding the inductor's block alone brakes the wire to the load, and the load's alone brakes the wire to the
    inductor: only the two together reach the row."""

    def build(self, lower=30.48):
        lying = {FLAT: resistor()[Orientation(90)]}
        wall = {FLAT: OrientedGeometry({}, (-2.0, -60.0, 2.0, 60.0))}
        parts = [
            symbol("RA", lying, (20.32, 0.0)),
            symbol("RB", lying, (45.72, lower)),
            symbol("RC", lying, (60.96, lower)),
            symbol("RX", lying, (20.32, 30.48)),              # stands where RA would have to go to meet RB
            symbol("RW", wall, (30.48, 15.24)),               # and the way round it is shut: RA cannot move right
        ]
        nets = [
            Connection("AB", (("RA", "2"), ("RB", "1")), "wire"),
            Connection("BC", (("RB", "2"), ("RC", "1")), "wire"),
        ]
        return parts, nets, {"RA": "a", "RB": "b", "RC": "c"}

    def test_two_blocks_joined_by_a_straight_wire_slide_together_to_a_third(self):
        parts, nets, groups = self.build()
        result = align_groups(parts, nets, groups=groups)
        self.assertGreater(result.before.bends, 0)
        self.assertEqual(result.after.bends, 0)
        row = {ref: result.placements[ref].position[1] for ref in ("RB", "RC")}
        self.assertAlmostEqual(row["RB"], row["RC"])             # they moved as a row
        self.assertAlmostEqual(row["RB"], result.placements["RA"].position[1] if "RA" in result.placements else 0.0)

    def test_a_row_already_in_line_does_not_leave_it(self):
        parts, nets, groups = self.build(lower=0.0)
        result = align_groups(parts, nets, groups=groups)
        self.assertEqual(result.before.bends, 0)
        self.assertEqual(result.after.bends, 0)
        for placement in result.placements.values():              # at most closer along the row, never off it
            self.assertAlmostEqual(placement.position[1], 0.0)


class OwnBodyTests(unittest.TestCase):
    """`auto-st2` (the LM317 supply): the transformer stood below the bridge rectifier, and the wire from its upper terminal to
    the bridge's top pin was estimated straight up through the bridge: free, because the bridge is one of the wire's own ends.
    No router can draw that. It went round the bridge and crossed two wires, in a sheet the estimate had counted as clean."""

    def bridge(self):
        """A diamond: the DC pins left and right, the AC pins above and below."""
        pins = {
            "1": PinGeometry((7.62, 0.0), RIGHT), "2": PinGeometry((-7.62, 0.0), LEFT),
            "3": PinGeometry((0.0, -7.62), UP), "4": PinGeometry((0.0, 7.62), DOWN),
        }
        return {FLAT: OrientedGeometry(pins, (-7.62, -7.62, 7.62, 7.62))}

    def secondary(self):
        """The two terminals of a winding on the right of a transformer, pointing right: 4 above 3."""
        pins = {"4": PinGeometry((5.08, -3.81), RIGHT), "3": PinGeometry((5.08, 3.81), RIGHT)}
        return {FLAT: OrientedGeometry(pins, (-5.08, -5.08, 5.08, 5.08))}

    NETS = [
        Connection("N3", (("D1", "3"), ("T1", "4")), "wire"),
        Connection("N4", (("D1", "4"), ("T1", "3")), "wire"),
    ]

    def cost(self, transformer_at, movable=False):
        symbols = [
            symbol("D1", self.bridge(), (50.8, 50.8)), symbol("T1", self.secondary(), transformer_at, movable=movable),
        ]
        model = _Model(symbols, self.NETS, {}, Weights(), None)
        return symbols, model.cost({s.reference: (s.position, s.orientation) for s in symbols})

    def test_a_wire_to_the_far_pin_of_the_part_it_joins_cannot_run_through_that_part(self):
        _symbols, below = self.cost((39.37, 66.04))              # the transformer below the bridge, its terminals left of it
        self.assertGreaterEqual(below.through, 1)

    def test_the_wires_of_a_transformer_beside_the_bridge_leave_and_arrive_along_the_pins(self):
        # the upper terminal is above the bridge's top pin and the lower below its bottom pin: an L each, nothing through
        _symbols, beside = self.cost((25.4, 50.8))
        self.assertEqual(beside.through, 0)
        self.assertEqual(beside.crossings, 0)

    def test_the_search_moves_the_transformer_to_where_its_wires_can_be_drawn(self):
        symbols, start = self.cost((39.37, 66.04), movable=True)
        result = refine_placement(symbols, self.NETS)
        self.assertGreaterEqual(start.through, 1)
        self.assertEqual(result.after.through, 0)
        self.assertLess(result.after.total, result.before.total)

    def test_a_wire_from_a_pin_that_points_away_cannot_run_across_its_own_part(self):
        # the pin of the left part points left, away from the right one: the estimated Z crosses the part it leaves
        left = {FLAT: OrientedGeometry({"1": PinGeometry((-5.08, 0.0), LEFT)}, (-5.08, -3.81, 3.81, 3.81))}
        right = {FLAT: OrientedGeometry({"1": PinGeometry((0.0, -3.81), UP)}, (-3.0, -3.81, 3.0, 3.81))}
        symbols = [symbol("A1", left, (50.8, 50.8)), symbol("B1", right, (50.8 + 25.4, 50.8 + 3.81 + 3.81))]
        nets = [Connection("S", (("A1", "1"), ("B1", "1")), "wire")]
        model = _Model(symbols, nets, {}, Weights(), None)
        self.assertGreaterEqual(model.cost({s.reference: (s.position, s.orientation) for s in symbols}).through, 1)

    def test_the_leg_out_of_a_pin_is_not_through_a_drawing_that_reaches_past_the_pin_tip(self):
        # `led_switch`: the LED's arrows are drawn 0.76 mm beyond its cathode's tip, and the straight wire down to the
        # transistor starts there. It is the way out of the pin, not a way through the LED.
        led = {FLAT: OrientedGeometry(
            {"1": PinGeometry((0.0, 3.81), DOWN), "2": PinGeometry((0.0, -3.81), UP)},
            (-2.54, -5.08, 3.556, 5.842), body=(-1.27, -3.81, 2.286, 4.572),
        )}
        collector = {FLAT: OrientedGeometry({"3": PinGeometry((0.0, -3.81), UP)}, (-3.0, -3.81, 3.0, 3.81))}
        symbols = [symbol("D1", led, (50.8, 50.8)), symbol("Q1", collector, (50.8, 64.77))]
        nets = [Connection("SINK", (("D1", "1"), ("Q1", "3")), "wire")]
        cost = _Model(symbols, nets, {}, Weights(), None).cost({s.reference: (s.position, s.orientation) for s in symbols})
        self.assertEqual((cost.through, cost.bends), (0, 0))

    def test_a_wire_that_leaves_a_pin_along_its_direction_is_never_through_its_own_part(self):
        symbols = [symbol("U1", chip(), (50.8, 50.8)), symbol("R1", {FLAT: resistor()[Orientation(90)]}, (80.0, 50.8 - 2.54))]
        nets = [Connection("A", (("U1", "1"), ("R1", "1")), "wire")]
        model = _Model(symbols, nets, {}, Weights(), None)
        self.assertEqual(model.cost({s.reference: (s.position, s.orientation) for s in symbols}).through, 0)


if __name__ == "__main__":
    unittest.main()
