"""The ladder check command: counts, legality and plan conformance, and that it runs from a bare checkout of ``backend``."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from backend.schematic import ladder_check, ladder_fixture
from backend.schematic.ladder_plan import plan_and_layout
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Placement, rotate_geometry
from tests.unit import test_ladder_layout as layout_tests

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "ladder"


def toy_case():
    symbols, nets, roles = layout_tests.toy()
    plan, placed = plan_and_layout(symbols, nets, roles, text_widths={})
    return plan, symbols, nets, placed


def moved(placed, **changes):
    """The placements with some parts moved by (dx, dy) or turned to an Orientation."""
    out = dict(placed)
    for ref, change in changes.items():
        place = out[ref]
        if isinstance(change, Orientation):
            out[ref] = Placement(place.position, change)
        else:
            out[ref] = Placement((place.position[0] + change[0], place.position[1] + change[1]), place.orientation)
    return out


def verdict_of(plan, symbols, nets, placements):
    return ladder_check.check(plan, ladder_fixture.apply_placements(symbols, placements), nets, sorted(placements))


class CheckTests(unittest.TestCase):
    def test_the_heuristic_placement_of_the_toy_is_legal_and_follows_its_plan(self):
        plan, symbols, nets, placed = toy_case()
        verdict = verdict_of(plan, symbols, nets, placed)
        self.assertEqual(verdict.legal, [])
        self.assertEqual([item for item in verdict.conformance if "closer than a gap" not in item], [])

    def test_overlapping_bodies_and_coincident_tips_are_not_legal(self):
        plan, symbols, nets, placed = toy_case()
        dx = placed["R1"].position[0] - placed["C3"].position[0]
        dy = placed["R1"].position[1] - placed["C3"].position[1]
        verdict = verdict_of(plan, symbols, nets, moved(placed, C3=(dx, dy)))
        self.assertIn("bodies overlap: C3 and R1", verdict.legal)
        self.assertTrue(any(item.startswith("pin tips coincide: C3.") for item in verdict.legal))

    def test_a_part_the_plan_places_but_the_candidate_does_not_is_named(self):
        plan, symbols, nets, placed = toy_case()
        partial = {ref: place for ref, place in placed.items() if ref != "C2"}
        self.assertIn("C2 is in the plan but not placed", verdict_of(plan, symbols, nets, partial).legal)

    def test_rows_out_of_order_are_not_legal(self):
        plan, symbols, nets, placed = toy_case()
        upside_down = {ref: Placement((p.position[0], -p.position[1]), p.orientation) for ref, p in placed.items()}
        legal = verdict_of(plan, symbols, nets, upside_down).legal
        self.assertTrue(any(item.startswith("rows out of order") for item in legal), legal)

    def test_conformance_names_a_turned_part_a_tip_off_its_wire_and_a_hang_out_of_rank(self):
        plan, symbols, nets, placed = toy_case()
        turned = Orientation(180)
        symbols = [replace(s, geometry={**s.geometry, turned: rotate_geometry(s.geometry[placed["R1"].orientation], 180)})
                   if s.reference == "R1" else s for s in symbols]
        self.assertIn(f"R1 turned {turned.rotation}{turned.mirror} where the plan has "
                      f"{plan.orientations['R1'].rotation}{plan.orientations['R1'].mirror}",
                      verdict_of(plan, symbols, nets, moved(placed, R1=turned)).conformance)
        off = verdict_of(plan, symbols, nets, moved(placed, C3=(0.0, 2.54))).conformance
        self.assertTrue(any(item.startswith("hang C3: tip off the straight drop of") and "U1.VO" in item for item in off), off)
        # R1 and C2 hang from the same pin of U1, nearest first by rank: exchange their places along the wire
        near, far = sorted((h for h in plan.hangs if h.wire == "adj"), key=lambda h: h.rank)
        dx = placed[far.reference].position[0] - placed[near.reference].position[0]
        swapped = moved(placed, **{near.reference: (dx, 0.0), far.reference: (-dx, 0.0)})
        ranked = verdict_of(plan, symbols, nets, swapped).conformance
        self.assertTrue(any(item.startswith(f"rank: {near.reference} (rank 0) lies farther") for item in ranked), ranked)

    def test_a_hang_on_a_free_trunk_is_reached_by_a_straight_drop_from_its_host_pin(self):
        plan, symbols, nets, placed = toy_case()
        # adj becomes an unnamed wire: a trunk through the hangs' tips, dropped to from U1.ADJ (pin normal down)
        plan = replace(plan, rows=[row for row in plan.rows if row.name != "adj"])
        drop = placed["C2"].position[1] - placed["U1"].position[1]
        self.assertGreater(drop, 5.0)       # the hangs' tips are 5.08 mm under the host pin: a free trunk
        verdict = verdict_of(plan, symbols, nets, placed)
        self.assertEqual([item for item in verdict.conformance if "adj" in item], [])
        # a hang off the trunk of its sibling: the two no longer share one
        off = verdict_of(plan, symbols, nets, moved(placed, C2=(0.0, 2.54))).conformance
        self.assertTrue(any("hangs on adj do not share one trunk" in item for item in off), off)
        # a hang above the host pin cannot be reached by a drop along the pin's normal (down)
        above = verdict_of(plan, symbols, nets, moved(placed, C2=(0.0, -drop - 2.54), R1=(0.0, -drop - 2.54))).conformance
        self.assertTrue(any(item.startswith("hang C2: tip off the straight drop of U1.ADJ") for item in above), above)

    def test_a_hang_beyond_the_ends_of_a_wire_between_chain_pins_is_reported(self):
        plan, symbols, nets, placed = toy_case()
        outside = verdict_of(plan, symbols, nets, moved(placed, C3=(200.0, 0.0))).conformance
        self.assertTrue(any(item.startswith("hang C3: outside the span of its wire out") for item in outside), outside)

    def test_a_chain_that_runs_backwards_is_reported(self):
        plan, symbols, nets, placed = toy_case()
        dx = placed["U1"].position[0] - placed["J1"].position[0]
        swapped = moved(placed, J1=(dx + 40.0, 0.0))
        self.assertTrue(any(item.startswith("chain order: ") for item in verdict_of(plan, symbols, nets, swapped).conformance))


class CountsTests(unittest.TestCase):
    def test_counts_are_the_placement_models_and_the_others_stand_in_a_row_below(self):
        plan, symbols, nets, placed = toy_case()
        posed = ladder_fixture.apply_placements(symbols, placed)
        counts = ladder_check.counts("toy", posed, nets, {}, None)
        self.assertEqual((counts.through, counts.overlaps, counts.crossings), (0, 0, 0))
        self.assertGreater(counts.wire_mm, 0)
        row = ladder_check.others_row(posed, ["RTERM"], gap=5.08, nets=nets)
        boxes = {s.reference: s for s in row}
        lowest = max(boxes[s.reference].position[1] + boxes[s.reference].geometry[boxes[s.reference].orientation].box[3]
                     for s in posed if s.reference != "RTERM")
        placed_term = boxes["RTERM"]
        top = placed_term.position[1] + placed_term.geometry[placed_term.orientation].box[1]
        self.assertAlmostEqual(top, lowest + 3 * 5.08)
        self.assertEqual(ladder_check.others_row(posed, [], nets=nets), posed)


class BareCheckoutTests(unittest.TestCase):
    """The command works on a copy of ``backend`` alone: no ``scripts``, no sandbox, no recordings, no repository path."""

    def run_in_copy(self, arguments, extra_files=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REPO / "backend", root / "backend", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(FIXTURES / "buck.json", root / "buck.json")
            for name, text in extra_files:
                (root / name).write_text(text, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "SBX_WORK_DIR")}
            env.update(PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
            return subprocess.run([sys.executable, *arguments], cwd=root, env=env, capture_output=True, text=True, timeout=180)

    def test_it_judges_a_placement_file_from_a_bare_copy_of_backend(self):
        fixture = ladder_fixture.load_fixture(FIXTURES / "buck.json")
        placement = json.dumps({"placements": ladder_fixture.placements_to_json(fixture.baseline)})
        done = self.run_in_copy(["-m", "backend.schematic.ladder_check", "buck.json", "mine.json", "--plan"],
                                [("mine.json", placement)])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("fixture buck", done.stdout)
        self.assertIn("legality: OK", done.stdout)
        self.assertIn("candidate", done.stdout)
        self.assertIn("baseline", done.stdout)
        self.assertIn("row ", done.stdout.split("plan:")[1])

    def test_a_placement_that_is_not_legal_exits_with_status_1_and_says_why(self):
        fixture = ladder_fixture.load_fixture(FIXTURES / "buck.json")
        piled = {ref: Placement((0.0, 0.0), place.orientation) for ref, place in fixture.baseline.items()}
        done = self.run_in_copy(["-m", "backend.schematic.ladder_check", "buck.json", "piled.json", "--json"],
                                [("piled.json", json.dumps(ladder_fixture.placements_to_json(piled)))])
        self.assertEqual(done.returncode, 1, done.stderr)
        report = json.loads(done.stdout)
        self.assertFalse(report["legal"])
        self.assertTrue(any(item.startswith("bodies overlap") for item in report["problems"]))
        self.assertEqual(report["baseline"]["wire_mm"], round(report["baseline"]["wire_mm"], 2))

    def test_importing_the_check_pulls_in_nothing_from_the_sandbox(self):
        done = self.run_in_copy(["-c", "import sys, backend.schematic.ladder_check; "
                                       "print(sorted(m for m in sys.modules if m.split('.')[0] in ('sbx', 'scripts', 'tests')))"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
