"""A connector at the end of a row has its pins toward the circuit: an output connector's point left (an audio jack pair)."""
from __future__ import annotations

import unittest

from backend.schematic.ladder_layout import ladder_layout
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Connection, OrientedGeometry, PinGeometry, PlacedSymbol, rotate_geometry
from tests.unit import test_ladder_layout as layout_tests

LEFT, RIGHT = layout_tests.LEFT, layout_tests.RIGHT


def jack(ref: str) -> PlacedSymbol:
    """A three-pin connector with every pin on its right: T (the chain pin) above, R (shared) and S (ground) below."""
    base = OrientedGeometry({"T": PinGeometry((5.08, -2.54), RIGHT), "R": PinGeometry((5.08, 0.0), RIGHT),
                             "S": PinGeometry((5.08, 2.54), RIGHT)}, (-5.08, -5.08, 6.35, 5.08), (-5.08, -3.81, 5.08, 3.81))
    return PlacedSymbol(ref, (64.0, 50.8), layout_tests.FLAT, {Orientation(r, m): rotate_geometry(base, r, m)
                                                              for r in (0, 90, 180, 270) for m in ("", "x", "y")})


class PassThroughTests(unittest.TestCase):
    def toy(self):
        symbols = [jack("J1"), layout_tests.horizontal("A"), layout_tests.horizontal("B"), jack("J2")]
        nets = [
            Connection("sig_in", (("J1", "T"), ("A", "1"))),
            Connection("sig_out", (("B", "2"), ("J2", "T"))),
            Connection("bus", (("J1", "R"), ("J2", "R"))),
            Connection("GND", (("J1", "S"), ("J2", "S")), style="power"),
        ]
        roles = layout_tests.Roles({"GND": "ground"}, (), {"in": ("J1", "A"), "out": ("B", "J2")}, {}, {}, {}, (("J1", "J2"),), ())
        return symbols, nets, roles

    def test_the_output_connector_at_the_end_of_its_row_faces_the_circuit_with_the_input_ones_pin_order(self):
        symbols, nets, roles = self.toy()
        result = ladder_layout(symbols, nets, roles, text_widths={})
        by_ref = {s.reference: s for s in symbols}
        face = lambda ref, pin: by_ref[ref].geometry[result[ref].orientation].pins[pin].normal
        tip_y = lambda ref, pin: result[ref].position[1] + by_ref[ref].geometry[result[ref].orientation].pins[pin].point[1]
        self.assertEqual(face("J1", "T"), RIGHT)
        self.assertEqual(face("J2", "T"), LEFT)                                      # not rotation 0: its pins would point away
        self.assertLess(result["B"].position[0], result["J2"].position[0])
        self.assertLess(tip_y("J2", "T"), tip_y("J2", "R"))                          # ground below the chain pin, as on the input one


if __name__ == "__main__":
    unittest.main()
