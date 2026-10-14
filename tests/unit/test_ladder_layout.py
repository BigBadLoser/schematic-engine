"""Rules for the pure ladder placer, using only hand-made role and geometry fixtures."""
from __future__ import annotations

import random
import unittest
from dataclasses import dataclass, replace

from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import (
    Connection, OrientedGeometry, PinGeometry, PlacedSymbol, estimate_net, rotate_geometry,
)
from backend.schematic.ladder_layout import ladder_layout

FLAT = Orientation(0)
LEFT, RIGHT, UP, DOWN = (-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)


@dataclass(frozen=True)
class Attachment:
    host: str
    net: str
    rail: str
    side: str


@dataclass(frozen=True)
class Rung:
    net_a: str
    net_b: str


@dataclass(frozen=True)
class Feedback:
    net_from: str
    net_to: str


@dataclass(frozen=True)
class Roles:
    rails: dict
    trunks: tuple[str, ...]
    chains: dict[str, tuple[str, ...]]
    attachments: dict[str, Attachment]
    rungs: dict[str, Rung]
    feedback: dict[str, Feedback]
    pairs: tuple[tuple[str, str], ...]
    other: tuple[str, ...]


def part(ref, pins, box, *, body=None, orientations=None):
    geometry = {FLAT: OrientedGeometry(pins, box, body)}
    if orientations:
        geometry.update(orientations)
    return PlacedSymbol(ref, (64.0, 50.8), FLAT, geometry)


def horizontal(ref, pin_names=("1", "2"), width=3.81):
    pins = {pin_names[0]: PinGeometry((-width, 0.0), LEFT), pin_names[1]: PinGeometry((width, 0.0), RIGHT)}
    return part(ref, pins, (-width, -2.54, width, 2.54), body=(-2.54, -1.27, 2.54, 1.27))


def vertical(ref, pin_names=("1", "2"), half=5.08, *, reverse=False):
    if reverse:
        pins = {pin_names[0]: PinGeometry((0.0, half), DOWN), pin_names[1]: PinGeometry((0.0, -half), UP)}
    else:
        pins = {pin_names[0]: PinGeometry((0.0, -half), UP), pin_names[1]: PinGeometry((0.0, half), DOWN)}
    geom = OrientedGeometry(pins, (-2.54, -half, 2.54, half), (-2.0, -2.0, 2.0, 2.0))
    return PlacedSymbol(ref, (64.0, 50.8), FLAT, {FLAT: geom})


def toy():
    symbols = [
        part("J1", {
            "SIG": PinGeometry((5.08, 0.0), RIGHT),
            "GND": PinGeometry((0.0, 5.08), DOWN),
            "VCC": PinGeometry((0.0, -5.08), UP),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81)),
        part("U1", {
            "VI": PinGeometry((-5.08, 0.0), LEFT),
            "VO": PinGeometry((5.08, 0.0), RIGHT),
            "ADJ": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81)),
        horizontal("RLOAD"),
        vertical("R4"), vertical("D3"), vertical("C3", reverse=True),
        vertical("R1", reverse=True), vertical("C2", reverse=True),
        vertical("RTERM", reverse=True), vertical("CLOAD", reverse=True),
    ]
    nets = [
        Connection("in", (("J1", "SIG"), ("U1", "VI"))),
        Connection("out", (("U1", "VO"), ("RLOAD", "1"), ("C3", "2"), ("R4", "1"), ("D3", "1"))),
        Connection("adj", (("U1", "ADJ"), ("R4", "2"), ("D3", "2"), ("R1", "2"), ("C2", "2"))),
        Connection("load", (("RLOAD", "2"), ("RTERM", "2"), ("CLOAD", "2"))),
        Connection("GND", (("J1", "GND"), ("C3", "1"), ("R1", "1"), ("C2", "1"), ("RTERM", "1"), ("CLOAD", "1")), style="power"),
        Connection("VCC", (("J1", "VCC"),), style="power"),
    ]
    roles = Roles(
        rails={"GND": "ground", "VCC": "supply"}, trunks=("out", "adj", "load"),
        chains={"main": ("J1", "U1", "RLOAD")},
        attachments={
            "C3": Attachment("U1", "out", "GND", "ground"),
            "R1": Attachment("U1", "adj", "GND", "ground"),
            "C2": Attachment("U1", "adj", "GND", "ground"),
            "RTERM": Attachment("RLOAD", "load", "GND", "ground"),
            "CLOAD": Attachment("RLOAD", "load", "GND", "ground"),
        },
        rungs={"R4": Rung("out", "adj"), "D3": Rung("out", "adj")},
        feedback={}, pairs=(), other=(),
    )
    return symbols, nets, roles


