"""The committed placements the CI workflow builds (``tests/fixtures/direct``) fit the circuits they are for."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.schematic_direct_smoke import load_placement
from scripts.schematic_validation_fixtures import build_fixture

DIRECT = Path(__file__).resolve().parents[1] / "fixtures" / "direct"


class DirectFixtureTests(unittest.TestCase):
    def test_every_expectation_has_a_placement_and_the_other_way_round(self) -> None:
        expected = json.loads((DIRECT / "expected.json").read_text(encoding="utf-8"))
        folders = {path.parent.name for path in DIRECT.glob("*/placement.json")}
        self.assertEqual(set(expected), folders)
        for values in expected.values():
            self.assertEqual(set(values), {"wire_length_mm", "corners", "crossings"})

    def test_a_placement_places_every_part_of_its_circuit(self) -> None:
        for fixture in sorted(json.loads((DIRECT / "expected.json").read_text(encoding="utf-8"))):
            with self.subTest(fixture=fixture):
                placed = set(load_placement(DIRECT / fixture / "placement.json"))
                parts = {s.reference for s in build_fixture(fixture).symbols if not s.authoring_auxiliary}
                self.assertEqual(parts - placed, set())
                self.assertEqual({r for r in placed if r.startswith("#")}, set())


if __name__ == "__main__":
    unittest.main()
