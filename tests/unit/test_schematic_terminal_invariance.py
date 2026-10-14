"""The planner of power and label terminals picks the same exit wherever the same geometry stands on the sheet."""
from __future__ import annotations

import random
from unittest import TestCase

from backend.schematic.presentation_geometry import Bounds
from backend.schematic.terminal_placement import (
    LabelEnd, choose_rail_symbol, choose_safe_terminal, power_symbol_footprint,
)

GRID = 1.27
# Offsets that are multiples of the grid, small and large, as a layout stands on a page wherever the auto-layout left it.
OFFSETS = ((0.0, 0.0), (31.75, 25.4), (209.55, 144.78), (177.8, 68.58), (63.5, 99.06), (15.24, 3.81))


def _shifted(box: Bounds, dx: float, dy: float) -> Bounds:
    return Bounds(box.left + dx, box.top + dy, box.right + dx, box.bottom + dy)


def _scenario(rng: random.Random):
    """A pin facing left among a few boxes and pins, all on the grid, and the symbol that ends its stub."""
    boxes = []
    for _ in range(rng.randint(1, 4)):
        left, top = rng.randint(-14, 4) * GRID, rng.randint(-8, 8) * GRID
        box = Bounds(left, top, left + rng.randint(2, 8) * GRID, top + rng.randint(2, 8) * GRID)
        if not (box.left - 3 <= 0.0 <= box.right + 3 and box.top - 3 <= 0.0 <= box.bottom + 3):
            boxes.append(box)
    pins = tuple((rng.randint(-8, 3) * GRID, rng.randint(-5, 5) * GRID) for _ in range(rng.randint(0, 3)))
    own = Bounds(0.0, -6 * GRID, 6 * GRID, 6 * GRID)
    return boxes, pins, own


def _choose(boxes, pins, own, end, dx, dy):
    try:
        plan = choose_safe_terminal(
            source=(dx, dy), preferred_normal=(-1.0, 0.0), own_bounds=_shifted(own, dx, dy),
            foreign_bounds=tuple(_shifted(box, dx, dy) for box in boxes),
            foreign_pin_points=tuple((px + dx, py + dy) for px, py in pins),
            grid_mm=GRID, clearance_mm=3.81, end_symbol=end,
        )
    except ValueError:
        return None
    return plan.strategy, round(plan.position[0] - dx, 4), round(plan.position[1] - dy, 4)


class TerminalInvarianceTests(TestCase):
    """`auto-demo8`: the opamp, moved 178 mm on its page and otherwise the same to 0.01 mm, got three power terminals in
    other places (a tie of two paths of one length was decided by float noise, as was a symbol standing exactly one
    clearance from a stub), and a third more wire."""

    def test_a_power_terminal_does_not_depend_on_where_the_geometry_stands(self):
        rng = random.Random(7)
        end = power_symbol_footprint("+5V", -1.0)
        moved = []
        for _ in range(300):
            boxes, pins, own = _scenario(rng)
            outcomes = {offset: _choose(boxes, pins, own, end, *offset) for offset in OFFSETS}
            if len(set(outcomes.values())) > 1:
                moved.append(outcomes)
        self.assertEqual(moved, [])

    def test_a_label_terminal_does_not_depend_on_where_the_geometry_stands(self):
        rng = random.Random(11)
        end = LabelEnd(text_width_mm=6.6, size_mm=1.524)
        moved = []
        for _ in range(300):
            boxes, pins, own = _scenario(rng)
            outcomes = {offset: _choose(boxes, pins, own, end, *offset) for offset in OFFSETS}
            if len(set(outcomes.values())) > 1:
                moved.append(outcomes)
        self.assertEqual(moved, [])

    def test_the_symbol_on_a_rail_does_not_depend_on_where_the_rail_stands(self):
        rng = random.Random(13)
        end = power_symbol_footprint("GND", -1.0)
        moved = []
        for _ in range(200):
            boxes, pins, _own = _scenario(rng)
            low, high = rng.randint(-6, 0) * GRID, rng.randint(1, 8) * GRID
            outcomes = {}
            for dx, dy in OFFSETS:
                plan = choose_rail_symbol(
                    horizontal=True, rail=dy + 2 * GRID, low=low + dx, high=high + dx, end_symbol=end,
                    foreign_bounds=tuple(_shifted(box, dx, dy) for box in boxes),
                    foreign_pin_points=tuple((px + dx, py + dy) for px, py in pins), grid_mm=GRID,
                )
                outcomes[(dx, dy)] = None if plan is None else (round(plan.position[0] - dx, 4), round(plan.position[1] - dy, 4))
            if len(set(outcomes.values())) > 1:
                moved.append(outcomes)
        self.assertEqual(moved, [])