class LadderLayoutTests(unittest.TestCase):
    def test_toy_ladder_has_chain_row_hanging_attachments_and_vertical_rungs(self):
        symbols, nets, roles = toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        # The three chain input/output tips on U1 and both series parts share y=0.
        u1 = result["U1"]
        u1_geom = by_ref["U1"].geometry[u1.orientation]
        self.assertAlmostEqual(u1.position[1] + u1_geom.pins["VI"].point[1], 0.0)
        self.assertAlmostEqual(u1.position[1] + u1_geom.pins["VO"].point[1], 0.0)
        self.assertAlmostEqual(result["J1"].position[1] + by_ref["J1"].geometry[result["J1"].orientation].pins["SIG"].point[1], 0.0)
        load_row = result["RTERM"].position[1] + by_ref["RTERM"].geometry[result["RTERM"].orientation].pins["2"].point[1]
        self.assertAlmostEqual(result["CLOAD"].position[1] + by_ref["CLOAD"].geometry[result["CLOAD"].orientation].pins["2"].point[1], load_row)
        for ref in ("J1", "U1", "RLOAD"):
            self.assertIn(ref, result)
        row_adj = result["R4"].position[1] + by_ref["R4"].geometry[result["R4"].orientation].pins["2"].point[1]
        for ref in ("R4", "D3"):
            geom, placement = by_ref[ref].geometry[result[ref].orientation], result[ref]
            self.assertAlmostEqual(placement.position[1] + geom.pins["1"].point[1], 0.0)
            self.assertAlmostEqual(placement.position[1] + geom.pins["2"].point[1], row_adj)
            self.assertGreaterEqual(placement.position[0] + geom.box[0],
                                    result["U1"].position[0] + by_ref["U1"].geometry[result["U1"].orientation].box[2])
            self.assertLessEqual(placement.position[0] + geom.box[2],
                                 result["RLOAD"].position[0] + by_ref["RLOAD"].geometry[result["RLOAD"].orientation].box[0])
        c3_geom, c3_placement = by_ref["C3"].geometry[result["C3"].orientation], result["C3"]
        c3_left = c3_placement.position[0] + c3_geom.box[0]
        c3_right = c3_placement.position[0] + c3_geom.box[2]
        self.assertGreater(c3_left, result["D3"].position[0] + by_ref["D3"].geometry[result["D3"].orientation].box[2])
        self.assertLess(c3_right, result["RLOAD"].position[0] + by_ref["RLOAD"].geometry[result["RLOAD"].orientation].box[0])
        for ref, signal, rail, row in (
            ("C3", "2", "1", 0.0), ("R1", "2", "1", row_adj), ("C2", "2", "1", row_adj),
            ("RTERM", "2", "1", load_row), ("CLOAD", "2", "1", load_row),
        ):
            geom, placement = by_ref[ref].geometry[result[ref].orientation], result[ref]
            self.assertAlmostEqual(placement.position[1] + geom.pins[signal].point[1], row)
            if roles.attachments[ref].side == "ground":
                self.assertGreaterEqual(placement.position[1] + geom.body[1], row)
            else:
                self.assertLessEqual(placement.position[1] + geom.body[3], row)
            rail_y = placement.position[1] + geom.pins[rail].point[1]
            self.assertGreater(rail_y, row) if roles.attachments[ref].side == "ground" else self.assertLess(rail_y, row)
        connected = [endpoint for net in nets for endpoint in net.pins]
        expected = [(symbol.reference, pin) for symbol in symbols for pin in symbol.geometry[FLAT].pins]
        self.assertEqual(sorted(connected), sorted(expected))
        self.assertIn(("RLOAD", "2"), next(net for net in nets if net.net == "load").pins)

        def tip(ref, pin):
            geom = by_ref[ref].geometry[result[ref].orientation]
            point = geom.pins[pin].point
            return (result[ref].position[0] + point[0], result[ref].position[1] + point[1])

        ground_row = 35.56
        supply = tip("J1", "VCC")
        wires = [(tip("J1", "SIG"), tip("U1", "VI")),
                 (tip("U1", "VO"), tip("RLOAD", "1")),
                 (tip("U1", "ADJ"), (tip("U1", "ADJ")[0], row_adj)),
                 ((tip("U1", "ADJ")[0], row_adj), (tip("D3", "2")[0], row_adj)),
                 (tip("RLOAD", "2"), (tip("RLOAD", "2")[0], load_row)),
                 ((tip("RLOAD", "2")[0], load_row), (tip("RTERM", "2")[0], load_row)),
                 (supply, (supply[0], -7.62))]
        wires.extend((tip(ref, pin), (tip(ref, pin)[0], ground_row)) for ref, pin in
                     (("J1", "GND"), ("C3", "1"), ("R1", "1"), ("C2", "1"), ("RTERM", "1"), ("CLOAD", "1")))
        bodies = []
        for ref, placement in result.items():
            geom = by_ref[ref].geometry[placement.orientation]
            left, top, right, bottom = geom.body
            x, y = placement.position
            bodies.append((ref, (x + left, y + top, x + right, y + bottom)))
        for start, end in wires:
            for ref, (left, top, right, bottom) in bodies:
                if abs(start[1] - end[1]) <= 1e-7:
                    y = start[1]
                    hit = top + 1e-7 < y < bottom - 1e-7 and max(min(start[0], end[0]), left) < min(max(start[0], end[0]), right) - 1e-7
                else:
                    x = start[0]
                    hit = left + 1e-7 < x < right - 1e-7 and max(min(start[1], end[1]), top) < min(max(start[1], end[1]), bottom) - 1e-7
                self.assertFalse(hit, f"wire segment {start}→{end} crosses {ref} body")

        def intersects(a, b):
            (ax, ay), (bx, by) = a
            (cx, cy), (dx, dy) = b
            a_vertical, b_vertical = abs(ax - bx) <= 1e-7, abs(cx - dx) <= 1e-7
            if a_vertical == b_vertical:
                if a_vertical:
                    return abs(ax - cx) <= 1e-7 and max(min(ay, by), min(cy, dy)) <= min(max(ay, by), max(cy, dy)) + 1e-7
                return abs(ay - cy) <= 1e-7 and max(min(ax, bx), min(cx, dx)) <= min(max(ax, bx), max(cx, dx)) + 1e-7
            vertical, horizontal = (a, b) if a_vertical else (b, a)
            x = vertical[0][0]
            y = horizontal[0][1]
            return (min(vertical[0][1], vertical[1][1]) - 1e-7 <= y <= max(vertical[0][1], vertical[1][1]) + 1e-7
                    and min(horizontal[0][0], horizontal[1][0]) - 1e-7 <= x <= max(horizontal[0][0], horizontal[1][0]) + 1e-7)

        c3_x, c3_y = c3_placement.position
        c3_pin1 = c3_geom.pins["1"]
        c3_tip = (c3_x + c3_pin1.point[0], c3_y + c3_pin1.point[1])
        c3_body_bottom = c3_y + c3_geom.body[3]
        c3_ground_lead = ((c3_tip[0], c3_body_bottom), c3_tip)
        adj_bus = (tip("C2", "2"), tip("D3", "2"))
        self.assertFalse(intersects(c3_ground_lead, adj_bus), "ADJ bus crosses C3's foreign GND pin lead")

    def test_toy_c3_ground_lead_clears_adj_wire_with_default_and_text_widths(self):
        symbols, nets, roles = toy()
        for text_widths in ({}, {"J1": 10.16, "U1": 14.0, "RLOAD": 16.0}):
            result = ladder_layout(symbols, nets, roles, text_widths=text_widths)
            by_ref = {symbol.reference: symbol for symbol in symbols}

            def tip(ref, pin):
                geom = by_ref[ref].geometry[result[ref].orientation]
                local = geom.pins[pin].point
                return (result[ref].position[0] + local[0], result[ref].position[1] + local[1])

            c3 = by_ref["C3"].geometry[result["C3"].orientation]
            placement = result["C3"].position
            lead_start = (placement[0] + c3.pins["1"].point[0], placement[1] + c3.body[3])
            lead_end = tip("C3", "1")
            start, end = tip("C2", "2"), tip("D3", "2")
            self.assertEqual(start[1], end[1])
            # The ADJ net's row segment must not meet the GND pin's body-to-tip lead.
            c3_x = lead_end[0]
            self.assertFalse(min(start[0], end[0]) - 1e-7 <= c3_x <= max(start[0], end[0]) + 1e-7
                             and min(lead_start[1], lead_end[1]) - 1e-7 <= start[1] <= max(lead_start[1], lead_end[1]) + 1e-7,
                             f"ADJ row crosses C3 GND lead at {text_widths}")
            c3_box = c3.box
            c3_left, c3_right = placement[0] + c3_box[0], placement[0] + c3_box[2]
            d3_right = result["D3"].position[0] + by_ref["D3"].geometry[result["D3"].orientation].box[2]
            load_left = result["RLOAD"].position[0] + by_ref["RLOAD"].geometry[result["RLOAD"].orientation].box[0]
            self.assertGreater(c3_left, d3_right)
            self.assertLess(c3_right, load_left)

    def test_offset_rung_pins_keep_the_checked_slot_after_skipping_an_obstacle(self):
        centered = OrientedGeometry({
            "1": PinGeometry((0.0, -5.08), UP),
            "2": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.0, -5.08, 5.0, 5.08))
        offset = OrientedGeometry({
            "1": PinGeometry((3.81, -5.08), UP),
            "2": PinGeometry((3.81, 5.08), DOWN),
        }, (-5.0, -5.08, 5.0, 5.08))
        r4 = PlacedSymbol("R4", (0.0, 0.0), FLAT, {FLAT: centered})
        d3 = PlacedSymbol("Z3", (0.0, 0.0), FLAT, {FLAT: offset})
        nets = [
            Connection("upper", (("R4", "1"), ("Z3", "1"))),
            Connection("lower", (("R4", "2"), ("Z3", "2"))),
        ]
        roles = Roles({}, ("upper", "lower"), {}, {},
                      {"R4": Rung("upper", "lower"), "Z3": Rung("upper", "lower")}, {}, (), ())
        result = ladder_layout([r4, d3], nets, roles, text_widths={})
        self.assertNotEqual(result["Z3"].position[0], result["R4"].position[0])
        for ref in ("R4", "Z3"):
            geom, placement = {"R4": centered, "Z3": offset}[ref], result[ref]
            self.assertAlmostEqual(placement.position[1] + geom.pins["1"].point[1], 7.62)
            self.assertAlmostEqual(placement.position[1] + geom.pins["2"].point[1], 17.78)
        boxes = []
        for ref, geom in (("R4", centered), ("Z3", offset)):
            x, y = result[ref].position
            l, t, right, bottom = geom.box
            boxes.append((x + l, y + t, x + right, y + bottom))
        self.assertFalse(boxes[0][0] < boxes[1][2] and boxes[1][0] < boxes[0][2])

    def test_input_order_does_not_change_placement(self):
        symbols, nets, roles = toy()
        expected = ladder_layout(symbols, nets, roles, text_widths={"J1": 9.0})
        random.Random(13).shuffle(symbols)
        random.Random(71).shuffle(nets)
        actual = ladder_layout(symbols, nets, roles, text_widths={"J1": 9.0})
        self.assertEqual(actual, expected)

    def test_chain_text_width_does_not_widen_the_gap_between_chain_parts(self):
        symbols, nets, roles = toy()
        narrow = ladder_layout(symbols, nets, roles, text_widths={})
        wide = ladder_layout(symbols, nets, roles, text_widths={"U1": 30.0})
        self.assertEqual(wide, narrow)

    def _two_parts(self, hung_below=0, hung_above=0):
        symbols = [horizontal("A"), horizontal("B")]
        nets = [Connection("mid", (("A", "2"), ("B", "1")))]
        attachments = {}
        for i in range(hung_below):
            ref = f"G{i}"
            symbols.append(vertical(ref, reverse=True))
            nets[0] = Connection("mid", (*nets[0].pins, (ref, "2")))
            nets.append(Connection("GND", ((ref, "1"),), style="power"))
            attachments[ref] = Attachment("A", "mid", "GND", "ground")
        for i in range(hung_above):
            ref = f"S{i}"
            symbols.append(vertical(ref))
            nets[0] = Connection("mid", (*nets[0].pins, (ref, "2")))
            nets.append(Connection("VCC", ((ref, "1"),), style="power"))
            attachments[ref] = Attachment("A", "mid", "VCC", "supply")
        roles = Roles({"GND": "ground", "VCC": "supply"}, (), {"main": ("A", "B")}, attachments, {}, {}, (), ())
        return symbols, nets, roles

    def test_chain_parts_are_a_gap_apart_with_nothing_hung_and_a_slot_pitch_per_hung_part_otherwise(self):
        by_ref = lambda symbols: {s.reference: s for s in symbols}  # noqa: E731
        spacing = {}
        for below, above in ((0, 0), (1, 0), (2, 0), (1, 2)):
            symbols, nets, roles = self._two_parts(below, above)
            result = ladder_layout(symbols, nets, roles, text_widths={"A": 40.0, "B": 40.0})
            a, b = extent(by_ref(symbols)["A"], result), extent(by_ref(symbols)["B"], result)
            spacing[(below, above)] = b[0] - a[2]
            shuffled = list(symbols)
            random.Random(5).shuffle(shuffled)
            self.assertEqual(ladder_layout(shuffled, list(reversed(nets)), roles, text_widths={"A": 40.0, "B": 40.0}), result)
        self.assertAlmostEqual(spacing[(0, 0)], 2.54)
        self.assertAlmostEqual(spacing[(1, 0)], 2.54 + 7.62 + 2.54)
        self.assertAlmostEqual(spacing[(2, 0)], 2.54 + 2 * 7.62 + 2.54)
        self.assertAlmostEqual(spacing[(1, 2)], 2.54 + 2 * 7.62 + 2.54)       # whichever side has more

    def test_a_rail_pin_beside_a_chain_pin_takes_a_slot_between_the_part_and_its_neighbour(self):
        spacing = {}
        for with_ground_pin in (False, True):
            pins = {"IN": PinGeometry((-5.08, 0.0), LEFT), "OUT": PinGeometry((5.08, 0.0), RIGHT)}
            if with_ground_pin:
                pins["G"] = PinGeometry((-5.08, -5.08), LEFT)
            chip = part("U", pins, (-5.08, -7.62, 5.08, 5.08), body=(-3.81, -7.62, 3.81, 5.08))
            symbols = [horizontal("R"), chip, horizontal("L")]
            nets = [Connection("in", (("R", "2"), ("U", "IN"))), Connection("out", (("U", "OUT"), ("L", "1")))]
            if with_ground_pin:
                nets.append(Connection("GND", (("U", "G"),), style="power"))
            roles = Roles({"GND": "ground"}, (), {"main": ("R", "U", "L")}, {}, {}, {}, (), ())
            result = ladder_layout(symbols, nets, roles, text_widths={})
            spacing[with_ground_pin] = extent(chip, result)[0] - extent(symbols[0], result)[2]
        self.assertAlmostEqual(spacing[True] - spacing[False], 7.62)

    def test_slot_occupied_by_host_is_skipped(self):
        symbols, nets, roles = toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        u1 = result["U1"].position
        ugeom = next(iter(next(s for s in symbols if s.reference == "U1").geometry.values()))
        host_tip_x = u1[0] + ugeom.pins["VO"].point[0]
        r1 = next(s for s in symbols if s.reference == "R1")
        signal_x = result["R1"].position[0] + r1.geometry[result["R1"].orientation].pins["2"].point[0]
        self.assertNotAlmostEqual(signal_x, host_tip_x)

    def test_attachment_without_legal_rail_facing_orientation_fails(self):
        symbols, nets, roles = toy()
        rterm = next(s for s in symbols if s.reference == "RTERM")
        invalid = vertical("RTERM")  # pin 1 points up, while this is a ground attachment
        symbols[symbols.index(rterm)] = invalid
        with self.assertRaisesRegex(ValueError, "No available orientation for RTERM"):
            ladder_layout(symbols, nets, roles, text_widths={})

    def test_supply_attachment_rail_pin_points_up(self):
        host = part("U1", {
            "VI": PinGeometry((-5.08, 0.0), LEFT),
            "VO": PinGeometry((5.08, 0.0), RIGHT),
            "ADJ": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08))
        resistor = vertical("R1")
        nets = [Connection("adj", (("U1", "ADJ"), ("R1", "2"))),
                Connection("VCC", (("R1", "1"),), style="power")]
        roles = Roles({"VCC": "supply"}, ("adj",), {"main": ("U1",)},
                      {"R1": Attachment("U1", "adj", "VCC", "supply")}, {}, {}, (), ())
        result = ladder_layout([host, resistor], nets, roles, text_widths={})
        geom = resistor.geometry[result["R1"].orientation]
        rail_y = result["R1"].position[1] + geom.pins["1"].point[1]
        signal_y = result["R1"].position[1] + geom.pins["2"].point[1]
        self.assertLess(rail_y, signal_y)
        self.assertEqual(geom.pins["1"].normal, UP)

    def _place_left_input_attachment(self, side):
        host = part("U1", {
            "VI": PinGeometry((-5.08, 0.0), LEFT),
            "VO": PinGeometry((5.08, 0.0), RIGHT),
            "ADJ": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81))
        resistor = vertical("R1", reverse=(side == "ground"))
        rail_name = "GND" if side == "ground" else "VCC"
        nets = [Connection("signal", (("U1", "VI"), ("R1", "2"))),
                Connection(rail_name, (("R1", "1"),), style="power")]
        roles = Roles({rail_name: side}, (), {"main": ("U1",)},
                      {"R1": Attachment("U1", "signal", rail_name, side)}, {}, {}, (), ())
        placements = ladder_layout([host, resistor], nets, roles, text_widths={})
        host_geom = host.geometry[placements["U1"].orientation]
        attachment_geom = resistor.geometry[placements["R1"].orientation]
        hx, hy = placements["U1"].position
        host_box = (hx + host_geom.box[0], hy + host_geom.box[1],
                    hx + host_geom.box[2], hy + host_geom.box[3])
        rx, ry = placements["R1"].position
        attach_box = (rx + attachment_geom.box[0], ry + attachment_geom.box[1],
                      rx + attachment_geom.box[2], ry + attachment_geom.box[3])
        self.assertLess(attach_box[2], host_box[0])
        host_tip = (hx + host_geom.pins["VI"].point[0], hy + host_geom.pins["VI"].point[1])
        signal = (rx + attachment_geom.pins["2"].point[0], ry + attachment_geom.pins["2"].point[1])
        rail = (rx + attachment_geom.pins["1"].point[0], ry + attachment_geom.pins["1"].point[1])
        body = attachment_geom.body or attachment_geom.box
        body_top, body_bottom = ry + body[1], ry + body[3]
        if side == "ground":
            self.assertGreater(rail[1], signal[1])
            self.assertEqual(attachment_geom.pins["1"].normal, DOWN)
            self.assertGreaterEqual(body_top, signal[1])
            rail_stub = (rail, (rail[0], rail[1] + 5.08))
        else:
            self.assertLess(rail[1], signal[1])
            self.assertEqual(attachment_geom.pins["1"].normal, UP)
            self.assertLessEqual(body_bottom, signal[1])
            rail_stub = (rail, (rail[0], rail[1] - 5.08))
        self.assertEqual(host_tip[1], signal[1])
        segments = ((host_tip, signal), rail_stub)
        for start, end in segments:
            for ref, (left, top, right, bottom) in (("U1", host_box), ("R1", attach_box)):
                if abs(start[1] - end[1]) <= 1e-7:
                    hit = (top + 1e-7 < start[1] < bottom - 1e-7
                           and max(min(start[0], end[0]), left) < min(max(start[0], end[0]), right) - 1e-7)
                else:
                    hit = (left + 1e-7 < start[0] < right - 1e-7
                           and max(min(start[1], end[1]), top) < min(max(start[1], end[1]), bottom) - 1e-7)
                self.assertFalse(hit, f"{side} route segment {start}→{end} crosses {ref} body")

    def test_ground_attachment_on_left_facing_input_uses_left_slot_without_body_hit(self):
        self._place_left_input_attachment("ground")

    def test_supply_attachment_on_left_facing_input_uses_left_slot_without_body_hit(self):
        self._place_left_input_attachment("supply")

    def test_overlap_error_names_both_parts(self):
        a = horizontal("A")
        huge = OrientedGeometry({"1": PinGeometry((-3.81, 0.0), LEFT), "2": PinGeometry((3.81, 0.0), RIGHT)},
                               (-600.0, -30.0, 600.0, 20.0), (-598.0, -28.0, 598.0, 18.0))
        # (too wide for the free slots a feedback part searches along its wire, so the final check names the pair)
        feedback = PlacedSymbol("FB", (64.0, 50.8), FLAT, {FLAT: huge})
        roles = Roles({}, (), {"main": ("A",)}, {}, {}, {"FB": Feedback("in", "out")}, (), ())
        nets = [Connection("in", (("A", "1"), ("FB", "1"))), Connection("out", (("A", "2"), ("FB", "2")))]
        with self.assertRaisesRegex(ValueError, "A and FB"):
            ladder_layout([a, feedback], nets, roles, text_widths={}, gap_mm=0)

    def test_chain_two_pin_orientation_must_be_horizontal_and_earlier_net_is_left(self):
        r1 = horizontal("R1")
        r1 = PlacedSymbol("R1", r1.position, FLAT, {
            FLAT: r1.geometry[FLAT],
            Orientation(90): rotate_geometry(r1.geometry[FLAT], 90),
        })
        r2 = horizontal("R2")
        roles = Roles({}, (), {"main": ("R1", "R2")}, {}, {}, {}, (), ())
        result = ladder_layout([r1, r2], [Connection("MID", (("R1", "2"), ("R2", "1")))], roles, text_widths={})
        self.assertEqual(result["R1"].orientation, FLAT)
        self.assertEqual(result["R2"].orientation, FLAT)
        self.assertLess(result["R1"].position[0], result["R2"].position[0])

    def test_chain_endpoint_rejects_a_vertical_two_pin_orientation(self):
        upright = horizontal("L101")
        vertical_geometry = rotate_geometry(upright.geometry[FLAT], 90)
        symbol = PlacedSymbol("L101", upright.position, FLAT, {Orientation(90): vertical_geometry})
        roles = Roles({}, (), {"main": ("L101",)}, {}, {}, {}, (), ())
        with self.assertRaisesRegex(ValueError, "No available orientation for L101"):
            ladder_layout([symbol], [], roles, text_widths={})

    def test_mult_pin_chain_mirrors_only_when_upright_flow_points_backwards(self):
        base = OrientedGeometry({
            "IN": PinGeometry((5.08, 0.0), RIGHT),
            "OUT": PinGeometry((-5.08, 0.0), LEFT),
            "G": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08))
        chip = PlacedSymbol("U1", (64.0, 50.8), FLAT, {FLAT: base, Orientation(0, "y"): rotate_geometry(base, 0, "y")})
        roles = Roles({}, (), {"main": ("R1", "U1", "R2")}, {}, {}, {}, (), ())
        r1, r2 = horizontal("R1"), horizontal("R2")
        nets = [Connection("IN", (("R1", "2"), ("U1", "IN"))), Connection("OUT", (("U1", "OUT"), ("R2", "1")))]
        result = ladder_layout([r1, chip, r2], nets, roles, text_widths={})
        self.assertEqual(result["U1"].orientation, Orientation(0, "y"))
        geom = chip.geometry[result["U1"].orientation]
        self.assertLess(geom.pins["IN"].normal[0], 0)
        self.assertGreater(geom.pins["OUT"].normal[0], 0)

    def test_transistor_with_vertical_chain_pins_is_turned_to_run_left_to_right(self):
        base = OrientedGeometry({
            "G": PinGeometry((-5.08, 0.0), LEFT),
            "D": PinGeometry((2.54, -5.08), UP),
            "S": PinGeometry((2.54, 5.08), DOWN),
        }, (-5.08, -5.08, 2.54, 5.08), (-2.54, -3.81, 2.54, 3.81))
        # Rotation 90 here turns up to left?  Let rotate_geometry say which turn does it.
        turns = {Orientation(r, m): rotate_geometry(base, r, m) for r in (0, 90, 180, 270) for m in ("", "y")}
        wanted = [o for o, g in turns.items() if g.pins["D"].normal[0] < -.5 and g.pins["S"].normal[0] > .5]
        self.assertTrue(wanted)
        q = PlacedSymbol("Q1", (64.0, 50.8), FLAT, turns)
        roles = Roles({}, (), {"main": ("R1", "Q1", "R2")}, {}, {}, {}, (), ())
        nets = [Connection("A", (("R1", "2"), ("Q1", "D"))), Connection("B", (("Q1", "S"), ("R2", "1")))]
        result = ladder_layout([horizontal("R1"), q, horizontal("R2")], nets, roles, text_widths={})
        self.assertEqual(result["Q1"].orientation, min(wanted, key=lambda o: (o.rotation, o.mirror)))
        geom = turns[result["Q1"].orientation]
        self.assertLess(geom.pins["D"].normal[0], 0)
        self.assertGreater(geom.pins["S"].normal[0], 0)

    def test_attachment_slot_failure_counts_rejections_by_reason(self):
        symbols, nets, roles = toy()
        # A very wide attachment leaves no free slot within reach of the host.
        wide = part("C3", {"1": PinGeometry((0.0, 5.08), DOWN), "2": PinGeometry((0.0, -5.08), UP)},
                    (-400.0, -5.08, 400.0, 5.08), body=(-2.0, -2.0, 2.0, 2.0))
        symbols = [wide if s.reference == "C3" else s for s in symbols]
        with self.assertRaises(ValueError) as caught:
            ladder_layout(symbols, nets, roles, text_widths={})
        message = str(caught.exception)
        self.assertRegex(message, r"No attachment slot for \w+ on \w+ \(host .*row y=.*, \d+ candidates: "
                                  r"\d+ crowded, \d+ near a pin, \d+ route blocked\)")
        counts = [int(n) for n in __import__("re").findall(r"(\d+) (?:crowded|near a pin|route blocked)", message)]
        self.assertEqual(sum(counts), int(__import__("re").search(r"(\d+) candidates", message).group(1)))

    def test_terminal_chip_tail_infers_feedback_output_pin_for_chain_row(self):
        source = horizontal("R2")
        chip = part("U1", {
            "VI": PinGeometry((-5.08, -2.54), LEFT),
            "VO": PinGeometry((5.08, 0.0), RIGHT),
            "ADJ": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81))
        feedback = horizontal("FB")
        nets = [
            Connection("input", (("R2", "2"), ("U1", "VI"), ("FB", "1"))),
            Connection("output", (("U1", "VO"), ("FB", "2"))),
        ]
        roles = Roles({}, (), {"main": ("R2", "U1")}, {}, {},
                      {"FB": Feedback("input", "output")}, (), ())
        result = ladder_layout([source, chip, feedback], nets, roles, text_widths={})
        geom = chip.geometry[result["U1"].orientation]
        self.assertEqual(result["U1"].orientation, FLAT)
        # The chain enters on VI (on the row) and leaves from VO at VO's own height: the row steps there.
        self.assertAlmostEqual(result["U1"].position[1] + geom.pins["VI"].point[1], 0.0)
        self.assertAlmostEqual(result["U1"].position[1] + geom.pins["VO"].point[1], 2.54)
        self.assertEqual(geom.pins["VO"].normal, RIGHT)
        self.assertEqual(geom.pins["VI"].normal, LEFT)

    def test_terminal_feedback_output_overrides_backward_upright_normal_and_hangs_c3_from_the_output_wire(self):
        source = horizontal("R2")
        # The library's upright output points left. Feedback membership identifies it,
        # so the existing y-mirror rule can put it on the right without moving it off row 0.
        base = part("U1", {
            "VI": PinGeometry((5.08, -2.54), RIGHT),
            "VO": PinGeometry((-5.08, 0.0), LEFT),
            "ADJ": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81))
        # Populate the mirrored geometry exactly as a library adapter would.
        chip = PlacedSymbol("U1", base.position, FLAT, {
            FLAT: base.geometry[FLAT],
            Orientation(0, "y"): rotate_geometry(base.geometry[FLAT], 0, "y"),
        })
        feedback = horizontal("FB")
        cap = vertical("C3", reverse=True)
        nets = [
            Connection("input", (("R2", "2"), ("U1", "VI"), ("FB", "1"))),
            Connection("output", (("U1", "VO"), ("FB", "2"), ("C3", "2"))),
            Connection("GND", (("C3", "1"),), style="power"),
        ]
        roles = Roles({"GND": "ground"}, ("output",), {"main": ("R2", "U1")},
                      {"C3": Attachment("U1", "output", "GND", "ground")}, {},
                      {"FB": Feedback("input", "output")}, (), ())
        result = ladder_layout([source, chip, feedback, cap], nets, roles, text_widths={})
        geom = chip.geometry[result["U1"].orientation]
        self.assertEqual(result["U1"].orientation, Orientation(0, "y"))
        self.assertAlmostEqual(result["U1"].position[1] + geom.pins["VI"].point[1], 0.0)
        c3_geom = cap.geometry[result["C3"].orientation]
        # The output net runs at VO's height, and C3 hangs from that wire.
        self.assertAlmostEqual(
            result["C3"].position[1] + c3_geom.pins["2"].point[1],
            result["U1"].position[1] + geom.pins["VO"].point[1],
        )

    def test_disconnected_single_member_blocks_follow_their_horizontal_net_normals(self):
        source = horizontal("J1")
        sink = horizontal("J2")
        roles = Roles({}, (), {"z_source": ("J1",), "a_sink": ("J2",)}, {}, {}, {}, (), ())
        result = ladder_layout([source, sink], [Connection("LINK", (("J1", "2"), ("J2", "1")))], roles, text_widths={})
        self.assertLess(result["J1"].position[0], result["J2"].position[0])

    def test_single_part_blocks_follow_a_transistors_drawn_flow_and_pull_their_neighbours_after_it(self):
        q = part("Q1", {
            "G": PinGeometry((-5.08, 0.0), LEFT),
            "D": PinGeometry((2.54, -5.08), UP),
            "S": PinGeometry((2.54, 5.08), DOWN),
        }, (-5.08, -5.08, 2.54, 5.08), body=(-2.54, -3.81, 2.54, 3.81))
        # Both neighbours lie as drawn with the same pin direction on the shared net, so only Q1 knows the order.
        def turnable(symbol):
            up = symbol.geometry[FLAT]
            return PlacedSymbol(symbol.reference, symbol.position, FLAT,
                                {Orientation(r, m): rotate_geometry(up, r, m) for r in (0, 90, 180, 270) for m in ("", "y")})
        q = turnable(q)
        src, load1, load2 = (turnable(vertical(n, reverse=rev)) for n, rev in (("A_SRC", True), ("B_L", True), ("C_R", False)))
        roles = Roles({}, (), {"a": ("C_R",), "b": ("B_L",), "c": ("Q1",), "d": ("A_SRC",)}, {}, {}, {}, (), ())
        nets = [Connection("IN", (("A_SRC", "1"), ("Q1", "D"))), Connection("MID", (("Q1", "S"), ("B_L", "1"))),
                Connection("OUT", (("B_L", "2"), ("C_R", "1")))]
        result = ladder_layout([q, src, load1, load2], nets, roles, text_widths={})
        xs = [result[r].position[0] for r in ("A_SRC", "Q1", "B_L", "C_R")]
        self.assertEqual(xs, sorted(xs))

    def test_shared_rail_does_not_order_independent_chains(self):
        source, sink = horizontal("J1"), horizontal("J2")
        roles = Roles({"GND": "ground"}, (), {"z_block": ("J1",), "a_block": ("J2",)}, {}, {}, {}, (), ())
        result = ladder_layout([source, sink], [Connection("GND", (("J1", "2"), ("J2", "1")))], roles, text_widths={})
        # Joined by a rail alone, neither block is ordered by the other: they stand in one column, in the order of the
        # roles downward, the last just above the first row, rather than flow being invented from GND.
        self.assertEqual(result["J2"].position[0], result["J1"].position[0])
        self.assertLess(result["J1"].position[1], result["J2"].position[1])

    def test_cross_block_chain_net_stays_on_chain_row_and_beats_larger_shared_net(self):
        source = horizontal("J1")
        regulator = part("U1", {
            "VI": PinGeometry((-5.08, 0.0), LEFT),
            "VO": PinGeometry((5.08, 0.0), RIGHT),
            "ADJ": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08))
        diode = horizontal("D2")
        load = horizontal("J2")
        nets = [
            Connection("in", (("J1", "2"), ("U1", "VI"))),
            Connection("rect", (("U1", "ADJ"), ("D2", "1"))),
            Connection("out", (("U1", "VO"), ("D2", "2"), ("J2", "1"), ("A1", "1"))),
        ]
        roles = Roles({}, ("out",), {"z_regulator": ("U1", "D2"), "a_load": ("J2",)}, {}, {}, {}, (), ())
        result = ladder_layout([source, regulator, diode, load], nets, roles, text_widths={})
        ugeom = regulator.geometry[result["U1"].orientation]
        # The chain enters U1 on VI (the row) and leaves through ADJ: the row steps to ADJ's height and J2 follows it.
        self.assertAlmostEqual(result["U1"].position[1] + ugeom.pins["VI"].point[1], 0.0)
        self.assertAlmostEqual(result["J2"].position[1], result["U1"].position[1] + ugeom.pins["ADJ"].point[1])

    @staticmethod
    def _tip(symbol, result, pin):
        geom = symbol.geometry[result[symbol.reference].orientation]
        position = result[symbol.reference].position
        return (position[0] + geom.pins[pin].point[0], position[1] + geom.pins[pin].point[1])

    def _interface_ladder(self):
        """Two blocks joined by net ``mid`` (three pins, so a trunk by count) with a ground part on it."""
        symbols = [
            horizontal("J1"),
            part("U1", {"VI": PinGeometry((-5.08, 0.0), LEFT), "VO": PinGeometry((5.08, 0.0), RIGHT)},
                 (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81)),
            horizontal("J2"), horizontal("J3"), vertical("C9"),
        ]
        nets = [
            Connection("in", (("J1", "2"), ("U1", "VI"))),
            Connection("mid", (("U1", "VO"), ("J2", "1"), ("C9", "1"))),
            Connection("out", (("J2", "2"), ("J3", "1"))),
            Connection("GND", (("C9", "2"),), style="power"),
        ]
        roles = Roles(
            {"GND": "ground"}, ("mid",), {"a_front": ("J1", "U1"), "b_back": ("J2", "J3")},
            {"C9": Attachment("U1", "mid", "GND", "ground")}, {}, {}, (), (),
        )
        return symbols, nets, roles

    def test_interface_net_lies_on_the_chain_row_and_its_attachment_stands_beside_the_host(self):
        symbols, nets, roles = self._interface_ladder()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        u1 = next(s for s in symbols if s.reference == "U1")
        c9 = next(s for s in symbols if s.reference == "C9")
        j2 = next(s for s in symbols if s.reference == "J2")
        # no trunk row of its own: the attachment's tip is on the chain row, between the host and the next block
        self.assertAlmostEqual(self._tip(c9, result, "1")[1], 0.0)
        self.assertGreater(self._tip(c9, result, "1")[0], self._tip(u1, result, "VO")[0])
        self.assertLess(self._tip(c9, result, "1")[0], self._tip(j2, result, "1")[0])

    def test_multi_pin_host_pin_off_the_chain_row_sets_the_row_of_its_attachment(self):
        host = part("Q1", {
            "B": PinGeometry((-3.81, 0.0), LEFT), "C": PinGeometry((3.81, 0.0), RIGHT),
            "E": PinGeometry((3.81, 5.08), RIGHT),
        }, (-3.81, -3.81, 3.81, 6.35), body=(-2.54, -3.81, 2.54, 6.35))
        symbols = [horizontal("J1"), host, horizontal("J2"), vertical("R9")]
        nets = [
            Connection("in", (("J1", "2"), ("Q1", "B"))),
            Connection("out", (("Q1", "C"), ("J2", "1"))),
            Connection("emit", (("Q1", "E"), ("R9", "1"))),
            Connection("GND", (("R9", "2"),), style="power"),
        ]
        roles = Roles({"GND": "ground"}, (), {"main": ("J1", "Q1", "J2")},
                      {"R9": Attachment("Q1", "emit", "GND", "ground")}, {}, {}, (), ())
        result = ladder_layout(symbols, nets, roles, text_widths={})
        self.assertAlmostEqual(self._tip(host, result, "C")[1], 0.0)
        r9 = symbols[-1]
        self.assertAlmostEqual(self._tip(host, result, "E")[1], 5.08)
        self.assertAlmostEqual(self._tip(r9, result, "1")[1], 5.08)
        shuffled = list(symbols)
        random.Random(5).shuffle(shuffled)
        self.assertEqual(ladder_layout(shuffled, list(reversed(nets)), roles, text_widths={}), result)

    def test_attachment_under_a_host_pin_that_points_at_it_hangs_at_the_pin_x_clear_of_the_host(self):
        host = part("Q1", {
            "B": PinGeometry((-3.81, 0.0), LEFT), "C": PinGeometry((3.81, 0.0), RIGHT),
            "E": PinGeometry((0.0, 5.08), DOWN),
        }, (-3.81, -3.81, 3.81, 6.35), body=(-2.54, -3.81, 2.54, 5.08))
        symbols = [horizontal("J1"), host, horizontal("J2"), vertical("R9")]
        nets = [
            Connection("in", (("J1", "2"), ("Q1", "B"))),
            Connection("out", (("Q1", "C"), ("J2", "1"))),
            Connection("emit", (("Q1", "E"), ("R9", "1"))),
            Connection("GND", (("R9", "2"),), style="power"),
        ]
        roles = Roles({"GND": "ground"}, (), {"main": ("J1", "Q1", "J2")},
                      {"R9": Attachment("Q1", "emit", "GND", "ground")}, {}, {}, (), ())
        result = ladder_layout(symbols, nets, roles, text_widths={})
        r9 = symbols[-1]
        e_tip, r_tip = self._tip(host, result, "E"), self._tip(r9, result, "1")
        self.assertAlmostEqual(r_tip[0], e_tip[0])
        self.assertGreaterEqual(r_tip[1], e_tip[1])
        self.assertLess(r_tip[1] - e_tip[1], 10.2)
        self.assertAlmostEqual(r_tip[1] - e_tip[1], 2.54)   # one grid step of straight wire from the source tip

    def test_a_sense_resistor_on_a_sources_other_pin_hangs_under_the_vertical_source(self):
        flat = horizontal("V1")
        geometry = {Orientation(r): rotate_geometry(flat.geometry[FLAT], r) for r in (0, 90, 180, 270)}
        symbols = [PlacedSymbol("V1", flat.position, FLAT, geometry), horizontal("R1"), vertical("R3", reverse=True)]
        nets = [
            Connection("sig", (("V1", "1"), ("R1", "1"))),
            Connection("sense", (("V1", "2"), ("R3", "2"))),
            Connection("GND", (("R3", "1"),), style="power"),
        ]
        roles = Roles({"GND": "ground"}, (), {"a_src": ("V1",), "b_load": ("R1",)},
                      {"R3": Attachment("V1", "sense", "GND", "ground")}, {}, {}, (), ())
        result = ladder_layout(symbols, nets, roles, text_widths={})
        v = symbols[0].geometry[result["V1"].orientation]
        self.assertLess(v.pins["1"].normal[1], -.5)          # chain pin up, on the row
        self.assertGreater(v.pins["2"].normal[1], .5)        # the other pin down
        self.assertAlmostEqual(self._tip(symbols[0], result, "1")[1], 0.0)
        r3 = self._tip(symbols[2], result, "2")
        v2 = self._tip(symbols[0], result, "2")
        self.assertAlmostEqual(r3[0], v2[0])                 # under the source, in its column
        self.assertGreater(r3[1], v2[1] - 1e-7)
        shuffled = list(symbols)
        random.Random(4).shuffle(shuffled)
        self.assertEqual(ladder_layout(shuffled, list(reversed(nets)), roles, text_widths={}), result)

    def test_a_block_feeding_a_transistors_base_is_on_the_row_to_its_left_and_the_transistor_stays_upright(self):
        q = part("Q", {
            "B": PinGeometry((-5.08, 0.0), LEFT),
            "C": PinGeometry((2.54, -5.08), UP),
            "E": PinGeometry((2.54, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81))
        symbols = [horizontal("F1"), horizontal("F2"), q, vertical("R9", reverse=True)]
        nets = [
            Connection("mid", (("F1", "2"), ("F2", "1"))),
            Connection("base", (("F2", "2"), ("Q", "B"))),
            Connection("emit", (("Q", "E"), ("R9", "2"))),
            Connection("GND", (("R9", "1"),), style="power"),
        ]
        roles = Roles({"GND": "ground"}, (), {"drive": ("F1", "F2"), "stage": ("Q",)},
                      {"R9": Attachment("Q", "emit", "GND", "ground")}, {}, {}, (), ())
        result = ladder_layout(symbols, nets, roles, text_widths={})
        self.assertEqual(result["Q"].orientation, FLAT)
        by_ref = {s.reference: s for s in symbols}
        base = self._tip(q_symbol := by_ref["Q"], result, "B")
        for ref in ("F1", "F2"):
            self.assertLess(extent(by_ref[ref], result)[2], extent(q_symbol, result)[0])
        self.assertAlmostEqual(self._tip(by_ref["F2"], result, "2")[1], base[1])
        self.assertLess(result["F1"].position[0], result["F2"].position[0])
        shuffled = list(symbols)
        random.Random(8).shuffle(shuffled)
        self.assertEqual(ladder_layout(shuffled, list(reversed(nets)), roles, text_widths={}), result)

    def _bjt_stage(self):
        """C1 -> Q1 base, Q1 collector -> C2, with parts hung on the C1-Q1 wire (two grounded, one on supply)."""
        q1 = part("Q1", {
            "B": PinGeometry((-5.08, 0.0), LEFT),
            "C": PinGeometry((2.54, -5.08), UP),
            "E": PinGeometry((2.54, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81))
        symbols = [horizontal("C1"), q1, horizontal("C2"),
                   vertical("R3", reverse=True), vertical("R3b", reverse=True), vertical("R4")]
        nets = [
            Connection("b", (("C1", "2"), ("Q1", "B"), ("R3", "2"), ("R3b", "2"), ("R4", "2"))),
            Connection("c", (("Q1", "C"), ("C2", "1"))),
            Connection("out", (("C2", "2"),)),
            Connection("GND", (("R3", "1"), ("R3b", "1")), style="power"),
            Connection("VCC", (("R4", "1"),), style="power"),
        ]
        roles = Roles(
            {"GND": "ground", "VCC": "supply"}, (), {"main": ("C1", "Q1", "C2")},
            {"R3": Attachment("C1", "b", "GND", "ground"), "R3b": Attachment("C1", "b", "GND", "ground"),
             "R4": Attachment("C1", "b", "VCC", "supply")}, {}, {}, (), (),
        )
        return symbols, nets, roles

    def test_chain_enters_a_multi_pin_part_on_its_input_pin_and_the_row_steps_to_its_output(self):
        symbols, nets, roles = self._bjt_stage()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        tip = lambda ref, pin: self._tip(by_ref[ref], result, pin)  # noqa: E731
        # C1 runs straight into the base on the row; the collector leaves 5.08 mm higher and C2 follows it there.
        self.assertAlmostEqual(tip("C1", "2")[1], 0.0)
        self.assertAlmostEqual(tip("Q1", "B")[1], 0.0)
        self.assertAlmostEqual(tip("Q1", "C")[1], -5.08)
        self.assertAlmostEqual(tip("C2", "1")[1], -5.08)
        self.assertAlmostEqual(tip("C2", "2")[1], -5.08)

    def test_attachments_hang_on_the_wire_between_two_chain_parts_nearest_the_serving_pin_first(self):
        symbols, nets, roles = self._bjt_stage()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        base, coupling = self._tip(by_ref["Q1"], result, "B"), self._tip(by_ref["C1"], result, "2")
        tips = {ref: self._tip(by_ref[ref], result, "2") for ref in ("R3", "R3b", "R4")}
        for ref, (x, y) in tips.items():
            # the tip is on the wire, or one grid step off it along a short straight wire when another tip would coincide
            self.assertTrue(abs(y) < 1e-7 or abs(abs(y) - 2.54) < 1e-7, msg=ref)
            self.assertTrue(coupling[0] < x < base[0], f"{ref} lies along the C1-Q1 wire")
        # R3 stands beside the base (the pin it serves, not the coupling capacitor's), R3b next, outward along the wire
        self.assertLess(base[0] - tips["R3"][0], base[0] - tips["R3b"][0])
        # ground side below the wire, supply side above it
        r3, r4 = result["R3"].position[1] + by_ref["R3"].geometry[result["R3"].orientation].box[1],             result["R4"].position[1] + by_ref["R4"].geometry[result["R4"].orientation].box[3]
        self.assertGreaterEqual(r3, -1e-7)
        self.assertLessEqual(r4, 1e-7)
        shuffled = list(symbols)
        random.Random(9).shuffle(shuffled)
        self.assertEqual(ladder_layout(shuffled, list(reversed(nets)), roles, text_widths={}), result)

    def test_a_stack_of_two_hung_parts_shares_an_x_with_a_grid_step_of_wire_between_their_tips(self):
        symbols, nets, roles = self._two_parts(1, 1)
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        g, v = self._tip(by_ref["G0"], result, "2"), self._tip(by_ref["S0"], result, "2")
        self.assertAlmostEqual(g[0], v[0])
        self.assertGreaterEqual(abs(g[1] - v[1]), 2.54 - 1e-7)

    def test_coincident_pin_tips_of_two_parts_are_rejected_by_the_legality_check(self):
        from backend.schematic.ladder_layout import _reject_coincident_tips
        _reject_coincident_tips([(0.0, 0.0, "A"), (0.0, 2.54, "B"), (5.08, 0.0, "C")])      # a grid step apart: fine
        _reject_coincident_tips([(1.0, 1.0, "A"), (1.0, 1.0, "A")])                          # one part's own pins: fine
        with self.assertRaisesRegex(ValueError, "A and B"):
            _reject_coincident_tips([(0.0, 0.0, "A"), (0.0, 0.0, "B")])

    def test_hung_parts_keep_their_bodies_apart_while_tips_may_meet_a_wire(self):
        symbols, nets, roles = self._two_parts(1, 0)
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        from backend.schematic.ladder_layout import _core
        def core(ref):
            l, t, r, b = _core(by_ref[ref].geometry[result[ref].orientation])
            x, y = result[ref].position
            return (x + l, y + t, x + r, y + b)
        for first in result:
            for second in result:
                if first < second:
                    a, b = core(first), core(second)
                    separated_x = max(a[0] - b[2], b[0] - a[2])
                    separated_y = max(a[1] - b[3], b[1] - a[3])
                    self.assertGreaterEqual(max(separated_x, separated_y), -1e-7)

    def _source_and_load(self, first):
        """A two-pin part with one rail pin and one signal pin feeding R1; every orientation is available."""
        flat = horizontal(first, ("2", "1"))
        geometry = {Orientation(r): rotate_geometry(flat.geometry[FLAT], r) for r in (0, 90, 180, 270)}
        symbols = [PlacedSymbol(first, flat.position, FLAT, geometry), horizontal("R1")]
        nets = [Connection("sig", ((first, "1"), ("R1", "1"))), Connection("GND", ((first, "2"),), style="power")]
        roles = Roles({"GND": "ground"}, (), {"a_first": (first,), "b_rest": ("R1",)}, {}, {}, {}, (), ())
        return symbols, nets, roles

    def test_a_source_feeding_the_row_stands_vertical_with_its_signal_pin_on_the_row_at_the_left_end(self):
        symbols, nets, roles = self._source_and_load("V1")
        result = ladder_layout(symbols, nets, roles, text_widths={})
        geom = symbols[0].geometry[result["V1"].orientation]
        self.assertLess(geom.pins["1"].normal[1], -.5)      # signal pin up
        self.assertGreater(geom.pins["2"].normal[1], .5)    # rail pin down
        self.assertAlmostEqual(self._tip(symbols[0], result, "1")[1], 0.0)
        self.assertAlmostEqual(self._tip(symbols[1], result, "1")[1], 0.0)
        # the first non-source part starts beside it
        right = result["V1"].position[0] + geom.box[2]
        self.assertGreaterEqual(result["R1"].position[0] + symbols[1].geometry[result["R1"].orientation].box[0], right)
        self.assertLess(result["R1"].position[0] + symbols[1].geometry[result["R1"].orientation].box[0], right + 12.0)
        shuffled = list(symbols)
        random.Random(2).shuffle(shuffled)
        self.assertEqual(ladder_layout(shuffled, list(reversed(nets)), roles, text_widths={}), result)

    def test_a_part_that_is_not_a_source_stays_horizontal_at_the_start_of_a_chain(self):
        symbols, nets, roles = self._source_and_load("R9")
        result = ladder_layout(symbols, nets, roles, text_widths={})
        self.assertEqual(result["R9"].orientation, FLAT)

    def test_feedback_is_horizontal_above_the_supply_area_and_pairs_are_adjacent(self):
        chain = ("J1", "U1", "RLOAD")
        symbols = [horizontal("J1"), horizontal("RLOAD"), horizontal("FB"),
                   part("U1", {"VI": PinGeometry((-5.08, 0), LEFT), "VO": PinGeometry((5.08, 0), RIGHT), "ADJ": PinGeometry((0, 5.08), DOWN)}, (-5.08, -5.08, 5.08, 5.08)),
                   part("Q2", {"B": PinGeometry((-3.81, 0), LEFT), "C": PinGeometry((0, -3.81), UP), "E": PinGeometry((0, 3.81), DOWN)}, (-3.81, -3.81, 3.81, 3.81)),
                   part("Q3", {"B": PinGeometry((-3.81, 0), LEFT), "C": PinGeometry((0, -3.81), UP), "E": PinGeometry((0, 3.81), DOWN)}, (-3.81, -3.81, 3.81, 3.81))]
        nets = [Connection("in", (("J1", "2"), ("U1", "VI"))), Connection("out", (("U1", "VO"), ("RLOAD", "1"))),
                Connection("f1", (("FB", "1"), ("U1", "VI"))), Connection("f2", (("FB", "2"), ("U1", "ADJ")))]
        roles = Roles({}, (), {"main": chain}, {}, {}, {"FB": Feedback("f1", "f2")}, (("Q2", "Q3"),), ())
        result = ladder_layout(symbols, nets, roles, text_widths={})
        self.assertEqual(result["FB"].orientation, FLAT)
        self.assertLess(result["FB"].position[1], result["J1"].position[1])
        self.assertEqual(result["Q2"].orientation, FLAT)
        self.assertAlmostEqual(result["Q3"].position[0] - result["Q2"].position[0], 7.62)


def switch(ref="Q"):
    """A three-pin part drawn with its control pin (G) left and its through pins (D above S) on the axis."""
    base = OrientedGeometry({
        "G": PinGeometry((-5.08, 0.0), LEFT),
        "D": PinGeometry((2.54, -5.08), UP),
        "S": PinGeometry((2.54, 5.08), DOWN),
    }, (-5.08, -5.08, 2.54, 5.08), (-2.54, -3.81, 2.54, 3.81))
    geometry = {Orientation(r, m): rotate_geometry(base, r, m) for r in (0, 90, 180, 270) for m in ("", "y")}
    return PlacedSymbol(ref, (64.0, 50.8), FLAT, geometry)


def either_way_up(ref):
    """A vertical two-pin part that may also be turned half a turn."""
    base = vertical(ref).geometry[FLAT]
    return PlacedSymbol(ref, (64.0, 50.8), FLAT, {FLAT: base, Orientation(180): rotate_geometry(base, 180)})


def extent(symbol, result):
    geometry = symbol.geometry[result[symbol.reference].orientation]
    x, y = result[symbol.reference].position
    return (x + geometry.box[0], y + geometry.box[1], x + geometry.box[2], y + geometry.box[3])


class LadderBlockPlanTests(unittest.TestCase):
    """Through and control pins, and blocks with no interface net between them."""

    def control_toy(self):
        symbols = [switch(), horizontal("V"), either_way_up("F1"), either_way_up("F2"), horizontal("L")]
        nets = [
            Connection("VIN", (("V", "2"), ("Q", "D"))),
            Connection("gg", (("F2", "2"), ("Q", "G"))),
            Connection("MID", (("F1", "2"), ("F2", "1"))),
            Connection("OUT", (("Q", "S"), ("L", "1"))),
            Connection("GND", (("F1", "1"), ("V", "1"), ("L", "2")), style="power"),
        ]
        roles = Roles({"GND": "ground"}, (),
                      {"drive": ("F1", "F2"), "filter": ("L",), "supply": ("V",), "switch": ("Q",)},
                      {}, {}, {}, (), ())
        return symbols, nets, roles

    def test_through_pins_run_the_row_and_the_control_pin_feeder_hangs_below_in_order(self):
        symbols, nets, roles = self.control_toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        q = by_ref["Q"].geometry[result["Q"].orientation]
        self.assertLess(q.pins["D"].normal[0], -.5)
        self.assertGreater(q.pins["S"].normal[0], .5)
        self.assertGreater(q.pins["G"].normal[1], .5)
        # The block feeding the drain is on the row before the part; the block on the source follows it.
        self.assertLess(result["V"].position[0], result["Q"].position[0])
        self.assertLess(result["Q"].position[0], result["L"].position[0])
        gate_x = result["Q"].position[0] + q.pins["G"].point[0]
        gate_y = result["Q"].position[1] + q.pins["G"].point[1]
        f2, f1 = extent(by_ref["F2"], result), extent(by_ref["F1"], result)
        # Both parts stand in the gate's column, the last part nearest the pin, the first farthest.
        for ref in ("F1", "F2"):
            geometry = by_ref[ref].geometry[result[ref].orientation]
            self.assertAlmostEqual(result[ref].position[0] + geometry.pins["1"].point[0], gate_x)
        f2_geometry = by_ref["F2"].geometry[result["F2"].orientation]
        self.assertAlmostEqual(result["F2"].position[0] + f2_geometry.pins["2"].point[0], gate_x)
        self.assertGreaterEqual(f2[1], gate_y)
        self.assertGreaterEqual(f1[1], f2[3])
        # Nothing else of the row stands in the column's footprint.
        for ref in ("V", "L"):
            other = extent(by_ref[ref], result)
            self.assertTrue(other[2] <= min(f1[0], f2[0]) or other[0] >= max(f1[2], f2[2]) or other[3] <= f2[1])

    def test_control_pin_hang_is_independent_of_input_order(self):
        symbols, nets, roles = self.control_toy()
        expected = ladder_layout(symbols, nets, roles, text_widths={})
        rng = random.Random(7)
        for _ in range(6):
            shuffled_symbols, shuffled_nets = symbols[:], nets[:]
            rng.shuffle(shuffled_symbols)
            rng.shuffle(shuffled_nets)
            blocks = list(roles.chains.items())
            rng.shuffle(blocks)
            shuffled_roles = Roles(roles.rails, (), dict(blocks), {}, {}, {}, (), ())
            self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, shuffled_roles, text_widths={}), expected)

    def parallel_toy(self):
        symbols = [horizontal(ref) for ref in ("S", "A1", "A2", "B1", "B2")]
        nets = [
            Connection("in", (("S", "2"), ("A1", "1"), ("B1", "1"))),
            Connection("am", (("A1", "2"), ("A2", "1"))),
            Connection("bm", (("B1", "2"), ("B2", "1"))),
        ]
        roles = Roles({}, ("in",), {"source": ("S",), "a_stage": ("A1", "A2"), "b_stage": ("B1", "B2")},
                      {}, {}, {}, (), ())
        return symbols, nets, roles

    def test_blocks_without_an_interface_net_stack_and_the_shared_source_stays_on_the_first_row(self):
        symbols, nets, roles = self.parallel_toy()
        result = ladder_layout(symbols, nets, roles, text_widths={}, gap_mm=2.54)
        by_ref = {s.reference: s for s in symbols}
        first = [extent(by_ref[ref], result) for ref in ("S", "A1", "A2")]
        second = [extent(by_ref[ref], result) for ref in ("B1", "B2")]
        self.assertAlmostEqual(result["S"].position[1], result["A1"].position[1])
        self.assertLess(result["S"].position[0], result["A1"].position[0])
        self.assertLess(result["A1"].position[0], result["A2"].position[0])
        self.assertLess(result["B1"].position[0], result["B2"].position[0])
        self.assertAlmostEqual(result["B1"].position[1], result["B2"].position[1])
        # Left-aligned with the first row; below it by at least two gaps.
        self.assertAlmostEqual(min(b[0] for b in second), min(b[0] for b in first))
        self.assertGreaterEqual(min(b[1] for b in second), max(b[3] for b in first) + 2 * 2.54 - 1e-6)
        self.assertEqual(sorted(result), ["A1", "A2", "B1", "B2", "S"])

    def test_stacked_rows_do_not_depend_on_input_order(self):
        symbols, nets, roles = self.parallel_toy()
        expected = ladder_layout(symbols, nets, roles, text_widths={})
        rng = random.Random(11)
        for _ in range(6):
            shuffled_symbols, shuffled_nets = symbols[:], nets[:]
            rng.shuffle(shuffled_symbols)
            rng.shuffle(shuffled_nets)
            # The order of ``roles.chains`` is the roles' flow order and is part of the input.
            self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}), expected)

    def test_blocks_joined_to_the_rest_by_rails_alone_stand_in_a_column_above_the_first_row(self):
        symbols, nets, roles = self.parallel_toy()
        supply = either_way_up("VS")
        symbols = [*symbols, supply]
        nets = [*nets, Connection("VCC", (("VS", "1"),), style="power"), Connection("GND", (("VS", "2"), ("B2", "2")), style="power")]
        roles = Roles({"VCC": "supply", "GND": "ground"}, ("in",), {**roles.chains, "supplies": ("VS",)}, {}, {}, {}, (), ())
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        first = [extent(by_ref[ref], result) for ref in ("S", "A1", "A2")]
        box = extent(supply, result)
        self.assertAlmostEqual(box[0], min(b[0] for b in first))
        self.assertLessEqual(box[3], min(b[1] for b in first) - 2 * 2.54 + 1e-6)
        geometry = supply.geometry[result["VS"].orientation]
        self.assertLess(geometry.pins["1"].point[1], geometry.pins["2"].point[1])

    def test_rail_only_parts_the_roles_leave_over_stack_in_the_supplies_column(self):
        symbols, nets, roles = self.parallel_toy()
        symbols = [*symbols, either_way_up("V2"), either_way_up("V3")]
        nets = [
            *nets, Connection("VCC", (("V2", "1"),), style="power"), Connection("NEG", (("V3", "2"),), style="power"),
            Connection("GND", (("V2", "2"), ("V3", "1"), ("B2", "2")), style="power"),
        ]
        roles = Roles({"VCC": "supply", "GND": "ground", "NEG": "negative"}, ("in",), roles.chains, {}, {}, {}, (),
                      ("V2", "V3"))
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        first = [extent(by_ref[ref], result) for ref in ("S", "A1", "A2")]
        top, bottom = (extent(by_ref[ref], result) for ref in ("V2", "V3"))
        self.assertAlmostEqual(top[0], min(b[0] for b in first))
        self.assertAlmostEqual(bottom[0], top[0])
        self.assertLessEqual(bottom[3], min(b[1] for b in first) - 2 * 2.54 + 1e-6)
        self.assertGreaterEqual(bottom[1], top[3] + 2 * 2.54 - 1e-6)      # the second is below the first
        for ref, high in (("V2", "1"), ("V3", "1")):
            pins = by_ref[ref].geometry[result[ref].orientation].pins
            low = "2" if high == "1" else "1"
            self.assertLess(pins[high].point[1], pins[low].point[1])      # the higher rail's pin is up


