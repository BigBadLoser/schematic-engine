"""What a reader notices in a sheet: text with a wire through it, parts far from what they serve, crossings, jogs."""
from __future__ import annotations

import unittest

from backend.schematic.presentation_geometry import Bounds, GeometryItem, GeometryKind as K, GeometryScene
from backend.schematic.readability import is_ground_name, measure_readability, rotated_label_owners


def wire(name, a, b):
    return GeometryItem(
        name, K.WIRE, name,
        Bounds(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), segment=(a, b),
    )


def pin(token, x, y, net=None):
    return GeometryItem(f"{token}:terminal", K.PIN_ENDPOINT, token.split(".")[0], Bounds(x, y, x, y), net=net)


def body(ref, left, top, right, bottom, library="Device:R"):
    return GeometryItem(f"{ref}:graphic", K.BODY, ref, Bounds(left, top, right, bottom), text=library)


def field(ref, kind, x, y, text="10k", angle=0.0, w=3.0, h=1.27):
    return GeometryItem(
        f"{ref}:field:{kind.value}", kind, ref, Bounds(x - w / 2, y - h / 2, x + w / 2, y + h / 2),
        anchor=(x, y), text=text, angle=angle,
    )


def label(name, text, x, y, angle=0.0):
    return GeometryItem(
        name, K.NET_LABEL, name, Bounds(x - 2, y - 1.0, x + 2, y + 0.2), net=text, anchor=(x, y), text=text, angle=angle,
    )


class CompositionTests(unittest.TestCase):
    """How straight the wires run: a person draws a straight wire where two pins can line up."""

    def two_pins(self, *wires):
        return GeometryScene.from_items((
            pin("U1.1", 0, 0), pin("R1.1", 20, 0), pin("R2.1", 20, 10), *wires,
        ))

    def test_a_pin_to_pin_wire_in_one_run_is_straight_and_has_no_corner(self):
        scene = GeometryScene.from_items((pin("U1.1", 0, 0), pin("R1.1", 20, 0), wire("w", (0, 0), (20, 0))))
        m = measure_readability(scene).measures
        self.assertEqual((m["connections"], m["connection_corners"], m["two_pin_wires"], m["straight_wires"]), (1, 0, 1, 1))

    def test_a_wire_with_a_jog_has_two_corners_and_is_not_straight(self):
        scene = GeometryScene.from_items((
            pin("U1.1", 0, 0), pin("R1.1", 20, 5),
            wire("a", (0, 0), (10, 0)), wire("b", (10, 0), (10, 5)), wire("c", (10, 5), (20, 5)),
        ))
        m = measure_readability(scene).measures
        self.assertEqual((m["connections"], m["connection_corners"], m["two_pin_wires"], m["straight_wires"]), (1, 2, 1, 0))

    def test_a_net_of_three_pins_is_two_connections(self):
        scene = GeometryScene.from_items((
            pin("U1.1", 0, 0), pin("R1.1", 20, 0), pin("R2.1", 10, 10),
            wire("a", (0, 0), (20, 0)), wire("b", (10, 0), (10, 10)),
        ))
        m = measure_readability(scene).measures
        self.assertEqual((m["connections"], m["two_pin_wires"]), (2, 0))

    def test_a_stub_to_a_label_is_not_a_connection(self):
        scene = GeometryScene.from_items((pin("U1.1", 0, 0), wire("w", (0, 0), (5, 0)), label("l", "SDA", 5, 0)))
        m = measure_readability(scene).measures
        self.assertEqual((m["connections"], m["two_pin_wires"]), (0, 0))


