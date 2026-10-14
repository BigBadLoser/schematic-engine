"""Reference and value text goes where a reader can read it: not on a wire, a symbol, a pin or another text."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.schematic.field_placement import file_angle, plan_fields, routing_text_obstacles
from backend.schematic.kicad_geometry import measured_scene_from_kicad, text_box, text_width_mm
from backend.schematic.presentation_geometry import Bounds, GeometryItem, GeometryKind as K, GeometryScene


def wire(name, a, b):
    return GeometryItem(
        name, K.WIRE, name,
        Bounds(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), segment=(a, b),
    )


def field(ref, kind, name, text, x, y, angle=0.0, size=1.27):
    box = text_box(x, y, text, size, angle)
    return GeometryItem(
        f"{ref}:field:{name}", kind, ref, box, anchor=(x, y), text=text, angle=angle, measurement="estimated",
    )


def vertical_part(ref="R1", x=0.0, y=0.0, rotation=0.0):
    """A resistor drawn vertically: a 2 x 5 mm body, a 1.27 mm pin at each end, a wire from each pin."""
    return [
        GeometryItem(f"{ref}:graphic", K.BODY, ref, Bounds(x - 1, y - 2.5, x + 1, y + 2.5), anchor=(x, y),
                     angle=rotation, text="Device:R"),
        GeometryItem(f"{ref}.1:stroke", K.PIN_STROKE, ref, Bounds(x, y - 3.77, x, y - 2.5)),
        GeometryItem(f"{ref}.2:stroke", K.PIN_STROKE, ref, Bounds(x, y + 2.5, x, y + 3.77)),
        GeometryItem(f"{ref}.1:terminal", K.PIN_ENDPOINT, ref, Bounds(x, y - 3.77, x, y - 3.77)),
        GeometryItem(f"{ref}.2:terminal", K.PIN_ENDPOINT, ref, Bounds(x, y + 3.77, x, y + 3.77)),
        wire(f"{ref}:up", (x, y - 15), (x, y - 3.77)),
        wire(f"{ref}:down", (x, y + 3.77), (x, y + 15)),
    ]


def overlaps(a: Bounds, b: Bounds) -> bool:
    return a.left < b.right and a.right > b.left and a.top < b.bottom and a.bottom > b.top


class FieldPlacementTests(unittest.TestCase):
    def default_fields(self, ref="R1", x=0.0, y=0.0):
        # where MCP Pro puts them: the reference above, the value below, both on the symbol's axis
        return [
            field(ref, K.REFERENCE, "Reference", ref, x, y - 3.81),
            field(ref, K.VALUE, "Value", "10k", x, y + 3.81),
        ]

    def test_the_same_scene_gives_the_same_moves_and_a_known_solver(self):
        scene = GeometryScene.from_items(tuple(
            vertical_part("R1") + self.default_fields("R1") + vertical_part("R2", 6.0) + self.default_fields("R2", 6.0)
        ))
        first, second = plan_fields(scene), plan_fields(scene)
        self.assertEqual(first.moves, second.moves)
        self.assertIn(first.solver, {"cp-sat", "cp-sat (deterministic limit)", "cp-sat (wall-clock limit)", "greedy", "none"})

    def test_text_on_the_wire_of_a_vertical_part_moves_beside_it(self):
        scene = GeometryScene.from_items(tuple(vertical_part() + self.default_fields()))
        plan = plan_fields(scene)
        self.assertEqual({move.field for move in plan.moves}, {"Reference", "Value"})
        wires = [item.segment for item in scene.items if item.kind == K.WIRE]
        for move in plan.moves:
            for (ax, ay), (bx, by) in wires:
                self.assertFalse(
                    max(ax, bx) > move.box.left and min(ax, bx) < move.box.right
                    and max(ay, by) > move.box.top and min(ay, by) < move.box.bottom,
                    (move.field, move.box),
                )
        self.assertGreater(plan.collisions_before.get("wire", 0), 0)
        self.assertEqual(plan.collisions_after, {"text": 0})

    def bare_vertical(self):
        return [i for i in vertical_part() if i.kind != K.WIRE]

    def test_vertical_two_pin_part_puts_both_texts_on_the_right(self):
        fields = [field("R1", K.REFERENCE, "Reference", "R1", 0, 0), field("R1", K.VALUE, "Value", "10k", 0, 0.5)]
        plan = plan_fields(GeometryScene.from_items(tuple(self.bare_vertical() + fields)))
        self.assertEqual(len(plan.moves), 2)
        for move in plan.moves:
            self.assertGreater(move.box.left, 1.0, move)

    def test_horizontal_two_pin_part_puts_reference_above_and_value_below(self):
        items = [
            GeometryItem("R1:graphic", K.BODY, "R1", Bounds(-2.5, -1, 2.5, 1), anchor=(0, 0), angle=90.0, text="Device:R"),
            GeometryItem("R1.1:terminal", K.PIN_ENDPOINT, "R1", Bounds(-3.77, 0, -3.77, 0)),
            GeometryItem("R1.2:terminal", K.PIN_ENDPOINT, "R1", Bounds(3.77, 0, 3.77, 0)),
            field("R1", K.REFERENCE, "Reference", "R1", 0, 0), field("R1", K.VALUE, "Value", "10k", 0, 0.5),
        ]
        plan = plan_fields(GeometryScene.from_items(tuple(items)))
        moves = {move.field: move for move in plan.moves}
        self.assertLess(moves["Reference"].box.bottom, -1.0)
        self.assertGreater(moves["Value"].box.top, 1.0)

    def test_wire_on_the_right_sends_both_texts_to_the_left_together(self):
        items = self.bare_vertical() + [wire("w1", (3, -20), (3, 20)), wire("w2", (6.5, -20), (6.5, 20)),
                                        field("R1", K.REFERENCE, "Reference", "R1", 0, 0),
                                        field("R1", K.VALUE, "Value", "10k", 0, 0.5)]
        plan = plan_fields(GeometryScene.from_items(tuple(items)))
        self.assertEqual(len(plan.moves), 2)
        for move in plan.moves:
            self.assertLess(move.box.right, -1.0, move)

    def test_moved_text_stays_close_to_its_symbol(self):
        scene = GeometryScene.from_items(tuple(vertical_part() + self.default_fields()))
        for move in plan_fields(scene).moves:
            self.assertLess(abs(move.x), 12.0)
            self.assertLess(abs(move.y), 8.0)

    def test_text_that_is_already_free_is_left_alone(self):
        scene = GeometryScene.from_items(tuple(vertical_part() + [
            field("R1", K.REFERENCE, "Reference", "R1", 5.0, -3.0),
            field("R1", K.VALUE, "Value", "10k", 5.0, 3.0),
        ]))
        self.assertEqual(plan_fields(scene).moves, ())

    def test_text_drawn_vertically_is_turned_horizontal_when_there_is_room(self):
        # a part turned a quarter turn writes its fields along the part: free of every wire, but vertical
        scene = GeometryScene.from_items(tuple(vertical_part(rotation=90.0) + [
            field("R1", K.REFERENCE, "Reference", "R1", 5.0, -3.0, angle=90.0),
            field("R1", K.VALUE, "Value", "10k", 5.0, 3.0, angle=90.0),
        ]))
        plan = plan_fields(scene)
        self.assertEqual({move.field for move in plan.moves}, {"Reference", "Value"})
        for move in plan.moves:
            self.assertGreater(move.box.right - move.box.left, move.box.bottom - move.box.top)

    def test_vertical_text_stays_when_every_horizontal_place_is_taken(self):
        # a text box can only be vertical here: a horizontal one anywhere near would lie on a wire or the symbol
        gap = [wire(f"w{i}", (x, -40.0), (x, 40.0)) for i, x in enumerate(range(-40, 41, 4))]  # 4 mm apart: the 1 mm text margin needs room
        scene = GeometryScene.from_items(tuple(
            vertical_part(rotation=90.0) + gap + [field("R1", K.VALUE, "Value", "10k", 6.0, 3.0, angle=90.0)]
        ))
        plan = plan_fields(scene)
        self.assertEqual([m for m in plan.moves if m.field == "Value" and m.box.right - m.box.left > m.box.bottom - m.box.top], [])

    def test_the_angle_written_makes_the_text_horizontal_whatever_the_symbols_rotation(self):
        self.assertEqual([file_angle(r) for r in (0, 90, 180, 270)], [0, 90, 0, 90])
        for rotation in (0, 90, 180, 270):
            absolute = (file_angle(rotation) + rotation) % 180
            self.assertEqual(absolute, 0)
        rotated = vertical_part(rotation=90.0)
        scene = GeometryScene.from_items(tuple(rotated + self.default_fields()))
        self.assertTrue(all(move.angle == 90 for move in plan_fields(scene).moves))

    def test_two_close_parts_do_not_put_their_text_on_each_other(self):
        scene = GeometryScene.from_items(tuple(
            vertical_part("R1", 0.0) + vertical_part("R2", 6.0) + self.default_fields("R1", 0.0) + self.default_fields("R2", 6.0)
        ))
        plan = plan_fields(scene)
        boxes = [move.box for move in plan.moves]
        for index, first in enumerate(boxes):
            for second in boxes[index + 1:]:
                self.assertFalse(overlaps(first, second))
        self.assertEqual(plan.collisions_after.get("text", 0), 0)

    def test_a_large_body_can_carry_its_own_text_when_the_outside_is_full(self):
        body = GeometryItem("U1:graphic", K.BODY, "U1", Bounds(0, 0, 20, 30), anchor=(10, 15), angle=0.0, text="MCU:Chip")
        fenced = [
            wire("n", (-30, -3), (50, -3)), wire("s", (-30, 33), (50, 33)),
            wire("e", (23, -30), (23, 60)), wire("w", (-3, -30), (-3, 60)),
        ]
        scene = GeometryScene.from_items(tuple([body, *fenced,
            field("U1", K.REFERENCE, "Reference", "U1", 10, -2.0),
            field("U1", K.VALUE, "Value", "STM32F030", 10, 32.0),
        ]))
        for move in plan_fields(scene).moves:
            self.assertTrue(0 < move.box.left and move.box.right < 20 and 0 < move.box.top and move.box.bottom < 30, move.box)

    def test_a_power_symbols_text_goes_on_the_side_away_from_its_pin(self):
        # a ground symbol: the pin (and its wire) at the top, the graphic below it
        parts = [
            GeometryItem("#PWR1:graphic", K.POWER_GRAPHIC, "#PWR1", Bounds(-1.27, 0, 1.27, 2.54), net="GND",
                         anchor=(0, 0), angle=0.0, text="power:GND"),
            GeometryItem("#PWR1.1:terminal", K.PIN_ENDPOINT, "#PWR1", Bounds(0, 0, 0, 0), net="GND"),
            wire("stub", (0, -10), (0, 0)),
            field("#PWR1", K.POWER_TEXT, "Value", "GND", 0, -1.0),         # on the wire
        ]
        moves = plan_fields(GeometryScene.from_items(tuple(parts))).moves
        self.assertEqual(len(moves), 1)
        self.assertGreater(moves[0].y, 1.0)          # below the graphic

    def _power_case(self, net, pin_y, body, value_y):
        parts = [
            GeometryItem("#PWR1:graphic", K.POWER_GRAPHIC, "#PWR1", body, net=net, anchor=(0, 0), angle=0.0, text="power:" + net),
            GeometryItem("#PWR1.1:terminal", K.PIN_ENDPOINT, "#PWR1", Bounds(0, pin_y, 0, pin_y), net=net),
            field("#PWR1", K.POWER_TEXT, "Value", net, 0, value_y),
        ]
        return plan_fields(GeometryScene.from_items(tuple(parts))).moves

    def test_a_hanging_supply_value_goes_below_even_when_it_stands_on_the_pin_side(self):
        moves = self._power_case("-5V", 0, Bounds(-0.76, 0, 0.76, 2.54), -1.5)   # pin on top, text above (kept)
        self.assertEqual(len(moves), 1)
        self.assertGreater(moves[0].y, 2.54)

    def test_a_standing_supply_value_goes_above(self):
        moves = self._power_case("+5V", 2.54, Bounds(-0.76, 0, 0.76, 2.54), 4.0)   # pin at the bottom, text below
        self.assertEqual(len(moves), 1)
        self.assertLess(moves[0].y, 0.0)


class TextBoxModelTests(unittest.TestCase):
    """A few of the 168 combinations measured against kicad-cli's rendering (the sandbox's field_justify probe)."""

    def test_justification_is_applied_in_the_texts_own_frame(self):
        w = text_width_mm("T0001", 1.27)
        # horizontal text, left-justified: the box starts at the anchor
        box = text_box(10, 10, "T0001", 1.27, 0, frozenset({"left"}))
        self.assertAlmostEqual(box.left, 10.0)
        self.assertAlmostEqual(box.right, 10.0 + w)
        # turned half a turn the same justification starts from the other side
        box = text_box(10, 10, "T0001", 1.27, 180, frozenset({"left"}))
        self.assertAlmostEqual(box.right, 10.0)
        self.assertAlmostEqual(box.left, 10.0 - w)

    def test_vertical_text_reads_bottom_to_top(self):
        w = text_width_mm("T0001", 1.27)
        box = text_box(10, 10, "T0001", 1.27, 90, frozenset({"left"}))
        self.assertAlmostEqual(box.bottom, 10.0)
        self.assertAlmostEqual(box.top, 10.0 - w)

    def test_a_mirror_flips_the_box_across_the_anchor(self):
        plain = text_box(10, 10, "T0001", 1.27, 0, frozenset({"top"}))
        mirrored = text_box(10, 10, "T0001", 1.27, 0, frozenset({"top"}), "x")
        self.assertAlmostEqual(plain.top, 10.0)
        self.assertAlmostEqual(mirrored.bottom, 10.0)
        left = text_box(10, 10, "T0001", 1.27, 0, frozenset({"left"}))
        flipped = text_box(10, 10, "T0001", 1.27, 0, frozenset({"left"}), "y")
        self.assertAlmostEqual(left.left, 10.0)
        self.assertAlmostEqual(flipped.right, 10.0)

    def test_a_centred_text_is_the_same_turned_half_a_turn(self):
        a, b = text_box(5, 5, "R12", 1.27, 0), text_box(5, 5, "R12", 1.27, 180)
        self.assertEqual((a.left, a.top, a.right, a.bottom), (b.left, b.top, b.right, b.bottom))


class RoutingTextObstacleTests(unittest.TestCase):
    def scene(self):
        return GeometryScene((
            *[field("R1", K.REFERENCE, "Reference", "R1", 0, -4), field("R1", K.VALUE, "Value", "10k", 0, 4)],
            field("#PWR01", K.POWER_TEXT, "Text", "GND", 10, 4),
            field("#PWR01", K.VALUE, "Value", "GND", 10, 8),
            GeometryItem("R1.1:name", K.PIN_NAME, "R1", Bounds(0, 0, 1, 1), text="A"),
            GeometryItem("R1.1:num", K.PIN_NUMBER, "R1", Bounds(0, 0, 1, 1), text="1"),
            GeometryItem("L1", K.NET_LABEL, "L1", Bounds(5, 5, 6, 6), text="VIN"),
        ))

    def kinds(self, scene):
        return sorted((i.kind.name, i.owner) for i in scene.items)

    def test_tight_drops_reference_and_value_of_ordinary_symbols_only(self):
        kinds = self.kinds(routing_text_obstacles(self.scene(), True))
        self.assertNotIn(("REFERENCE", "R1"), kinds)
        self.assertNotIn(("VALUE", "R1"), kinds)
        for kept in (("VALUE", "#PWR01"), ("POWER_TEXT", "#PWR01"), ("PIN_NAME", "R1"), ("PIN_NUMBER", "R1"), ("NET_LABEL", "L1")):
            self.assertIn(kept, kinds)

    def test_legacy_keeps_everything(self):
        scene = self.scene()
        self.assertIs(routing_text_obstacles(scene, False), scene)


class WireFreeTests(unittest.TestCase):
    def test_no_text_on_a_wire_when_the_outer_ring_is_clear(self):
        # wires across every near side and on the axis; only the outer east and west places are clear
        items = [
            *vertical_part("R1"),
            wire("v1", (-2.0, -12), (-2.0, 12)),
            wire("v2", (2.0, -12), (2.0, 12)),
            *[field("R1", K.REFERENCE, "Reference", "R1", 0, -3.81), field("R1", K.VALUE, "Value", "10k", 0, 3.81)],
        ]
        plan = plan_fields(GeometryScene(tuple(items)))
        wires = [i.segment for i in items if i.kind == K.WIRE]
        self.assertGreater(len(plan.moves), 0)
        for move in plan.moves:
            box = move.box
            for (ax, ay), (bx, by) in wires:
                hit = (min(ax, bx) < box.right and max(ax, bx) > box.left and min(ay, by) < box.bottom and max(ay, by) > box.top)
                self.assertFalse(hit, (move, (ax, ay, bx, by)))


class SheetOrderTests(unittest.TestCase):
    """KiCad writes symbol blocks in random order; the plan must not depend on it."""

    @staticmethod
    def symbol(reference: str, x: float, y: float) -> str:
        return (
            f'  (symbol (lib_id "Device:R") (at {x} {y} 0) (unit 1)\n'
            f'    (property "Reference" "{reference}" (at {x} {y - 4} 0) (effects (font (size 1.27 1.27))))\n'
            f'    (property "Value" "10k" (at {x} {y + 4} 0) (effects (font (size 1.27 1.27)))))\n'
        )

    def moves_for(self, blocks: list[str]) -> list:
        head = (
            '(kicad_sch (version 20260306) (lib_symbols (symbol "Device:R" (symbol "R_1_1"'
            ' (rectangle (start -1 -2) (end 1 2))'
            ' (pin passive line (at 0 -4 90) (length 2) (name "~") (number "1"))'
            ' (pin passive line (at 0 4 270) (length 2) (name "~") (number "2")))))\n'
        )
        wire = "  (wire (pts (xy 30 36) (xy 30 20)))\n  (wire (pts (xy 30 44) (xy 30 60)))\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.kicad_sch"
            path.write_text(head + "".join(blocks) + wire + ")", encoding="utf-8")
            scene, _ = measured_scene_from_kicad(path)
        return [(m.owner, m.field, m.x, m.y, m.angle) for m in plan_fields(scene).moves]

    def test_swapping_two_symbol_blocks_gives_the_same_moves(self):
        a, b, c = self.symbol("R1", 30, 40), self.symbol("R2", 34, 40), self.symbol("R3", 60, 40)
        reference = self.moves_for([a, b, c])
        self.assertTrue(reference)
        self.assertEqual(reference, self.moves_for([c, a, b]))
        self.assertEqual(reference, self.moves_for([b, c, a]))


if __name__ == "__main__":
    unittest.main()