class LadderSideRowTests(unittest.TestCase):
    """A chain part's pins off the chain row: a link leaves a pin along its direction, on a row above or below the part."""

    def toy(self, entered_by_the_top_pin=False):
        """S -> Q -> P -> LOAD.  P has a pin on each side; Q has one pin on its left and two on its right, the lower
        one 10.16 mm below the row.  Q's lower pin and P's bottom pin share a net; P's top pin has a part hung on it."""
        quad = part("P", {
            "L": PinGeometry((-5.08, 0.0), LEFT), "R": PinGeometry((5.08, 0.0), RIGHT),
            "T": PinGeometry((0.0, -5.08), UP), "B": PinGeometry((0.0, 5.08), DOWN),
        }, (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81))
        two = part("Q", {
            "L": PinGeometry((-5.08, 0.0), LEFT), "U": PinGeometry((5.08, 0.0), RIGHT),
            "D": PinGeometry((5.08, 10.16), RIGHT),
        }, (-5.08, -2.54, 5.08, 12.7), body=(-3.81, -2.54, 3.81, 12.7))
        symbols = [horizontal("S"), two, quad, horizontal("LOAD")] + ([] if entered_by_the_top_pin else [vertical("H"), vertical("G", reverse=True)])
        entry = "T" if entered_by_the_top_pin else "L"
        nets = [
            Connection("a", (("S", "2"), ("Q", "L"))),
            Connection("b", (("Q", "U"), ("P", entry))),
            Connection("lower", (("Q", "D"), ("P", "B")) + (() if entered_by_the_top_pin else (("G", "2"),))),
            Connection("out", (("P", "R"), ("LOAD", "1"))),
        ] + ([] if entered_by_the_top_pin else [
            Connection("top", (("P", "T"), ("H", "2"))), Connection("VCC", (("H", "1"),), style="power"),
        ]) + [
            Connection("GND", (("S", "1"), ("LOAD", "2")) + (() if entered_by_the_top_pin else (("G", "1"),)), style="power"),
        ]
        roles = Roles(
            {"GND": "ground", "VCC": "supply"}, (), {"main": ("S", "Q", "P", "LOAD")},
            {} if entered_by_the_top_pin else {
                "H": Attachment("P", "top", "VCC", "supply"), "G": Attachment("P", "lower", "GND", "ground"),
            }, {}, {}, (), (),
        )
        return symbols, nets, roles

    def tips(self, symbols, result, ref):
        by_ref = {s.reference: s for s in symbols}
        geometry, (x, y) = by_ref[ref].geometry[result[ref].orientation], result[ref].position
        return {pin: (x + pg.point[0], y + pg.point[1]) for pin, pg in geometry.pins.items()}

    def test_a_pin_off_the_row_gets_its_net_a_row_on_its_own_side_and_a_shared_net_one_straight_row(self):
        symbols, nets, roles = self.toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        p, q, h = (self.tips(symbols, result, ref) for ref in ("P", "Q", "H"))
        by_ref = {s.reference: s for s in symbols}
        p_box = extent(by_ref["P"], result)
        # the part hung on the top pin stands on a row above the part, one gap beyond the pin tip, at the pin's x
        self.assertAlmostEqual(h["2"][1], p["T"][1] - 2.54)
        self.assertAlmostEqual(h["2"][0], p["T"][0])
        self.assertLess(h["1"][1], h["2"][1])
        # the bottom net is one row below the part: Q's lower pin leaves along it, P's bottom pin is reached from below
        self.assertGreaterEqual(q["D"][1], p["B"][1] + 2.54 - 1e-6)
        self.assertGreater(q["D"][1], p_box[3])
        self.assertAlmostEqual(self.tips(symbols, result, "G")["2"][1], q["D"][1])      # what hangs there stands on the row
        route = estimate_net([(q["D"], RIGHT), (p["B"], DOWN)])
        self.assertEqual(sum(r.bends for r in route), 1)
        for a, b in zip(route[0].points, route[0].points[1:]):
            if abs(a[1] - b[1]) < 1e-9:
                self.assertGreaterEqual(a[1], p_box[3])
        self.assertAlmostEqual(q["U"][1], p["L"][1])

    def test_a_chain_entering_a_part_by_its_top_pin_runs_the_row_through_its_output_and_wraps_over_the_top(self):
        symbols, nets, roles = self.toy(entered_by_the_top_pin=True)
        result = ladder_layout(symbols, nets, roles, text_widths={})
        p, q = self.tips(symbols, result, "P"), self.tips(symbols, result, "Q")
        by_ref = {s.reference: s for s in symbols}
        pins = by_ref["P"].geometry[result["P"].orientation].pins
        self.assertEqual(pins["T"].normal, UP)
        self.assertAlmostEqual(p["R"][1], q["U"][1])                  # the chain row passes through P's output pin
        self.assertLess(p["T"][1], p["R"][1])
        route = estimate_net([(q["U"], RIGHT), (p["T"], UP)])
        top = min(y for r in route for _x, y in r.points)
        self.assertLessEqual(top, p["T"][1] - 2.54 + 1e-6)             # over the top of the part, not across it
        self.assertEqual(sum(1 for r in route for a, b in zip(r.points, r.points[1:])
                             if abs(a[1] - b[1]) < 1e-9 and min(a[0], b[0]) < p["T"][0] < max(a[0], b[0])
                             and p["T"][1] < a[1] < p["B"][1]), 0)

    def test_side_rows_do_not_depend_on_input_order(self):
        for entered in (False, True):
            symbols, nets, roles = self.toy(entered)
            expected = ladder_layout(symbols, nets, roles, text_widths={})
            rng = random.Random(5)
            for _ in range(6):
                shuffled_symbols, shuffled_nets = symbols[:], nets[:]
                rng.shuffle(shuffled_symbols)
                rng.shuffle(shuffled_nets)
                self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}), expected)


