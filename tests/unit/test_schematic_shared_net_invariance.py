"""The topology planner cuts a net into the same branches wherever the same circuit stands on the sheet."""
from __future__ import annotations

import random
from unittest import TestCase

from backend.schematic import NetIntent, PinEndpoint
from backend.schematic.presentation_geometry import Bounds, GeometryItem, GeometryKind, GeometryScene
from backend.schematic.shared_net_routing import plan_shared_net

GRID = 1.27
OFFSETS = ((0.0, 0.0), (31.75, 25.4), (209.55, 144.78), (177.8, 68.58), (63.5, 99.06), (15.24, 3.81))


def _scenario(rng: random.Random):
    pins = []
    taken = set()
    for index in range(rng.randint(3, 5)):
        while True:
            # few distinct coordinates, so that pins line up and distances tie
            point = (rng.randint(0, 6) * 2 * GRID, rng.randint(0, 5) * 2 * GRID)
            if point not in taken:
                taken.add(point)
                break
        pins.append((f"R{index + 1}", "1", point))
    bodies = []
    for _ in range(rng.randint(0, 3)):
        left, top = rng.randint(-2, 12) * GRID, rng.randint(-2, 10) * GRID
        bodies.append(Bounds(left, top, left + rng.randint(2, 5) * GRID, top + rng.randint(2, 5) * GRID))
    return pins, bodies


def _plan(pins, bodies, dx, dy):
    items = [
        GeometryItem(f"{ref}.{pin}:terminal", GeometryKind.PIN_ENDPOINT, ref, Bounds(x + dx, y + dy, x + dx, y + dy))
        for ref, pin, (x, y) in pins
    ]
    items += [
        GeometryItem(f"X{index}:body", GeometryKind.BODY, f"X{index}", Bounds(b.left + dx, b.top + dy, b.right + dx, b.bottom + dy))
        for index, b in enumerate(bodies)
    ]
    net = NetIntent("N", tuple(PinEndpoint(ref, pin) for ref, pin, _point in pins))
    try:
        plan = plan_shared_net(net, GeometryScene(items=tuple(items)))
    except ValueError:
        return None
    junction = None if plan.junction is None else (round(plan.junction[0] - dx, 4), round(plan.junction[1] - dy, 4))

    def end(edge, suffix):
        if edge.get(f"ref{suffix}"):
            return edge[f"ref{suffix}"]
        x, y = edge[f"point{suffix}"]
        return (round(x - dx, 4), round(y - dy, 4))

    return plan.strategy, junction, tuple((edge["id"], end(edge, "1"), end(edge, "2")) for edge in plan.edges)


class SharedNetInvarianceTests(TestCase):
    """`auto-tr2-b`: the opamp, moved 178 mm on its page, had one net cut into other branches (two pairs of pins of one
    distance, told apart by float noise instead of by name)."""

    def test_a_net_is_cut_into_the_same_branches_wherever_the_circuit_stands(self):
        rng = random.Random(5)
        moved = []
        for _ in range(250):
            pins, bodies = _scenario(rng)
            outcomes = {offset: _plan(pins, bodies, *offset) for offset in OFFSETS}
            if len({repr(value) for value in outcomes.values()}) > 1:
                moved.append(outcomes)
        self.assertEqual(moved, [])
