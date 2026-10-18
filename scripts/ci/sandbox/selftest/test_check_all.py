"""scripts/ci/check_all.py: what counts as a regression (the builds themselves are not run here)."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

spec = importlib.util.spec_from_file_location("check_all", REPO / "scripts" / "ci" / "check_all.py")
check_all = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_all)


def built(sheet, **values):
    return {"sheet": sheet, "built": True, "netlist": "PASS", "throughs": 0, "shorts": 0, "crossings": 0, "wire": 100.0, **values}


WAS = {"a": {"built": True, "netlist": "PASS", "throughs": 0, "shorts": 0, "crossings": 2, "wire_mm": 100.0},
       "b": {"built": False, "known_failure": "routing: a wire through TP5"}}


class CompareTests(unittest.TestCase):
    def test_a_sheet_that_is_as_good_is_no_problem(self):
        problems, notes = check_all.compare([built("a", crossings=2), {"sheet": "b", "built": False, "failure": "x"}], WAS, counts=True)
        self.assertEqual((problems, notes), ([], []))

    def test_a_sheet_that_stopped_building_or_got_worse_is_listed(self):
        problems, _ = check_all.compare([{"sheet": "a", "built": False, "failure": "boom"}], WAS, counts=True)
        self.assertEqual(problems, ["a: stopped building: boom"])
        for key, label in (("throughs", "through"), ("shorts", "short"), ("crossings", "crossing")):
            problems, _ = check_all.compare([built("a", **{key: 3})], WAS, counts=True)
            self.assertEqual(len(problems), 1)
            self.assertIn(label, problems[0])
        problems, _ = check_all.compare([{**built("a", crossings=2), "netlist": "FAIL"}], WAS, counts=True)
        self.assertEqual(problems, ["a: netlist FAIL"])

    def test_a_known_failure_that_builds_now_is_a_note_not_a_problem(self):
        problems, notes = check_all.compare([built("b")], WAS, counts=True)
        self.assertEqual(problems, [])
        self.assertIn("builds now", notes[0])

    def test_one_seed_does_not_compare_counts(self):
        problems, _ = check_all.compare([built("a", crossings=9)], WAS, counts=False)
        self.assertEqual(problems, [])

    def test_the_record_keeps_what_the_comparison_needs(self):
        self.assertEqual(check_all.record({"sheet": "b", "built": False, "failure": "x"}), {"built": False, "known_failure": "x"})
        self.assertEqual(check_all.record(built("a", corners=3, field_collisions=0))["crossings"], 0)

    def test_the_sheets_are_the_four_demos_and_the_ten_corpus_circuits(self):
        self.assertEqual(len(check_all.SHEETS), 14)
        self.assertEqual(check_all.DEMO_PICKS, {"demo_buck": 7, "demo_lm317": 5, "demo_opamp": 11, "demo_ampac": 5})


if __name__ == "__main__":
    unittest.main()
