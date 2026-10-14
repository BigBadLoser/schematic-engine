"""The ladder fixtures under tests/fixtures/ladder: loadable with the repository alone, consistent with their plans."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from backend.schematic import ladder_check, ladder_fixture
from backend.schematic.ladder_plan import Plan, plan_and_layout
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Placement
from tests.unit import test_ladder_layout as layout_tests

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ladder"
DEMOS = ("lm317", "buck", "opamp", "amp")
CORPUS = ("bitaxe_power", "bms_supply", "gamma_preamp", "gamma_sipm", "piaa_power", "pico_hbridge", "pmod_hv",
          "tally_power", "tally_timecode", "uno_r4_power")
LIMIT_BYTES = 300 * 1024


class FixtureFileTests(unittest.TestCase):
    def test_all_fourteen_fixtures_are_there_and_small(self):
        for name in (*DEMOS, *CORPUS):
            path = FIXTURES / f"{name}.json"
            with self.subTest(name):
                self.assertTrue(path.is_file(), path)
                self.assertLess(path.stat().st_size, LIMIT_BYTES)

    def test_each_loads_into_objects_without_the_sandbox(self):
        for name in (*DEMOS, *CORPUS):
            with self.subTest(name):
                plan, symbols, nets, baseline = ladder_fixture.load(FIXTURES / f"{name}.json")
                refs = {s.reference for s in symbols}
                self.assertIsInstance(plan, Plan)
                self.assertTrue(plan.parts() <= refs, sorted(plan.parts() - refs))
                self.assertTrue(set(baseline) <= plan.parts())
                self.assertTrue({ref for net in nets for ref, _pin in net.pins} <= refs)
                for ref, place in baseline.items():
                    symbol = next(s for s in symbols if s.reference == ref)
                    self.assertIn(place.orientation, symbol.geometry)
                    self.assertEqual(plan.orientations[ref], place.orientation)

    def test_the_plan_in_a_fixture_round_trips(self):
        for name in (*DEMOS, *CORPUS):
            with self.subTest(name):
                plan = ladder_fixture.load_fixture(FIXTURES / f"{name}.json").plan
                self.assertEqual(Plan.from_json(plan.to_json()), plan)

    def test_every_planned_part_has_a_place_in_the_baseline(self):
        # A part the heuristic finds no slot for is left out of the plan (it is in no structure), so a baseline never lacks a
        # part the plan has. (tally_timecode's R21 once had no slot; with its test points as taps it has.)
        for name in (*DEMOS, *CORPUS):
            fixture = ladder_fixture.load_fixture(FIXTURES / f"{name}.json")
            with self.subTest(name):
                self.assertEqual(fixture.unplaced, ())

    def test_the_baseline_of_every_complete_fixture_is_legal(self):
        for name in (*DEMOS, *CORPUS):
            fixture = ladder_fixture.load_fixture(FIXTURES / f"{name}.json")
            if fixture.unplaced:
                continue
            with self.subTest(name):
                verdict = ladder_check.check(fixture.plan, ladder_check.posed(fixture, fixture.baseline), fixture.nets,
                                             sorted(fixture.baseline))
                self.assertEqual(verdict.legal, [])


class CheckCommandTests(unittest.TestCase):
    def test_the_baseline_judged_against_itself_matches_the_harness_numbers(self):
        fixture = ladder_fixture.load_fixture(FIXTURES / "lm317.json")
        result = ladder_check.judge(fixture, dict(fixture.baseline), estimate=True)
        candidate, baseline = result["candidate"], result["baseline"]
        self.assertEqual((candidate.through, candidate.overlaps, candidate.crossings, candidate.short), (0, 0, 0, 0))
        self.assertAlmostEqual(candidate.wire_mm, baseline.wire_mm)
        self.assertAlmostEqual(baseline.wire_mm, 224.8, places=1)           # what `sandbox.py ladder --circuit lm317` prints
        self.assertEqual(result["verdict"].legal, [])

    def test_the_plans_own_wires_are_scored_unless_the_estimate_is_asked_for(self):
        fixture = ladder_fixture.load_fixture(FIXTURES / "lm317.json")
        planned = ladder_check.judge(fixture, dict(fixture.baseline))["baseline"]
        estimated = ladder_check.judge(fixture, dict(fixture.baseline), estimate=True)["baseline"]
        self.assertAlmostEqual(planned.wire_mm, 232.4, places=1)      # the trunks are drawn at the hang tips, with drops
        self.assertAlmostEqual(estimated.wire_mm, 224.8, places=1)    # the estimate found a shorter tree for one net
        self.assertEqual((planned.through, planned.short), (estimated.through, estimated.short))

    def test_a_placement_file_is_judged_and_a_pile_of_parts_is_not_legal(self):
        fixture = ladder_fixture.load_fixture(FIXTURES / "buck.json")
        piled = {ref: Placement((0.0, 0.0), place.orientation) for ref, place in fixture.baseline.items()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "piled.json"
            path.write_text(json.dumps(ladder_fixture.placements_to_json(piled)), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(ladder_check.main([str(FIXTURES / "buck.json"), str(path)]), 1)
            self.assertIn("bodies overlap", printed.getvalue())
            self.assertIn("legality:", printed.getvalue())
            good = Path(directory) / "baseline.json"
            good.write_text(json.dumps({"placements": ladder_fixture.placements_to_json(fixture.baseline)}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(ladder_check.main([str(FIXTURES / "buck.json"), str(good)]), 0)
            self.assertIn("legality: OK", printed.getvalue())
        result = ladder_check.judge(fixture, piled)
        self.assertTrue(any(item.startswith("bodies overlap") for item in result["verdict"].legal))
        self.assertTrue(any(item.startswith("pin tips coincide") for item in result["verdict"].legal))

    def test_a_missing_part_and_an_unknown_orientation_are_named(self):
        fixture = ladder_fixture.load_fixture(FIXTURES / "buck.json")
        partial = dict(fixture.baseline)
        gone = sorted(partial)[0]
        del partial[gone]
        verdict = ladder_check.judge(fixture, partial)["verdict"]
        self.assertIn(f"{gone} is in the plan but not placed", verdict.legal)
        two_pin = next(s.reference for s in fixture.symbols if len(s.geometry) == 4)       # no mirrored orientations
        with self.assertRaisesRegex(ValueError, "no orientation"):
            ladder_check.judge(fixture, {**fixture.baseline, two_pin: Placement((0.0, 0.0), Orientation(0, "x"))})

    def test_rows_out_of_order_are_legality_failures(self):
        fixture = ladder_fixture.load_fixture(FIXTURES / "lm317.json")
        rows = [row for row in fixture.plan.rows if row.kind == "chain"]
        self.assertTrue(rows)
        # turn the sheet upside down: every row's y changes sign, so the rows come in the opposite order
        flipped = {ref: Placement((place.position[0], -place.position[1]), place.orientation)
                   for ref, place in fixture.baseline.items()}
        verdict = ladder_check.judge(fixture, flipped)["verdict"]
        self.assertTrue(any(item.startswith("rows out of order") for item in verdict.legal))


class FixtureBuildTests(unittest.TestCase):
    def test_a_document_rebuilds_the_same_objects(self):
        symbols, nets, roles = layout_tests.toy()
        plan, placed = plan_and_layout(symbols, nets, roles, text_widths={})
        document = ladder_fixture.build_document("toy", plan, symbols, nets, placed, roles=roles, roles_summary="chain main: J1 U1 RLOAD",
                                                 reach={("U1", "VO"): 4.445}, text_widths={"U1": 5.0})
        again = ladder_fixture.fixture_from_document(json.loads(ladder_fixture.dumps(document)))
        self.assertEqual(again.plan, plan)
        self.assertEqual(again.baseline, placed)
        self.assertEqual(again.nets, nets)
        self.assertEqual(again.reach, {("U1", "VO"): 4.445})
        self.assertEqual(again.roles["chains"], {"main": ["J1", "U1", "RLOAD"]})
        self.assertEqual(again.roles["summary"], "chain main: J1 U1 RLOAD")
        for original, rebuilt in zip(symbols, again.symbols):
            self.assertEqual(original.reference, rebuilt.reference)
            self.assertEqual(dict(original.geometry), dict(rebuilt.geometry))
        self.assertEqual(again.unplaced, ())

    def test_a_placement_file_accepts_the_bare_mapping_and_the_keyed_one(self):
        placements = {"U1": Placement((1.27, 2.54), Orientation(90, "y"))}
        data = ladder_fixture.placements_to_json(placements)
        self.assertEqual(ladder_fixture.placements_from_json(data), placements)
        self.assertEqual(ladder_fixture.placements_from_json({"placements": data}), placements)


if __name__ == "__main__":
    unittest.main()