class CollisionTests(unittest.TestCase):
    def test_a_reference_with_a_wire_through_it_is_counted(self):
        scene = GeometryScene.from_items((
            wire("w", (0, 0), (0, 20)),
            field("R1", K.REFERENCE, 0, 10, "R1"),
        ))
        report = measure_readability(scene)
        self.assertEqual(report.measures["text_on_wire"], 1)
        self.assertEqual(report.measures["field_on_wire"], 1)
        self.assertEqual(report.of("text_on_wire")[0].ids, ("R1:field:reference", "w"))

    def test_text_beside_a_wire_is_not(self):
        scene = GeometryScene.from_items((wire("w", (0, 0), (0, 20)), field("R1", K.REFERENCE, 4, 10, "R1")))
        self.assertEqual(measure_readability(scene).measures["text_on_wire"], 0)

    def test_a_label_is_not_on_the_wire_it_hangs_from(self):
        scene = GeometryScene.from_items((wire("w", (0, 5), (10, 5)), label("l", "SDA", 10, 5)))
        self.assertEqual(measure_readability(scene).measures["text_on_wire"], 0)

    def test_a_vertical_label_is_an_issue_and_a_vertical_field_is_only_counted(self):
        scene = GeometryScene.from_items((
            label("l", "UART_RX", 4, 4, angle=90.0), label("m", "SDA", 20, 4),
            field("R1", K.VALUE, 40, 4, "10k", angle=90.0),
        ))
        report = measure_readability(scene)
        self.assertEqual(report.measures["rotated_text"], 2)
        self.assertEqual(report.measures["rotated_labels"], 1)
        self.assertEqual([issue.ids for issue in report.of("rotated_label")], [("l",)])
        self.assertEqual(report.measures["labels"], 2)

    def test_a_vertical_label_names_the_symbol_whose_stub_it_stands_on(self):
        scene = GeometryScene.from_items((
            wire("stub", (0, 0), (0, 5)), pin("U1.1", 0, 0), label("l", "UART_TX", 0, 5, angle=90.0),
            wire("stub2", (10, 0), (15, 0)), pin("J3.2", 10, 0), label("m", "SDA", 15, 0),
        ))
        self.assertEqual(rotated_label_owners(scene), frozenset({"U1"}))
        self.assertEqual(rotated_label_owners(GeometryScene.from_items((wire("w", (0, 0), (5, 0)), label("m", "SDA", 5, 0)))), frozenset())

    def test_a_label_with_no_stub_to_a_pin_names_nobody(self):
        scene = GeometryScene.from_items((label("l", "UART_TX", 0, 5, angle=90.0),))
        self.assertEqual(rotated_label_owners(scene), frozenset())

    def test_a_wire_across_the_inside_of_a_body_is_counted_but_a_pin_tip_just_inside_its_box_is_not(self):
        # a connector whose pins are on its right: a wire to the far tip that runs through the body from the left
        through = GeometryScene.from_items((
            body("J1", 10, 0, 12.54, 8, "Connector_Generic:Conn_01x03"), wire("w", (0, 4), (16.5, 4)),
        ))
        report = measure_readability(through)
        self.assertEqual(report.measures["wires_through_body"], 1)
        self.assertEqual([issue.ids for issue in report.of("wire_through_body")], [("w", "J1:graphic")])
        # an LED's pin tip lies 0.76 mm inside the box of its graphic: the wire leaving it is not through anything
        leaving = GeometryScene.from_items((
            body("D1", 0, 0, 3.5, 5.84, "Device:LED"), wire("w", (1.75, 5.08), (1.75, 12)),
        ))
        self.assertEqual(measure_readability(leaving).measures["wires_through_body"], 0)
        beside = GeometryScene.from_items((body("R1", 0, 0, 2, 5), wire("w", (5, -3), (5, 9))))
        self.assertEqual(measure_readability(beside).measures["wires_through_body"], 0)

    def test_crossings_are_counted_and_located(self):
        scene = GeometryScene.from_items((wire("a", (0, 5), (10, 5)), wire("b", (5, 0), (5, 10))))
        report = measure_readability(scene)
        self.assertEqual(report.measures["crossings"], 1)
        self.assertEqual(report.of("crossing")[0].where.left < 5 < report.of("crossing")[0].where.right, True)


def circuit(distance, *, library="Device:C", ref="C1"):
    """A four-pin part with a two-pin part wired to its pin through a net, ``distance`` mm away."""
    items = [
        body("U1", 0, 0, 10, 10, "MCU:Chip"),
        pin("U1.1", -2.54, 2.54), pin("U1.2", -2.54, 5.08), pin("U1.3", -2.54, 7.62), pin("U1.4", 12.54, 2.54),
        body(ref, -distance - 5, 2, -distance - 3, 6, library),
        pin(f"{ref}.1", -distance - 4, 0), pin(f"{ref}.2", -distance - 4, 8),
        pin("#PWR1.1", -distance - 4, 12, net="GND"),
        wire("sig", (-2.54, 2.54), (-distance - 4, 2.54)), wire("sig2", (-distance - 4, 2.54), (-distance - 4, 0)),
        wire("gnd", (-distance - 4, 8), (-distance - 4, 12)),
    ]
    return GeometryScene.from_items(tuple(items))


NETS = {"SIG": ["U1.1", "C1.1"], "GND": ["C1.2"]}


