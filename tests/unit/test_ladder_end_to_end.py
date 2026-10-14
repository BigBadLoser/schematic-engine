"""Roles, plan, heuristic layout, legality: whole pipelines on the buck and lm317 fixtures."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from backend.schematic import ladder_check, ladder_fixture
from backend.schematic.ladder_plan import plan_and_layout
from backend.schematic.ladder_roles import Attachment, Feedback, Lane, Roles, Rung

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ladder"


def roles_of(data) -> Roles:
    """The ``Roles`` a fixture's JSON describes."""
    return Roles(
        data["rails"], tuple(data["trunks"]), {k: tuple(v) for k, v in data["chains"].items()},
        {k: Attachment(**v) for k, v in data["attachments"].items()}, {k: Rung(**v) for k, v in data["rungs"].items()},
        {k: Feedback(**v) for k, v in data["feedback"].items()}, tuple(tuple(p) for p in data["pairs"]), tuple(data["other"]),
        {k: Lane(**{**v, "series": tuple(v["series"]), "nets": tuple(v["nets"])}) for k, v in data.get("lanes", {}).items()})


class EndToEndTests(unittest.TestCase):
    def test_roles_to_plan_to_heuristic_layout_is_legal_with_nothing_through_or_short(self):
        for name in ("buck", "lm317"):
            with self.subTest(name):
                fixture = ladder_fixture.load_fixture(FIXTURES / f"{name}.json")
                plan, placed = plan_and_layout(fixture.symbols, fixture.nets, roles_of(fixture.roles),
                                               text_widths=fixture.text_widths, gap_mm=fixture.gap_mm,
                                               slot_pitch_mm=fixture.slot_pitch_mm)
                self.assertEqual(plan, fixture.plan)
                result = ladder_check.judge(replace(fixture, plan=plan), placed)
                self.assertEqual(result["verdict"].legal, [])
                self.assertEqual(result["candidate"].through, 0)
                self.assertEqual(result["candidate"].short, 0)

    def test_the_legaliser_solves_the_buck_plan_legally_with_no_more_wire_than_the_heuristic(self):
        try:
            import ortools  # noqa: F401
        except ImportError:
            self.skipTest("ortools is not installed")
        from backend.schematic.ladder_solve import legalise
        fixture = ladder_fixture.load_fixture(FIXTURES / "buck.json")
        placed = legalise(fixture.plan, fixture.symbols, fixture.nets, gap_mm=fixture.gap_mm, pitch_mm=fixture.slot_pitch_mm)
        result = ladder_check.judge(fixture, dict(placed))
        self.assertEqual(result["verdict"].legal, [])
        self.assertLessEqual(result["candidate"].wire_mm, result["baseline"].wire_mm + 1e-6)


if __name__ == "__main__":
    unittest.main()