class LadderPairAndStackTests(unittest.TestCase):
    """A pair is drawn face to face; a block entering a part's up- or down-pointing pin stacks on that side."""

    def pair_toy(self):
        symbols = [horizontal("F"), switch("QA"), switch("QB"), vertical("RE", reverse=True),
                   vertical("RA"), vertical("RB"), vertical("RG", reverse=True)]
        nets = [
            Connection("g", (("F", "2"), ("QA", "G"))),
            Connection("e", (("QA", "S"), ("QB", "S"), ("RE", "2"))),
            Connection("ca", (("QA", "D"), ("RA", "2"))),
            Connection("cb", (("QB", "D"), ("RB", "2"))),
            Connection("gb", (("QB", "G"), ("RG", "2"))),
            Connection("GND", (("F", "1"), ("RE", "1"), ("RG", "1")), style="power"),
            Connection("VCC", (("RA", "1"), ("RB", "1")), style="power"),
        ]
        roles = Roles(
            {"GND": "ground", "VCC": "supply"}, (), {"pair": ("F", "QA", "QB")},
            {"RE": Attachment("QA", "e", "GND", "ground"), "RA": Attachment("QA", "ca", "VCC", "supply"),
             "RB": Attachment("QB", "cb", "VCC", "supply"), "RG": Attachment("QB", "gb", "GND", "ground")},
            {}, {}, (("QA", "QB"),), (),
        )
        return symbols, nets, roles

    @staticmethod
    def tip(symbols, result, ref, pin):
        geometry = {s.reference: s for s in symbols}[ref].geometry[result[ref].orientation]
        return (result[ref].position[0] + geometry.pins[pin].point[0], result[ref].position[1] + geometry.pins[pin].point[1])

    def test_a_pair_stands_face_to_face_with_its_shared_net_straight_and_the_shared_part_at_the_midpoint(self):
        symbols, nets, roles = self.pair_toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        qa = by_ref["QA"].geometry[result["QA"].orientation]
        qb = by_ref["QB"].geometry[result["QB"].orientation]
        # the first member keeps its orientation, the second is its mirror: bases outward, emitters inward
        self.assertEqual(result["QA"].orientation, FLAT)
        self.assertEqual(result["QB"].orientation, Orientation(0, "y"))
        self.assertEqual(qa.pins["G"].normal, LEFT)
        self.assertEqual(qb.pins["G"].normal, RIGHT)
        self.assertEqual(qa.pins["S"].normal, qb.pins["S"].normal)
        a, b = self.tip(symbols, result, "QA", "S"), self.tip(symbols, result, "QB", "S")
        self.assertAlmostEqual(a[1], b[1])                                  # one straight wire
        self.assertGreaterEqual(b[0] - a[0], 2.54 - 1e-6)
        # the shared part hangs from the middle of that wire, below it
        re = self.tip(symbols, result, "RE", "2")
        self.assertAlmostEqual(re[0], (a[0] + b[0]) / 2)
        self.assertGreaterEqual(re[1], a[1])
        # each collector's load stands above its own collector
        for member, load in (("QA", "RA"), ("QB", "RB")):
            collector, load_tip = self.tip(symbols, result, member, "D"), self.tip(symbols, result, load, "2")
            self.assertAlmostEqual(load_tip[0], collector[0])
            self.assertLess(load_tip[1], collector[1])
        # the chain row is not stepped by the pair: both bases lie on the feeder's row
        self.assertAlmostEqual(self.tip(symbols, result, "F", "2")[1], self.tip(symbols, result, "QA", "G")[1])

    def test_a_pair_does_not_depend_on_input_order(self):
        symbols, nets, roles = self.pair_toy()
        expected = ladder_layout(symbols, nets, roles, text_widths={})
        rng = random.Random(3)
        for _ in range(6):
            shuffled_symbols, shuffled_nets = symbols[:], nets[:]
            rng.shuffle(shuffled_symbols)
            rng.shuffle(shuffled_nets)
            self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}), expected)

    def stack_toy(self):
        flat = horizontal("RL")
        both_ways = PlacedSymbol("RL", flat.position, FLAT, {FLAT: flat.geometry[FLAT], Orientation(180): rotate_geometry(flat.geometry[FLAT], 180)})
        symbols = [horizontal("F0"), switch("Q0"), switch("Q1"), both_ways, vertical("RG", reverse=True)]
        nets = [
            Connection("g0", (("F0", "2"), ("Q0", "G"))),
            Connection("x", (("Q0", "D"), ("Q1", "G"))),
            Connection("v", (("Q1", "D"), ("RL", "1"))),
            Connection("tail", (("RL", "2"), ("RG", "2"))),
            Connection("GND", (("F0", "1"), ("Q0", "S"), ("Q1", "S"), ("RG", "1")), style="power"),
        ]
        roles = Roles({"GND": "ground"}, (), {"feed": ("F0",), "first": ("Q0",), "second": ("Q1",), "load": ("RL",)},
                      {"RG": Attachment("RL", "tail", "GND", "ground")}, {}, {}, (), ())
        return symbols, nets, roles

    def test_a_block_entering_an_up_pointing_pin_stands_above_the_part_and_the_path_stays_on_one_row(self):
        symbols, nets, roles = self.stack_toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        q1_box = extent(by_ref["Q1"], result)
        rl_box = extent(by_ref["RL"], result)
        collector = self.tip(symbols, result, "Q1", "D")
        self.assertEqual(by_ref["Q1"].geometry[result["Q1"].orientation].pins["D"].normal, UP)
        self.assertLessEqual(rl_box[3], q1_box[1])                         # the load is wholly above the part
        self.assertLess(self.tip(symbols, result, "RL", "1")[1], collector[1])
        self.assertLessEqual(max(self.tip(symbols, result, "RL", pin)[0] for pin in ("1", "2")), collector[0] + 1e-6)
        # the path stays on one line: both gates are on the feeder's row, whatever height the first collector leaves at
        row = self.tip(symbols, result, "F0", "2")[1]
        self.assertAlmostEqual(self.tip(symbols, result, "Q0", "G")[1], row)
        self.assertAlmostEqual(self.tip(symbols, result, "Q1", "G")[1], row)
        self.assertLess(result["Q0"].position[0], result["Q1"].position[0])

    def test_a_stacked_block_does_not_depend_on_input_order(self):
        symbols, nets, roles = self.stack_toy()
        expected = ladder_layout(symbols, nets, roles, text_widths={})
        rng = random.Random(9)
        for _ in range(6):
            shuffled_symbols, shuffled_nets = symbols[:], nets[:]
            rng.shuffle(shuffled_symbols)
            rng.shuffle(shuffled_nets)
            self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}), expected)