class SupportTests(unittest.TestCase):
    def test_a_support_part_near_its_host_is_not_flagged(self):
        report = measure_readability(circuit(8), nets=NETS, rail_nets=["GND"], ground_nets=["GND"])
        self.assertEqual(report.measures["support_parts"], 1)
        self.assertEqual(report.measures["support_far"], 0)

    def test_one_far_from_its_host_is_flagged_with_the_distance(self):
        report = measure_readability(circuit(60), nets=NETS, rail_nets=["GND"], ground_nets=["GND"])
        self.assertEqual(report.measures["support_far"], 1)
        issue = report.of("support_far")[0]
        self.assertEqual(issue.ids, ("C1", "U1.1"))
        self.assertGreater(issue.magnitude, 60)

    def test_the_net_can_be_read_from_the_wires_and_power_names(self):
        report = measure_readability(circuit(60))
        self.assertEqual(report.measures["support_parts"], 1)
        self.assertEqual(report.measures["support_far"], 1)

    def test_a_connector_is_not_a_support_part(self):
        report = measure_readability(circuit(60, library="Connector_Generic:Conn_01x02"), nets=NETS,
                                     rail_nets=["GND"], ground_nets=["GND"])
        self.assertEqual(report.measures["support_parts"], 0)

    def test_a_source_is_not_a_support_part_but_the_same_geometry_as_a_capacitor_is(self):
        nets = {"SIG": ["U1.1", "V1.1"], "GND": ["V1.2"]}
        source = measure_readability(circuit(60, ref="V1"), nets=nets, rail_nets=["SIG", "GND"], ground_nets=["GND"])
        self.assertEqual(source.measures["support_far"], 0)
        self.assertNotIn("V1", [part.owner for part in source.support_parts])
        cap = measure_readability(circuit(60, ref="C1"), nets=NETS, rail_nets=["GND"], ground_nets=["GND"])
        self.assertEqual(cap.measures["support_far"], 1)

    def test_the_threshold_is_a_parameter(self):
        report = measure_readability(circuit(20), nets=NETS, rail_nets=["GND"], ground_nets=["GND"], support_far_mm=10)
        self.assertEqual(report.measures["support_far"], 1)

    def test_distance_beyond_comfortable_adds_up_even_when_it_is_not_a_failure(self):
        near = measure_readability(circuit(8), nets=NETS, rail_nets=["GND"], ground_nets=["GND"])
        middling = measure_readability(circuit(30), nets=NETS, rail_nets=["GND"], ground_nets=["GND"])
        self.assertEqual(near.measures["support_excess_mm"], 0.0)
        self.assertEqual(middling.measures["support_far"], 0)
        self.assertGreater(middling.measures["support_excess_mm"], 10.0)


class WiringTests(unittest.TestCase):
    def test_a_small_step_in_a_wire_is_a_jog(self):
        scene = GeometryScene.from_items((
            wire("a", (0, 0), (10, 0)), wire("b", (10, 0), (10, 1.27)), wire("c", (10, 1.27), (20, 1.27)),
        ))
        self.assertEqual(measure_readability(scene).measures["jogs"], 1)

    def test_a_step_of_a_pin_spacing_or_more_is_not(self):
        scene = GeometryScene.from_items((
            wire("a", (0, 0), (10, 0)), wire("b", (10, 0), (10, 5.08)), wire("c", (10, 5.08), (20, 5.08)),
        ))
        self.assertEqual(measure_readability(scene).measures["jogs"], 0)

    def test_a_connection_that_goes_round_is_a_detour(self):
        points = [(0, 0), (0, 30), (30, 30), (30, 0)]            # joins two pins 30 mm apart with 90 mm of wire
        scene = GeometryScene.from_items((
            pin("A.1", 0, 0), pin("B.1", 30, 0),
            *(wire(f"w{i}", a, b) for i, (a, b) in enumerate(zip(points, points[1:]))),
        ))
        report = measure_readability(scene)
        self.assertEqual(report.measures["detours"], 1)
        self.assertAlmostEqual(report.of("detour")[0].magnitude, 60.0)

    def test_a_label_hung_far_from_its_pin_is_reported(self):
        scene = GeometryScene.from_items((
            pin("U1.1", 0, 0), wire("w", (0, 0), (30, 0)), label("l", "SDA", 30, 0),
        ))
        report = measure_readability(scene)
        self.assertEqual(report.measures["label_far"], 1)
        self.assertEqual(report.measures["label_stub_max_mm"], 30.0)


class NameTests(unittest.TestCase):
    def test_ground_names(self):
        for name in ("GND", "AGND", "DGND", "VSS", "0V", "/GND"):
            self.assertTrue(is_ground_name(name), name)
        for name in ("+3V3", "VCC", "SDA", "GNDREF_X1"):
            self.assertEqual(is_ground_name(name), name == "GNDREF_X1", name)


if __name__ == "__main__":
    unittest.main()
