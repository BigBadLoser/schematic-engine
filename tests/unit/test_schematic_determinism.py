"""A sheet's items come out in the same order, and fields are planned the same way, whatever order KiCad stored them in."""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from backend.kicad import mcp_extended_server as server
from backend.schematic.field_placement import plan_fields
from backend.schematic.kicad_geometry import measured_scene_from_kicad
from backend.schematic.presentation_geometry import GeometryScene

HEAD = r"""(kicad_sch
  (version 20260306)
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (rectangle (start -1 -1) (end 1 1))
        (pin passive line (at -3 0 0) (length 2) (name "~") (number "1"))
      )
    )
  )
"""


def block(ref, x, y):
    return (
        f'  (symbol (lib_id "Device:R") (at {x} {y} 0) (unit 1) '
        f'(property "Reference" "{ref}" (at {x} {y - 4} 0) (effects (font (size 1.27 1.27)))) '
        f'(property "Value" "10k" (at {x} {y + 4} 0) (effects (font (size 1.27 1.27)))))\n'
    )


BLOCKS = [block(f"R{i}", 30 + 12 * (i % 3), 40 + 15 * (i // 3)) for i in range(1, 7)] + [
    f"  (wire (pts (xy {10 + i} 40) (xy {27 + i} 40)) (uuid \"w{i}\"))\n" for i in range(5)
] + [
    f"  (junction (at {10 + i} 40) (diameter 0))\n" for i in range(3)
] + [
    f'  (label "N{i}" (at {10 + i} 41 0) (effects (font (size 1.27 1.27))))\n' for i in range(3)
]


def scene_of(blocks, directory):
    path = Path(directory) / "s.kicad_sch"
    path.write_text(HEAD + "".join(blocks) + ")", encoding="utf-8")
    return measured_scene_from_kicad(path)[0]


class DeterminismTests(unittest.TestCase):
    def permutations(self):
        for seed in range(5):
            blocks = list(BLOCKS)
            random.Random(seed).shuffle(blocks)
            yield blocks

    def test_measured_scene_order_ignores_document_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = scene_of(BLOCKS, tmp).items
            for blocks in self.permutations():
                self.assertEqual(scene_of(blocks, tmp).items, base)

    def test_plan_fields_ignores_scene_item_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = list(scene_of(BLOCKS, tmp).items)
        base = plan_fields(GeometryScene(tuple(items)))
        for seed in range(5):
            shuffled = list(items)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(plan_fields(GeometryScene(tuple(shuffled))), base)

    def test_symbol_lookup_ignores_unit_order(self):
        a = {"reference": "U1", "x": 5.0, "y": 1.0}
        b = {"reference": "U1", "x": 2.0, "y": 9.0}
        self.assertIs(server._symbol_by_reference({"symbols": [a, b]}, "U1"), b)
        self.assertIs(server._symbol_by_reference({"symbols": [b, a]}, "U1"), b)


if __name__ == "__main__":
    unittest.main()