class LadderFeedbackAndOverTheTopTests(unittest.TestCase):
    """A feedback end joins its net's wire one slot before the part; a net over a part's upper axis pin clears what
    hangs between the parts."""

    def feedback_toy(self, neighbour):
        source = horizontal("R2")
        pins = {"VI": PinGeometry((-5.08, 0.0), LEFT), "VO": PinGeometry((5.08, 0.0), RIGHT),
                "ADJ": PinGeometry((0.0, 5.08), DOWN)}
        if neighbour:
            pins["NI"] = PinGeometry((-5.08, -5.08), LEFT)
        chip = part("U1", pins, (-5.08, -7.62, 5.08, 5.08), body=(-3.81, -6.35, 3.81, 3.81))
        nets = [Connection("inv", (("R2", "2"), ("U1", "VI"), ("FB", "1"))), Connection("out", (("U1", "VO"), ("FB", "2")))]
        roles = Roles({}, (), {"main": ("R2", "U1")}, {}, {}, {"FB": Feedback("inv", "out")}, (), ())
        return [source, chip, horizontal("FB", width=5.08)], nets, roles

    def test_a_feedback_end_joins_the_wire_one_slot_before_the_pin_and_two_with_a_pin_beside_it(self):
        for neighbour, slots in ((False, 1), (True, 3)):
            symbols, nets, roles = self.feedback_toy(neighbour)
            result = ladder_layout(symbols, nets, roles, text_widths={}, gap_mm=12.7)
            by_ref = {s.reference: s for s in symbols}
            tip = lambda ref, pin: result[ref].position[0] + by_ref[ref].geometry[result[ref].orientation].pins[pin].point[0]
            self.assertAlmostEqual(tip("FB", "1"), tip("U1", "VI") - slots * 2.54)
            self.assertGreater(tip("FB", "1"), tip("R2", "2"))
            expected = result
            rng = random.Random(3)
            for _ in range(4):
                shuffled_symbols, shuffled_nets = symbols[:], nets[:]
                rng.shuffle(shuffled_symbols)
                rng.shuffle(shuffled_nets)
                self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}, gap_mm=12.7), expected)


    def over_toy(self):
        """A pair whose second member's collector (an upper axis pin) feeds the next stage's control pin at the row's
        own height; a divider hangs from the second member's outward base, between the two."""
        symbols = [horizontal("F"), switch("QA"), switch("QB"), switch("QZ"), vertical("RE", reverse=True),
                   vertical("RA"), vertical("RB"), vertical("RG", reverse=True), vertical("RH"), horizontal("LOAD"),
                   vertical("RC", reverse=True)]
        nets = [
            Connection("g", (("F", "2"), ("QA", "G"))),
            Connection("e", (("QA", "S"), ("QB", "S"), ("RE", "2"))),
            Connection("ca", (("QA", "D"), ("RA", "2"))),
            Connection("cb", (("QB", "D"), ("RB", "2"), ("RC", "2"), ("QZ", "G"))),
            Connection("gb", (("QB", "G"), ("RG", "2"), ("RH", "2"))),
            Connection("zd", (("QZ", "D"), ("LOAD", "1"))),
            Connection("GND", (("F", "1"), ("RE", "1"), ("RG", "1"), ("QZ", "S"), ("LOAD", "2"), ("RC", "1")), style="power"),
            Connection("VCC", (("RA", "1"), ("RB", "1"), ("RH", "1")), style="power"),
        ]
        roles = Roles(
            {"GND": "ground", "VCC": "supply"}, (), {"pair": ("F", "QA", "QB"), "z": ("QZ", "LOAD")},
            {"RE": Attachment("QA", "e", "GND", "ground"), "RA": Attachment("QA", "ca", "VCC", "supply"),
             "RB": Attachment("QB", "cb", "VCC", "supply"), "RG": Attachment("QB", "gb", "GND", "ground"),
             "RH": Attachment("QB", "gb", "VCC", "supply"), "RC": Attachment("QB", "cb", "GND", "ground")},
            {}, {}, (("QA", "QB"),), (),
        )
        return symbols, nets, roles

    def test_a_net_leaving_an_upper_pin_is_wired_over_what_hangs_between_its_parts(self):
        symbols, nets, roles = self.over_toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        tip = lambda ref, pin: LadderPairAndStackTests.tip(symbols, result, ref, pin)
        # the next stage's control pin is on the row, the collector above it: the net must go up and over the divider
        self.assertGreater(tip("QB", "S")[1], tip("QZ", "G")[1])
        self.assertLess(tip("QB", "D")[1], tip("QZ", "G")[1])
        divider_top = min(tip("RH", "1")[1], tip("RH", "2")[1])
        self.assertLessEqual(tip("RB", "2")[1], divider_top - 2.54 + 1e-6)         # the row clears the divider by a gap
        self.assertAlmostEqual(tip("RC", "2")[1], tip("RB", "2")[1])               # what hangs on the net stands on that row
        self.assertLess(tip("RB", "2")[1], tip("QB", "D")[1] - 2.54)                # above the collector, not beside it
        expected = result
        rng = random.Random(7)
        for _ in range(5):
            shuffled_symbols, shuffled_nets = symbols[:], nets[:]
            rng.shuffle(shuffled_symbols)
            rng.shuffle(shuffled_nets)
            self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}), expected)


