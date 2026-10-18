"""The solver's disk cache: a hit is the fresh answer, and any input of the answer changes the key."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import ladder, placement_cache  # noqa: E402
from backend.schematic import ladder_fixture, ladder_solve  # noqa: E402
from backend.schematic.orientation import Orientation  # noqa: E402

FIXTURE = ladder_fixture.load_fixture(REPO / "tests" / "fixtures" / "ladder" / "lm317.json")


def key(**changes):
    given = dict(plan=FIXTURE.plan, symbols=FIXTURE.symbols, nets=FIXTURE.nets, gap_mm=2.54, pitch_mm=7.62, hint=None,
                 seeds=(7, 11), budget=20.0)
    given.update(changes)
    return placement_cache.key(given.pop("plan"), given.pop("symbols"), given.pop("nets"), **given)


class KeyTests(unittest.TestCase):
    def test_the_same_inputs_have_the_same_key(self):
        self.assertEqual(key(), key())

    def test_each_input_of_the_answer_changes_the_key(self):
        base = key()
        first = FIXTURE.symbols[0]
        turned = next(o for o in first.geometry if o != first.orientation)
        changes = {
            "plan": replace(FIXTURE.plan, rows=FIXTURE.plan.rows[:-1]) if len(FIXTURE.plan.rows) > 1 else replace(FIXTURE.plan, sequence=()),
            "symbols": [replace(first, orientation=turned), *FIXTURE.symbols[1:]],
            "nets": [*FIXTURE.nets[1:]],
            "gap_mm": 5.08,
            "pitch_mm": 5.08,
            "seeds": (7,),
            "budget": 40.0,
            "hint": {first.reference: SimpleNamespace(position=(0.0, 0.0), orientation=Orientation(0))},
        }
        for name, value in changes.items():
            with self.subTest(name):
                self.assertNotEqual(key(**{name: value}), base)

    def test_the_source_of_the_solver_or_the_plan_changes_the_key(self):
        base = key()
        with tempfile.TemporaryDirectory() as folder:
            copies = []
            for name in placement_cache.SOURCES:
                copy = Path(folder) / Path(name).name
                copy.write_bytes((REPO / name).read_bytes())
                copies.append(copy)
            with mock.patch.object(placement_cache.env, "REPO_ROOT", Path(folder)), \
                    mock.patch.object(placement_cache, "SOURCES", tuple(c.name for c in copies)):
                same = key()
                copies[0].write_bytes(copies[0].read_bytes() + b"\n# changed\n")
                self.assertNotEqual(key(), same)
        self.assertNotEqual(base, "")


class RoundTripTests(unittest.TestCase):
    def test_a_stored_answer_reads_back_to_the_same_placements_wires_and_status(self):
        with mock.patch.object(ladder_solve, "_SOLVER_DETERMINISTIC_TIME", 2.0):
            fresh = ladder_solve.legalise(FIXTURE.plan, FIXTURE.symbols, FIXTURE.nets, seeds=(7,))
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(placement_cache, "directory", lambda: Path(folder)):
            placement_cache.store("abc", fresh, fresh.status)
            back = placement_cache.load("abc")
            self.assertEqual(dict(back), dict(fresh))
            self.assertEqual(back.wires, fresh.wires)
            self.assertEqual(back.status, fresh.status)
            same = lambda r: json.dumps(placement_cache.encode(r, r.status), sort_keys=True, default=str)
            self.assertEqual(same(back), same(fresh))
            self.assertIsNone(placement_cache.load("missing"))


class PlacementsPathTests(unittest.TestCase):
    def solve(self, folder, **environment):
        roles = SimpleNamespace(other=())
        base = {ladder.SOLVER_ENV: "cpsat", ladder.SEEDS_ENV: "7", "KICAD_AI_LADDER_CACHE": "1"}
        base.update(environment)
        with mock.patch.dict(os.environ, base), mock.patch.object(placement_cache, "directory", lambda: Path(folder)), \
                mock.patch("backend.schematic.ladder_plan.plan_and_layout", return_value=(FIXTURE.plan, {})), \
                mock.patch.object(ladder, "text_widths", lambda _intent: {}), \
                mock.patch.object(ladder_solve, "_SOLVER_DETERMINISTIC_TIME", 2.0):
            return ladder._placements(FIXTURE.symbols, FIXTURE.nets, None, roles, "lm317")

    def test_a_hit_returns_the_fresh_placements_and_says_so_and_the_switch_and_the_inputs_matter(self):
        with tempfile.TemporaryDirectory() as folder:
            fresh, note, wires = self.solve(folder)
            self.assertNotIn("[cached]", note)
            hit, note, hit_wires = self.solve(folder)
            self.assertIn("[cached]", note)
            self.assertEqual(json.dumps(placement_cache.encode(hit, "x")["placements"], sort_keys=True),
                             json.dumps(placement_cache.encode(fresh, "x")["placements"], sort_keys=True))
            self.assertEqual(hit_wires, wires)
            self.assertNotIn("[cached]", self.solve(folder, **{ladder.SEEDS_ENV: "11"})[1])       # another seed list: a miss
            self.assertNotIn("[cached]", self.solve(folder, KICAD_AI_LADDER_CACHE="0")[1])         # switched off: solved
            self.assertEqual(len(list(Path(folder).glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
