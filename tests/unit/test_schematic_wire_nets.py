"""Which wires are one conductor, and where different conductors cross."""
from __future__ import annotations

import unittest

from backend.schematic.presentation_geometry import Bounds, GeometryItem, GeometryKind as K, GeometryScene
from backend.schematic.wire_nets import wire_crossings, wire_network


def wire(name, a, b):
    return GeometryItem(
        name, K.WIRE, name,
        Bounds(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), segment=(a, b),
    )


def pin(token, x, y, net=None):
    return GeometryItem(f"{token}:terminal", K.PIN_ENDPOINT, token.split(".")[0], Bounds(x, y, x, y), net=net)


def label(name, text, x, y):
    return GeometryItem(
        name, K.NET_LABEL, name, Bounds(x - 2, y - 1, x + 2, y), net=text, anchor=(x, y), text=text,
    )


class ConductorTests(unittest.TestCase):
    def test_wires_that_touch_end_to_end_are_one_conductor(self):
        scene = GeometryScene.from_items((wire("a", (0, 0), (10, 0)), wire("b", (10, 0), (10, 10))))
        network = wire_network(scene)
        self.assertEqual(len(network.components), 1)
        self.assertEqual(network.components[0].length_mm, 20.0)

    def test_a_wire_ending_on_the_length_of_another_joins_it(self):
        scene = GeometryScene.from_items((wire("a", (0, 0), (10, 0)), wire("b", (5, 0), (5, 8))))
        self.assertEqual(len(wire_network(scene).components), 1)

    def test_wires_that_only_cross_stay_separate(self):
        scene = GeometryScene.from_items((wire("a", (0, 5), (10, 5)), wire("b", (5, 0), (5, 10))))
        network = wire_network(scene)
        self.assertEqual(len(network.components), 2)
        self.assertEqual(len(wire_crossings(scene, network)), 1)

    def test_a_t_is_not_a_crossing(self):
        scene = GeometryScene.from_items((wire("a", (0, 5), (10, 5)), wire("b", (5, 5), (5, 10))))
        self.assertEqual(wire_crossings(scene, wire_network(scene)), [])

    def test_pins_labels_and_power_names_belong_to_the_conductor_they_touch(self):
        scene = GeometryScene.from_items((
            wire("a", (0, 0), (10, 0)),
            pin("R1.1", 0, 0), pin("U1.2", 10, 0), pin("#PWR1.1", 0, 0, net="+3V3"),
            label("l", "SDA", 5, 0),
        ))
        network = wire_network(scene)
        component = network.components[0]
        self.assertEqual(sorted(component.pins), ["R1.1", "U1.2"])
        self.assertEqual(component.power, ("+3V3",))
        self.assertEqual(component.labels, ("SDA",))
        self.assertEqual(network.by_pin["R1.1"], network.by_pin["U1.2"])

    def test_a_pin_on_the_middle_of_a_wire_is_not_connected_but_a_label_there_is(self):
        # KiCad joins a pin to the end of a wire (ERC: "pin not connected" otherwise); a label joins anywhere along one
        scene = GeometryScene.from_items((
            wire("a", (0, 0), (10, 0)), pin("R1.1", 5, 0), label("l", "SDA", 5, 0),
        ))
        network = wire_network(scene)
        self.assertNotIn("R1.1", network.by_pin)
        self.assertEqual(network.components[0].labels, ("SDA",))

    def test_two_pins_at_the_same_point_are_connected_without_a_wire(self):
        scene = GeometryScene.from_items((pin("R1.2", 4, 4), pin("C1.1", 4, 4)))
        network = wire_network(scene)
        self.assertEqual(network.by_pin["R1.2"], network.by_pin["C1.1"])

    def test_an_unwired_pin_has_no_conductor(self):
        scene = GeometryScene.from_items((pin("R1.1", 0, 0),))
        self.assertNotIn("R1.1", wire_network(scene).by_pin)


if __name__ == "__main__":
    unittest.main()