if __name__ == "__main__":
    unittest.main()


class LadderConnectorAndTwinTests(unittest.TestCase):
    def test_a_connector_with_every_pin_one_way_stands_at_the_row_end_pins_towards_the_circuit(self):
        up = OrientedGeometry({"1": PinGeometry((-5.08, 0.0), LEFT), "2": PinGeometry((-5.08, 2.54), LEFT)},
                              (-6.35, -2.54, 2.54, 5.08), (-2.54, -1.27, 1.27, 3.81))
        j1 = PlacedSymbol("J1", (0.0, 0.0), FLAT,
                          {Orientation(r): rotate_geometry(up, r, "") for r in (0, 90, 180, 270)})
        nets = [Connection("n", (("J1", "1"), ("R1", "1"))), Connection("x", (("J1", "2"),)), Connection("m", (("R1", "2"),))]
        roles = Roles({}, (), {"in": ("J1", "R1")}, {}, {}, {}, (), ())
        result = ladder_layout([j1, horizontal("R1")], nets, roles, text_widths={})
        geometry = j1.geometry[result["J1"].orientation]
        self.assertEqual(geometry.pins["1"].normal, RIGHT)
        self.assertLess(result["J1"].position[0], result["R1"].position[0])

    def test_two_feedback_parts_on_the_same_nets_stand_side_by_side(self):
        source = horizontal("R2")
        chip = part("U1", {"VI": PinGeometry((-5.08, 0.0), LEFT), "VO": PinGeometry((5.08, 0.0), RIGHT)},
                    (-5.08, -5.08, 5.08, 5.08), body=(-3.81, -3.81, 3.81, 3.81))
        nets = [Connection("inv", (("R2", "2"), ("U1", "VI"), ("FA", "1"), ("FB", "1"))),
                Connection("out", (("U1", "VO"), ("FA", "2"), ("FB", "2")))]
        feedback = {"FA": Feedback("inv", "out"), "FB": Feedback("inv", "out")}
        roles = Roles({}, (), {"main": ("R2", "U1")}, {}, {}, feedback, (), ())
        result = ladder_layout([source, chip, horizontal("FA", width=5.08), horizontal("FB", width=5.08)], nets, roles,
                               text_widths={})
        self.assertNotEqual(result["FA"].position, result["FB"].position)
        self.assertAlmostEqual(result["FA"].position[1], result["FB"].position[1])


@dataclass(frozen=True)
class Lane:
    hub: str
    pin: str
    net: str
    series: tuple
    nets: tuple


@dataclass(frozen=True)
class LaneRoles(Roles):
    lanes: dict = None


def either_way_round(ref, width=3.81):
    """A horizontal two-pin part that may also be turned half a turn."""
    base = horizontal(ref, width=width).geometry[FLAT]
    return PlacedSymbol(ref, (64.0, 50.8), FLAT, {FLAT: base, Orientation(180): rotate_geometry(base, 180)})


def lane_hub(with_empty_pin=False):
    """A controller in a row (J1 -> H -> J2) with a pin each side that carries a lane: L (left), RA and RB (right)."""
    pins = {
        "IN": PinGeometry((-7.62, 0.0), LEFT), "OUT": PinGeometry((7.62, 0.0), RIGHT),
        "L": PinGeometry((-7.62, 5.08), LEFT),
        "RA": PinGeometry((7.62, 5.08), RIGHT), "RB": PinGeometry((7.62, 10.16), RIGHT),
    }
    if with_empty_pin:
        pins["NC"] = PinGeometry((-7.62, 10.16), LEFT)
    return part("H", pins, (-7.62, -3.81, 7.62, 12.7), body=(-6.35, -3.81, 6.35, 12.7))


def lane_circuit(with_empty_pin=False):
    symbols = [
        horizontal("J1", ("A", "SIG")), lane_hub(with_empty_pin), horizontal("J2", ("SIG", "B")),
        either_way_round("R1"), vertical("C1", reverse=True), vertical("CS"),
    ]
    nets = [
        Connection("in", (("J1", "SIG"), ("H", "IN"))),
        Connection("out", (("H", "OUT"), ("J2", "SIG"))),
        Connection("la", (("H", "L"), ("R1", "2"))),
        Connection("m1", (("R1", "1"), ("C1", "2"))),
        Connection("ra", (("H", "RA"), ("CS", "1"))),
        Connection("rb", (("H", "RB"), ("CS", "2"))),
        Connection("GND", (("C1", "1"),), style="power"),
    ]
    roles = LaneRoles(
        {"GND": "ground"}, (), {"main": ("J1", "H", "J2")}, {"C1": Attachment("R1", "m1", "GND", "ground")}, {}, {}, (), (),
        lanes={"H.L": Lane("H", "L", "la", ("R1",), ("la", "m1")), "H.RA": Lane("H", "RA", "ra", ("CS",), ("ra", "rb"))},
    )
    return symbols, nets, roles


class LadderLaneTests(unittest.TestCase):
    def tips(self, symbols, result, ref):
        symbol = next(s for s in symbols if s.reference == ref)
        geometry = symbol.geometry[result[ref].orientation]
        x, y = result[ref].position
        return {pin: (x + g.point[0], y + g.point[1]) for pin, g in geometry.pins.items()}

    def test_a_series_part_runs_along_the_lane_from_the_pin_and_its_shunt_hangs_from_the_lanes_end(self):
        symbols, nets, roles = lane_circuit()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        hub, r1, c1 = (self.tips(symbols, result, ref) for ref in ("H", "R1", "C1"))
        lane_y = hub["L"][1]
        self.assertAlmostEqual(hub["L"][1], hub["IN"][1] + 5.08)                       # the lane keeps the pin pitch
        self.assertAlmostEqual(r1["2"][1], lane_y)                                      # on the pin's height
        self.assertAlmostEqual(r1["1"][1], lane_y)
        self.assertLess(r1["2"][0], hub["L"][0] - 2.54 + 1e-6)                          # away from the chip, a wire's length off
        self.assertLess(r1["1"][0], r1["2"][0])                                         # in the order they connect
        self.assertAlmostEqual(c1["2"][1], lane_y)                                      # the shunt's tip is on the lane
        self.assertLess(c1["2"][0], r1["1"][0] + 1e-6)                                  # beyond the series part
        self.assertGreater(c1["1"][1], lane_y)                                          # toward its rail: down

    def test_a_part_between_two_lanes_stands_between_them_beyond_the_chip(self):
        symbols, nets, roles = lane_circuit()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        hub, cs = self.tips(symbols, result, "H"), self.tips(symbols, result, "CS")
        self.assertAlmostEqual(cs["2"][1], hub["RB"][1])                                # the lane it ends on has the tip
        self.assertAlmostEqual(cs["1"][1], cs["2"][1] - 10.16)                          # the other tip stands off its lane
        self.assertAlmostEqual(cs["1"][0], cs["2"][0])
        self.assertGreater(cs["1"][0], hub["RA"][0] + 2.54 - 1e-6)                      # to the right of the chip's pins

    def test_the_chip_makes_room_for_its_lanes_and_an_empty_pin_takes_none(self):
        symbols, nets, roles = lane_circuit()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        plain = ladder_layout(symbols, nets, replace(roles, lanes={}, attachments={}), text_widths={},)
        left = lambda res, ref: res[ref].position[0]
        self.assertGreater(left(result, "H") - left(result, "J1"), left(plain, "H") - left(plain, "J1") + 7.62)
        self.assertGreater(left(result, "J2") - left(result, "H"), left(plain, "J2") - left(plain, "H"))
        padded, padded_nets, padded_roles = lane_circuit(with_empty_pin=True)
        self.assertEqual(ladder_layout(padded, padded_nets, padded_roles, text_widths={}), result)

    def test_input_order_does_not_change_the_lanes(self):
        symbols, nets, roles = lane_circuit()
        expected = ladder_layout(symbols, nets, roles, text_widths={})
        rng = random.Random(3)
        for _ in range(5):
            shuffled_symbols, shuffled_nets = symbols[:], nets[:]
            rng.shuffle(shuffled_symbols)
            rng.shuffle(shuffled_nets)
            self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}), expected)

    def test_the_recorded_plan_has_the_lanes_their_hangs_and_spans(self):
        from backend.schematic.ladder_plan import plan_and_layout
        symbols, nets, roles = lane_circuit()
        plan, placed = plan_and_layout(symbols, nets, roles, text_widths={})
        self.assertEqual(placed, ladder_layout(symbols, nets, roles, text_widths={}))
        lanes = {row.above: row for row in plan.rows if row.kind == "lane"}
        self.assertEqual(sorted(lanes), ["H.L", "H.RA"])
        self.assertEqual(lanes["H.L"].order, ("R1",))
        self.assertEqual(lanes["H.RA"].order, ("CS",))
        self.assertEqual([(h.reference, h.wire, h.side) for h in plan.hangs], [("C1", "m1", "below")])
        self.assertIn("CS", {span.reference for span in plan.spans})
        self.assertTrue({"R1", "C1", "CS"} <= plan.parts())

    def test_a_lane_on_a_chain_pin_leaves_its_parts_in_a_row_under_everything(self):
        symbols, nets, roles = lane_circuit()
        nets = [Connection("in", (("J1", "SIG"), ("H", "IN"), ("R1", "2"))) if net.net == "in" else net for net in nets]
        nets = [net for net in nets if net.net != "la"]
        roles = replace(roles, lanes={"H.IN": Lane("H", "IN", "in", ("R1",), ("in", "m1"))})
        result = ladder_layout(symbols, nets, roles, text_widths={})
        bottoms = [extent(s, result)[3] for s in symbols if s.reference not in ("R1", "C1", "CS")]
        self.assertGreater(extent(next(s for s in symbols if s.reference == "R1"), result)[1], max(bottoms))


def half_bridge(legs=1, load="attachment", feeders=False):
    """``legs`` pairs of three-pin parts in series between +V and GND (the upper's S over the lower's D, the junction ``j<n>``).

    With one leg the load is a vertical two-pin part from the junction to ground (an attachment); with two it is a
    horizontal two-pin part between the junctions (a bridge)."""
    symbols, nets, chains, attachments = [], [], {}, {}
    rails_vcc, rails_gnd = [], []
    for leg in range(legs):
        high, low = f"QH{leg}", f"QL{leg}"
        symbols += [switch(high), switch(low)]
        chains[f"high{leg}"], chains[f"low{leg}"] = (high,), (low,)
        nets.append(Connection(f"j{leg}", ((high, "S"), (low, "D"))))
        rails_vcc.append((high, "D"))
        rails_gnd.append((low, "S"))
    if legs == 1:
        symbols.append(vertical("RL"))
        nets[0] = Connection("j0", (("QH0", "S"), ("QL0", "D"), ("RL", "1")))
        rails_gnd.append(("RL", "2"))
        attachments["RL"] = Attachment("QH0", "j0", "GND", "ground")
    else:
        symbols.append(horizontal("RL"))
        nets[0] = Connection("j0", (*nets[0].pins, ("RL", "1")))
        nets[1] = Connection("j1", (*nets[1].pins, ("RL", "2")))
        chains["load"] = ("RL",)
    if feeders:
        for leg in range(legs):
            for kind in ("H", "L"):
                ref = f"RG{kind}{leg}"
                symbols.append(either_way_round(ref))
                chains[f"gate{kind}{leg}"] = (ref,)
                nets.append(Connection(f"g{kind}{leg}", ((f"Q{kind}{leg}", "G"), (ref, "1"))))
    nets += [Connection("VCC", tuple(rails_vcc), style="power"), Connection("GND", tuple(rails_gnd), style="power")]
    roles = Roles({"GND": "ground", "VCC": "supply"}, tuple(f"j{leg}" for leg in range(legs)), chains, attachments,
                  {}, {}, (), ())
    return symbols, nets, roles


class LadderStackedPairTests(unittest.TestCase):
    """A net whose pins face each other across rows (one down from the part above, one up from the part below) is a
    junction between a stacked pair: one vertical column, the net a straight wire between the tips."""

    @staticmethod
    def tip(symbols, result, ref, pin):
        geometry = {s.reference: s for s in symbols}[ref].geometry[result[ref].orientation]
        return (result[ref].position[0] + geometry.pins[pin].point[0], result[ref].position[1] + geometry.pins[pin].point[1])

    def test_a_half_bridge_is_one_column_with_the_junction_a_straight_wire_and_the_load_beside_it(self):
        symbols, nets, roles = half_bridge()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        upper, lower = self.tip(symbols, result, "QH0", "S"), self.tip(symbols, result, "QL0", "D")
        self.assertAlmostEqual(upper[0], lower[0])                          # one vertical wire between the tips
        self.assertGreaterEqual(lower[1] - upper[1], 2 * 2.54 - 1e-6)
        self.assertLess(self.tip(symbols, result, "QH0", "D")[1], upper[1])  # the upper part's other through pin is above it
        load = self.tip(symbols, result, "RL", "1")
        self.assertGreater(load[0], upper[0])                               # beside the wire, on the side away from the gates
        self.assertTrue(upper[1] + 2.54 - 1e-6 <= load[1] <= lower[1] - 2.54 + 1e-6)
        self.assertEqual(result["QH0"].orientation, result["QL0"].orientation)

    def test_two_half_bridges_stand_side_by_side_with_the_load_across_their_junctions_and_gates_outward(self):
        symbols, nets, roles = half_bridge(legs=2)
        result = ladder_layout(symbols, nets, roles, text_widths={})
        a, b = self.tip(symbols, result, "QH0", "S"), self.tip(symbols, result, "QH1", "S")
        self.assertAlmostEqual(self.tip(symbols, result, "QL0", "D")[0], a[0])
        self.assertAlmostEqual(self.tip(symbols, result, "QL1", "D")[0], b[0])
        self.assertAlmostEqual(a[1], b[1])
        self.assertLess(a[0], b[0])
        self.assertAlmostEqual(self.tip(symbols, result, "RL", "1")[1], self.tip(symbols, result, "RL", "2")[1])
        self.assertTrue(a[0] < self.tip(symbols, result, "RL", "1")[0] < self.tip(symbols, result, "RL", "2")[0] < b[0])
        by_ref = {s.reference: s for s in symbols}
        gate = lambda ref: by_ref[ref].geometry[result[ref].orientation].pins["G"].normal
        self.assertEqual((gate("QH0"), gate("QL0")), (LEFT, LEFT))
        self.assertEqual((gate("QH1"), gate("QL1")), (RIGHT, RIGHT))

    def test_the_resistor_alone_on_a_gate_stands_beside_it_on_the_outer_side_of_its_column(self):
        symbols, nets, roles = half_bridge(legs=2, feeders=True)
        result = ladder_layout(symbols, nets, roles, text_widths={})
        for leg, outward in ((0, -1), (1, 1)):                        # the first column's gates face left, the second's right
            for kind in "HL":
                gate = self.tip(symbols, result, f"Q{kind}{leg}", "G")
                near, far = self.tip(symbols, result, f"RG{kind}{leg}", "1"), self.tip(symbols, result, f"RG{kind}{leg}", "2")
                self.assertAlmostEqual(near[1], gate[1])               # on the gate's own height
                self.assertAlmostEqual((near[0] - gate[0]) * outward, 2.54)
                self.assertGreater((far[0] - near[0]) * outward, 0)
        # and every part clear of the columns' parts, the load between the columns
        boxes = {ref: extent({s.reference: s for s in symbols}[ref], result) for ref in result}
        for a in boxes:
            for b in boxes:
                if a < b:
                    self.assertFalse(boxes[a][0] < boxes[b][2] and boxes[b][0] < boxes[a][2]
                                     and boxes[a][1] < boxes[b][3] and boxes[b][1] < boxes[a][3], (a, b))

    def test_a_stacked_pair_does_not_depend_on_input_order(self):
        for legs in (1, 2):
            symbols, nets, roles = half_bridge(legs=legs)
            expected = ladder_layout(symbols, nets, roles, text_widths={})
            rng = random.Random(4)
            for _ in range(6):
                shuffled_symbols, shuffled_nets = symbols[:], nets[:]
                rng.shuffle(shuffled_symbols)
                rng.shuffle(shuffled_nets)
                self.assertEqual(ladder_layout(shuffled_symbols, shuffled_nets, roles, text_widths={}), expected)

    def test_two_parts_whose_pins_do_not_face_across_rows_are_not_stacked(self):
        symbols, nets, roles = half_bridge()
        # the lower part turned so that its joined pin faces down too: an ordinary chain, no column
        flipped = [replace(s, geometry={FLAT: s.geometry[Orientation(180)]}) if s.reference == "QL0" else s for s in symbols]
        from backend.schematic.ladder_plan import plan_from_layout
        self.assertEqual(plan_from_layout(flipped, nets, roles, text_widths={}).columns, ())


class LadderStackedPairJunctionTests(unittest.TestCase):
    """The junction of a stacked pair may carry several pins of one part and a hub's pin, and the lower part may be the
    first or last of a longer chain."""

    @staticmethod
    def power_fet(ref):
        """Source pins 1-3 together at the bottom, drain 5 at the top, gate 4 at the left: a five-pin power FET."""
        base = OrientedGeometry({
            "4": PinGeometry((-5.08, 0.0), LEFT), "5": PinGeometry((2.54, -5.08), UP),
            "1": PinGeometry((2.54, 5.08), DOWN), "2": PinGeometry((2.54, 5.08), DOWN), "3": PinGeometry((2.54, 5.08), DOWN),
        }, (-5.08, -5.08, 2.54, 5.08), (-2.54, -3.81, 2.54, 3.81))
        geometry = {Orientation(r, m): rotate_geometry(base, r, m) for r in (0, 90, 180, 270) for m in ("", "y")}
        return PlacedSymbol(ref, (64.0, 50.8), FLAT, geometry)

    def test_three_source_pins_on_the_junction_and_a_hub_pin_there_still_make_a_stack(self):
        hub = part("U1", {str(i): PinGeometry((-7.62 if i < 4 else 7.62, 2.54 * (i % 3)), LEFT if i < 4 else RIGHT) for i in range(1, 7)},
                   (-7.62, -2.54, 7.62, 7.62))
        symbols = [self.power_fet("QH"), self.power_fet("QL"), hub, horizontal("L")]
        nets = [
            Connection("sw", (("QH", "1"), ("QH", "2"), ("QH", "3"), ("QL", "5"), ("U1", "5"), ("L", "1"))),
            Connection("VIN", (("QH", "5"),), style="power"),
            Connection("GND", (("QL", "1"), ("QL", "2"), ("QL", "3")), style="power"),
        ]
        roles = Roles({"GND": "ground", "VIN": "supply"}, ("sw",), {"hi": ("QH", "L"), "lo": ("QL",), "ctl": ("U1",)}, {}, {}, {}, (), ())
        from backend.schematic.ladder_plan import plan_from_layout
        plan = plan_from_layout(symbols, nets, roles, text_widths={})
        self.assertEqual([(c.pin, c.order) for c in plan.columns], [("net:sw", ("QH", "QL"))])
        self.assertIn("L", plan.parts())                      # the rest of the high side's chain goes on without it


class LadderGroupAffinityTests(unittest.TestCase):
    def toy(self):
        symbols = [horizontal(ref) for ref in ("A1", "A2", "A3", "A4", "A5", "B1")]
        nets = [
            Connection("c1", (("A1", "2"), ("A2", "1"))),
            Connection("tap", (("A2", "2"), ("A3", "1"), ("B1", "1"))),
            Connection("c3", (("A3", "2"), ("A4", "1"))),
            Connection("c4", (("A4", "2"), ("A5", "1"))),
        ]
        roles = Roles({}, ("tap",), {"a": ("A1", "A2", "A3", "A4", "A5"), "b": ("B1",)}, {}, {}, {}, (), ())
        return symbols, nets, roles

    def test_a_group_stands_under_the_part_of_the_rows_above_it_shares_a_net_with_not_at_the_left_edge(self):
        symbols, nets, roles = self.toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}

        def tip(ref, pin):
            g = by_ref[ref].geometry[result[ref].orientation].pins[pin]
            return result[ref].position[0] + g.point[0]

        self.assertGreater(result["B1"].position[1], result["A2"].position[1])        # a row of its own, below
        self.assertAlmostEqual(tip("B1", "1"), (tip("A2", "2") + tip("A3", "1")) / 2, delta=2.54)   # under the tap
        self.assertGreater(tip("B1", "1"), tip("A1", "1") + 10)                       # not at the left edge
