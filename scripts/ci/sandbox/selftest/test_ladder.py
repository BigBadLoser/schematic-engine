"""The ladder harness: the pure parts (no recorded run, no MCP Pro, no KiCad, no Chrome)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import ladder  # noqa: E402
from backend.schematic.intent import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent  # noqa: E402
from backend.schematic.orientation import Orientation  # noqa: E402
from backend.schematic.placement_refinement import (  # noqa: E402
    Connection, OrientedGeometry, PinGeometry, Placement, PlacedSymbol,
)

LEFT, RIGHT = (-1.0, 0.0), (1.0, 0.0)


def two_pin() -> OrientedGeometry:
    pins = {"1": PinGeometry((-3.81, 0.0), LEFT), "2": PinGeometry((3.81, 0.0), RIGHT)}
    return OrientedGeometry(pins, (-3.81, -1.0, 3.81, 1.0))


def three_pin() -> OrientedGeometry:
    pins = {"1": PinGeometry((-5.08, 0.0), LEFT), "2": PinGeometry((5.08, 0.0), RIGHT), "3": PinGeometry((0.0, 5.08), (0.0, 1.0))}
    return OrientedGeometry(pins, (-5.08, -5.08, 5.08, 5.08))


def symbols() -> list[PlacedSymbol]:
    upright = Orientation(0)
    return [
        PlacedSymbol("V1", (0.0, 0.0), upright, {upright: two_pin()}, movable=True),
        PlacedSymbol("U1", (30.48, 0.0), upright, {upright: three_pin()}, movable=True),
        PlacedSymbol("R1", (60.96, 5.08), upright, {upright: two_pin()}, movable=True),
        PlacedSymbol("#PWR01", (0.0, 20.0), upright, {upright: two_pin()}),
    ]


def circuit() -> dict:
    nets = [Connection("A", (("V1", "2"), ("U1", "1")), "wire"), Connection("B", (("U1", "2"), ("R1", "1")), "wire")]
    return {"refine": {"solver": "refine", "args": (symbols(), nets), "kwargs": {}}}


def intent(references=("V1", "U1", "R1")) -> SchematicIntent:
    items = tuple(SymbolIntent(r, "Device", "R", "10k") for r in references)
    return SchematicIntent(items, (NetIntent("A", (PinEndpoint(references[0], "2"), PinEndpoint(references[1], "1"))),))


class FindTests(unittest.TestCase):
    def test_a_circuit_matches_by_its_references_and_the_count_is_reported(self):
        recorded = [circuit(), circuit(), {"refine": {"args": ([symbols()[0]], []), "kwargs": {}}}]
        with mock.patch.object(ladder.placement_replay, "load_circuits", return_value=recorded), \
                mock.patch.object(ladder, "build_fixture", return_value=intent()):
            found, count, used, _d = ladder.find_circuit(Path("run"), "demo_x", "last")
            self.assertIs(found, recorded[1])
            self.assertEqual((count, used), (2, 1))
            self.assertIs(ladder.find_circuit(Path("run"), "demo_x", 0)[0], recorded[0])

    def test_no_match_names_the_fixture(self):
        with mock.patch.object(ladder.placement_replay, "load_circuits", return_value=[circuit()]), \
                mock.patch.object(ladder, "build_fixture", return_value=intent(("V1", "Q9"))):
            with self.assertRaisesRegex(LookupError, "demo_x"):
                ladder.find_circuit(Path("run"), "demo_x")

    def test_the_shipped_pick_is_the_match_nearest_the_sheet(self):
        near, far = circuit(), circuit()
        far["refine"]["args"] = ([replace(s, position=(s.position[0] + 10, s.position[1])) for s in symbols()], [])
        anchors = {s.reference: s.position for s in symbols()}
        with mock.patch.object(ladder, "engine_pose", side_effect=lambda c: list(c["refine"]["args"][0])):
            self.assertEqual(ladder.pick_circuit([far, near], anchors), (1, 0.0))
            self.assertAlmostEqual(ladder.pick_circuit([far], anchors)[1], 30.0)   # three shared references, 10 mm each

    def test_either_form_of_a_circuit_name_is_accepted(self):
        self.assertEqual(ladder.fixture_name("buck"), "demo_buck")
        self.assertEqual(ladder.fixture_name("demo_buck"), "demo_buck")


class PoseTests(unittest.TestCase):
    def test_every_orientation_is_offered_from_one_recorded(self):
        turned = Orientation(270)    # recorded turned 270: the upright left pin (-3.81, 0) lies at (0, -3.81)
        pins = {"1": PinGeometry((0.0, -3.81), (0.0, -1.0)), "2": PinGeometry((0.0, 3.81), (0.0, 1.0))}
        recorded = PlacedSymbol("R9", (0.0, 0.0), turned, {turned: OrientedGeometry(pins, (-1.0, -3.81, 1.0, 3.81))}, movable=True)
        flag = symbols()[3]
        wide = ladder.all_orientations([recorded, flag])
        self.assertEqual(wide[1], flag)
        self.assertEqual(set(wide[0].geometry), {Orientation(r) for r in (0, 90, 180, 270)})
        pin = lambda r: wide[0].geometry[Orientation(r)].pins["1"].point
        # a quarter turn takes (x, y) to (y, -x): 0 (-3.81, 0), 90 (0, 3.81), 180 (3.81, 0), 270 (0, -3.81)
        self.assertEqual((pin(0), pin(90), pin(180), pin(270)), ((-3.81, 0.0), (0.0, 3.81), (3.81, 0.0), (0.0, -3.81)))
        self.assertEqual(wide[0].geometry[turned].pins["1"].normal, (0.0, -1.0))

    def test_a_mirrored_three_pin_part_gets_all_twelve(self):
        mirrored = Orientation(90, "x")
        recorded = PlacedSymbol("U9", (0.0, 0.0), mirrored, {mirrored: ladder.solvers.rotate_geometry(three_pin(), 90, "x")}, movable=True)
        wide = ladder.all_orientations([recorded])[0]
        self.assertEqual(len(wide.geometry), 12)
        self.assertEqual(wide.geometry[Orientation(0)].pins["3"].point, (0.0, 5.08))
        self.assertEqual(wide.geometry[mirrored], recorded.geometry[mirrored])

    def test_a_failing_layout_leaves_the_other_circuit_and_names_the_error(self):
        roles = SimpleNamespace(chains={}, attachments={}, rungs={}, feedback={}, pairs=(), other=())
        calls = []
        def layout(syms, nets, found, *, text_widths):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("No available orientation for L101")
            return {}
        injected = {
            "backend.schematic.ladder_roles": SimpleNamespace(classify=lambda _i, _c: roles),
            "backend.schematic.ladder_layout": SimpleNamespace(ladder_layout=layout),
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, injected),                 mock.patch.object(ladder, "find_circuit", return_value=(circuit(), 1, 0, None)),                 mock.patch.object(ladder, "build_fixture", return_value=intent()),                 mock.patch.object(ladder, "png_writer", return_value="none"), mock.patch("builtins.print"):
            summary = ladder.run(Path("run"), ["buck", "amp"], engine=True, out_dir=Path(directory))
            self.assertTrue((Path(directory) / "buck_engine.svg").exists())
            self.assertFalse((Path(directory) / "buck_ladder.svg").exists())
            self.assertTrue((Path(directory) / "amp_ladder.svg").exists())
            self.assertEqual((Path(directory) / "summary.md").read_text(encoding="utf-8"), summary)
        self.assertIn("| ladder: error: buck: No available orientation for L101 |", summary)
        self.assertIn("chain", summary.split("### amp")[0] + "chain")     # the failed circuit still shows its roles
        self.assertIn("| ladder |", summary.split("### amp")[1])

    def test_the_pool_is_used_only_with_a_main_file(self):
        with mock.patch.object(sys, "argv", ["-"]):
            self.assertFalse(ladder._has_main_file())
        with mock.patch.object(sys, "argv", [str(Path(ladder.__file__))]):
            self.assertTrue(ladder._has_main_file())

    def test_pin_counts_come_from_the_geometry(self):
        self.assertEqual(ladder.pin_counts(symbols()), {"V1": 2, "U1": 3, "R1": 2, "#PWR01": 2})

    def test_without_the_ladder_modules_the_engine_placement_stands_in(self):
        absent = {"backend.schematic.ladder_roles": None, "backend.schematic.ladder_layout": None}
        with mock.patch.dict(sys.modules, absent):
            result = ladder.ladder_pose(symbols(), circuit()["refine"]["args"][1], intent())
        self.assertIsNone(result.roles)
        self.assertEqual(result.note, ladder.STAND_IN)
        self.assertEqual(result.symbols, symbols())
        self.assertIn("(stand-in)", ladder.roles_summary(result.roles))

    def test_placements_are_applied_and_other_symbols_stay(self):
        roles = SimpleNamespace(chains={"main": ("V1", "U1")}, attachments={}, rungs={}, feedback={}, pairs=(), other=("R1",))
        fake_roles = SimpleNamespace(classify=lambda _intent, counts: roles if counts["U1"] == 3 else None)
        fake_layout = SimpleNamespace(
            ladder_layout=lambda syms, nets, found, *, text_widths: {"U1": Placement((12.7, 2.54), Orientation(0, "y"))},
        )
        injected = {"backend.schematic.ladder_roles": fake_roles, "backend.schematic.ladder_layout": fake_layout}
        with mock.patch.dict(sys.modules, injected):
            result = ladder.ladder_pose(symbols(), [], intent())
        moved = {s.reference: s for s in result.symbols}
        self.assertEqual(result.note, "ladder")
        self.assertIsNone(result.wires)         # the heuristic placer draws no wires: the plan's stand
        self.assertEqual((moved["U1"].position, moved["U1"].orientation), ((12.7, 2.54), Orientation(0, "y")))
        self.assertEqual(moved["R1"].position, (60.96, 5.08))
        self.assertIn("chain main: V1 -> U1", ladder.roles_summary(result.roles))

    def test_a_part_the_layout_leaves_out_joins_the_other_parts_and_a_placed_unit_stays_where_it_was_put(self):
        roles = SimpleNamespace(chains={"main": ("V1", "U1")}, attachments={}, rungs={}, feedback={}, pairs=(), other=("R1",))
        fake_roles = SimpleNamespace(classify=lambda _intent, counts: roles)
        placed = {"U1": Placement((12.7, 2.54), Orientation(0, "")), "V1": Placement((0.0, 0.0), Orientation(0, ""))}
        fake_layout = SimpleNamespace(ladder_layout=lambda syms, nets, found, *, text_widths: placed)
        with mock.patch.dict(sys.modules, {"backend.schematic.ladder_roles": fake_roles, "backend.schematic.ladder_layout": fake_layout}):
            result = ladder.ladder_pose(symbols(), [], intent())
        self.assertEqual(set(result.roles.other), {"R1"})                # R1 was already other; nothing else was left out
        leftover = SimpleNamespace(chains={"main": ("V1", "U1")}, attachments={}, rungs={}, feedback={}, pairs=(), other=())
        fake_roles = SimpleNamespace(classify=lambda _intent, counts: leftover)
        with mock.patch.dict(sys.modules, {"backend.schematic.ladder_roles": fake_roles, "backend.schematic.ladder_layout": fake_layout}):
            result = ladder.ladder_pose(symbols(), [], intent())
        self.assertEqual(set(result.roles.other), {"R1"})                # the part the layout did not place

    def test_the_units_a_symbol_list_holds_expand_the_intent(self):
        from backend.schematic.intent import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
        parts = symbols()
        unit_symbols = [replace(s, reference="U1:1") if s.reference == "U1" else s for s in parts]
        unit_symbols.append(replace(next(s for s in parts if s.reference == "U1"), reference="U1:2"))
        pins = ladder.unit_pins_of(unit_symbols)
        self.assertEqual(sorted(pins["U1"]), [1, 2])
        whole = SchematicIntent(symbols=(SymbolIntent("U1", "Device", "R", "u"),), nets=(NetIntent("n", (PinEndpoint("U1", "1"),)),))
        self.assertEqual(ladder.expand_for(parts, whole), whole)         # no unit placeable in the symbols: unchanged

    def test_text_widths_are_the_wider_of_reference_and_value(self):
        widths = ladder.text_widths(intent())
        self.assertEqual(sorted(widths), ["R1", "U1", "V1"])
        self.assertTrue(all(width > 0 for width in widths.values()))


class ReportTests(unittest.TestCase):
    def test_counts_have_wire_and_an_extent(self):
        counts = ladder.describe("engine", symbols(), circuit())
        self.assertGreater(counts.wire_mm, 0)
        self.assertGreater(counts.extent[0], 0)
        self.assertIn("| engine |", ladder.table("x", [counts]))

    def test_the_picture_names_every_symbol(self):
        drawing = ladder.picture(symbols(), circuit()["refine"]["args"][1], circuit(), "test")
        self.assertTrue(drawing.startswith("<svg"))
        for reference in ("V1", "U1", "R1"):
            self.assertIn(reference, drawing)
        self.assertIn("<section>", ladder.both_page(drawing, drawing))

    def test_a_picture_without_a_png_writer_is_still_returned(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ladder, "png_writer", return_value="none"):
            self.assertFalse(ladder.to_png("<svg/>", Path(directory) / "x.png"))


class PositionSourceTests(unittest.TestCase):
    """KICAD_AI_LADDER_PLACEMENT and KICAD_AI_LADDER_SOLVER: where the ladder's positions come from."""

    def setUp(self):
        self.roles = SimpleNamespace(chains={"main": ("V1", "U1")}, attachments={}, rungs={}, feedback={}, pairs=(), other=("R1",))

        def never(syms, nets, found, *, text_widths):
            raise AssertionError("the heuristic placer must not run when the environment names another source")

        self.injected = {
            "backend.schematic.ladder_roles": SimpleNamespace(classify=lambda _intent, _counts: self.roles),
            "backend.schematic.ladder_layout": SimpleNamespace(ladder_layout=never),
        }
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def environment(self, **values):
        """The environment with neither switch set, then ``values`` (``PLACEMENT``/``SOLVER``)."""
        cleaned = {k: v for k, v in os.environ.items() if k not in (ladder.PLACEMENT_ENV, ladder.SOLVER_ENV)}
        cleaned.update({ladder.PLACEMENT_ENV if k == "PLACEMENT" else ladder.SOLVER_ENV: v for k, v in values.items()})
        return mock.patch.dict(os.environ, cleaned, clear=True)

    def write(self, name, placements):
        path = self.folder / name
        path.write_text(json.dumps({"placements": placements}), encoding="utf-8")
        return path

    U1 = {"x": 12.7, "y": 2.54, "rotation": 0, "mirror": "y"}

    def test_a_placement_file_stands_in_for_the_heuristic_placer(self):
        path = self.write("mine.json", {"U1": self.U1})
        with mock.patch.dict(sys.modules, self.injected), self.environment(PLACEMENT=str(path)):
            result = ladder.ladder_pose(symbols(), [], intent(), "demo_buck")
        moved = {s.reference: s for s in result.symbols}
        self.assertEqual(result.note, "placement from mine.json")
        self.assertEqual((moved["U1"].position, moved["U1"].orientation), ((12.7, 2.54), Orientation(0, "y")))
        self.assertEqual(moved["R1"].position, (60.96, 5.08))                       # not placed: stays where it was

    def test_without_the_switch_the_heuristic_placer_runs_and_the_note_is_ladder(self):
        calls = []
        layout = SimpleNamespace(ladder_layout=lambda syms, nets, found, *, text_widths: calls.append(1) or {})
        with mock.patch.dict(sys.modules, {**self.injected, "backend.schematic.ladder_layout": layout}), self.environment():
            result = ladder.ladder_pose(symbols(), [], intent())
        self.assertEqual((result.note, calls), ("ladder", [1]))

    def test_a_folder_is_searched_for_the_circuit_by_name_with_or_without_its_prefix(self):
        self.write("buck.json", {"U1": self.U1})
        self.write("corpus_other.json", {})
        with self.environment(PLACEMENT=str(self.folder)):
            self.assertEqual(ladder.placement_file("demo_buck"), self.folder / "buck.json")
            self.assertEqual(ladder.placement_file("buck"), self.folder / "buck.json")
            self.assertEqual(ladder.placement_file("corpus_other"), self.folder / "corpus_other.json")
            with self.assertRaisesRegex(FileNotFoundError, "no amp.json"):
                ladder.placement_file("amp")
            with self.assertRaisesRegex(FileNotFoundError, "no <circuit>.json"):
                ladder.placement_file("")
        with self.environment():
            self.assertIsNone(ladder.placement_file("buck"))
        with self.environment(PLACEMENT=str(self.folder / "absent.json")), self.assertRaisesRegex(FileNotFoundError, "no such file"):
            ladder.placement_file("buck")

    def test_a_placement_naming_a_part_or_an_orientation_the_circuit_lacks_is_refused(self):
        stranger = self.write("stranger.json", {"Z9": self.U1})
        flipped = self.write("flipped.json", {"R1": {"x": 0, "y": 0, "rotation": 0, "mirror": "x"}})       # two-pin parts have no mirror
        with mock.patch.dict(sys.modules, self.injected):
            with self.environment(PLACEMENT=str(stranger)), self.assertRaisesRegex(ValueError, "Z9"):
                ladder.ladder_pose(symbols(), [], intent(), "x")
            with self.environment(PLACEMENT=str(flipped)), self.assertRaisesRegex(ValueError, "R1 has no orientation 0x"):
                ladder.ladder_pose(symbols(), [], intent(), "x")

    def test_both_switches_at_once_are_refused(self):
        path = self.write("mine.json", {})
        with mock.patch.dict(sys.modules, self.injected), self.environment(PLACEMENT=str(path), SOLVER="cpsat"), \
                self.assertRaisesRegex(ValueError, "both set"):
            ladder.ladder_pose(symbols(), [], intent())

    def test_the_cpsat_switch_runs_the_plan_through_the_solver_and_reports_its_status(self):
        plan, seen = object(), []

        def solve(given_plan, widened, nets, hint=None, seeds=None):
            seen.append((given_plan, [s.reference for s in widened]))
            return SimpleNamespace(placements={"U1": Placement((5.08, 7.62), Orientation(90))}, status="OPTIMAL 0.1 s")

        injected = {**self.injected, "backend.schematic.ladder_solve": SimpleNamespace(legalise_with_status=solve)}
        with mock.patch.dict(sys.modules, injected), self.environment(SOLVER="cpsat"), \
                mock.patch("backend.schematic.ladder_plan.plan_and_layout", return_value=(plan, {})) as derive:
            result = ladder.ladder_pose(symbols(), [], intent())
        moved = {s.reference: s for s in result.symbols}
        self.assertEqual(result.note, "solver cpsat: OPTIMAL 0.1 s")
        self.assertEqual((moved["U1"].position, moved["U1"].orientation), ((5.08, 7.62), Orientation(90)))
        self.assertIs(seen[0][0], plan)
        self.assertEqual(seen[0][1], ["V1", "U1", "R1", "#PWR01"])
        self.assertEqual(derive.call_args.kwargs["text_widths"], ladder.text_widths(intent()))
        # a hung part the heuristic finds no slot for stays in the plan for the solver to place
        self.assertTrue(derive.call_args.kwargs["partial"])

    def test_the_heuristic_layout_of_the_plan_is_the_solvers_hint_only_when_asked(self):
        layout, seen = {"U1": Placement((12.7, 5.08), Orientation(0))}, []

        def solve(given_plan, widened, nets, hint=None, seeds=None):
            seen.append(hint)
            return SimpleNamespace(placements={"U1": Placement((5.08, 7.62), Orientation(90))}, status="OPTIMAL 0.1 s")

        injected = {**self.injected, "backend.schematic.ladder_solve": SimpleNamespace(legalise_with_status=solve)}
        with mock.patch.dict(sys.modules, injected), self.environment(SOLVER="cpsat"), \
                mock.patch("backend.schematic.ladder_plan.plan_and_layout", return_value=(object(), layout)):
            ladder.ladder_pose(symbols(), [], intent())
            with mock.patch.dict(os.environ, {ladder.HINT_ENV: "heuristic"}):
                ladder.ladder_pose(symbols(), [], intent())
        self.assertEqual(seen, [None, layout])

    def test_the_seed_list_of_the_environment_reaches_the_solver(self):
        seen = []

        def solve(given_plan, widened, nets, hint=None, seeds=None):
            seen.append(seeds)
            return SimpleNamespace(placements={"U1": Placement((5.08, 7.62), Orientation(90))}, status="OPTIMAL 0.1 s")

        injected = {**self.injected, "backend.schematic.ladder_solve": SimpleNamespace(legalise_with_status=solve)}
        with mock.patch.dict(sys.modules, injected), self.environment(SOLVER="cpsat"), \
                mock.patch("backend.schematic.ladder_plan.plan_and_layout", return_value=(object(), {})):
            ladder.ladder_pose(symbols(), [], intent())
            with mock.patch.dict(os.environ, {ladder.SEEDS_ENV: "7,11, 13"}):
                ladder.ladder_pose(symbols(), [], intent())
        self.assertEqual(seen, [None, (7, 11, 13)])

    def test_the_wires_the_solver_drew_travel_with_the_poses_and_are_what_describe_scores(self):
        class Placed(dict):
            wires = {"N1": ["drawn"]}

        def solve(given_plan, widened, nets, hint=None, seeds=None):
            return SimpleNamespace(placements=Placed(U1=Placement((5.08, 7.62), Orientation(90))), status="OPTIMAL 0.1 s")

        injected = {**self.injected, "backend.schematic.ladder_solve": SimpleNamespace(legalise_with_status=solve)}
        with mock.patch.dict(sys.modules, injected), self.environment(SOLVER="cpsat"), \
                mock.patch("backend.schematic.ladder_plan.plan_and_layout", return_value=(object(), {})):
            result = ladder.ladder_pose(symbols(), [], intent())
        self.assertEqual(result.wires, {"N1": ["drawn"]})
        seen = []
        with mock.patch("backend.schematic.ladder_plan.plan_routes", return_value={"N1": ["derived"], "N2": ["derived"]}), \
                mock.patch("backend.schematic.ladder_plan.attach_unplanned",
                           side_effect=lambda plan, syms, nets, routes: seen.append(routes) or {}):
            ladder.describe("ladder", symbols(), circuit(), object(), result.wires)
        # the solver's drawing of a net replaces the plan's re-derivation of it; the others stay; unplanned nets are attached
        self.assertEqual(seen, [{"N1": ["drawn"], "N2": ["derived"]}])

    def test_an_unknown_solver_and_a_missing_solver_module_are_named(self):
        with mock.patch.dict(sys.modules, self.injected), self.environment(SOLVER="annealing"), self.assertRaisesRegex(ValueError, "annealing"):
            ladder.ladder_pose(symbols(), [], intent())
        absent = {**self.injected, "backend.schematic.ladder_solve": None}
        with mock.patch.dict(sys.modules, absent), self.environment(SOLVER="cpsat"), self.assertRaisesRegex(RuntimeError, "ladder_solve.py"):
            ladder.ladder_pose(symbols(), [], intent())

    def test_the_table_caption_carries_the_source_and_the_default_is_unchanged(self):
        counts = ladder.describe("ladder", symbols(), circuit())
        self.assertIn("### x (solver cpsat: OPTIMAL 1.0 s)\n", ladder.table("x", [counts], "solver cpsat: OPTIMAL 1.0 s"))
        self.assertTrue(ladder.table("x", [counts]).startswith("### x\n"))

    def test_a_run_prints_the_source_in_the_caption_of_each_circuit(self):
        path = self.write("p.json", {"U1": self.U1})
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, self.injected), \
                self.environment(PLACEMENT=str(path)), \
                mock.patch.object(ladder, "find_circuit", return_value=(circuit(), 1, 0, None)), \
                mock.patch.object(ladder, "build_fixture", return_value=intent()), \
                mock.patch.object(ladder, "png_writer", return_value="none"), mock.patch("builtins.print"):
            summary = ladder.run(Path("run"), ["buck"], engine=False, out_dir=Path(directory))
        self.assertIn("### buck (placement from p.json)", summary)
        self.assertIn("| ladder |", summary)


class FixtureCommandTests(unittest.TestCase):
    def arguments(self, **overrides):
        values = {"run": "rec-1", "demos": "all", "corpus": "all", "out": "somewhere"}
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_the_command_hands_the_chosen_circuits_to_the_writer_and_reports_each_file(self):
        import sandbox

        written = [("lm317", 23000, ""), ("amp", 34000, "")]
        with mock.patch.object(sandbox.report, "resolve_run", return_value=Path("run")), \
                mock.patch.object(sandbox.ladder, "write_fixtures", return_value=written) as write, mock.patch("builtins.print") as shown:
            status = sandbox.cmd_ladder_fixtures(self.arguments(demos="lm317,amp", corpus="none"))
        write.assert_called_once_with(Path("run"), Path("somewhere"), ["lm317", "amp"], [])
        self.assertEqual(status, 0)
        self.assertEqual(shown.call_count, 2)

    def test_all_means_every_demo_and_every_corpus_circuit_and_a_failure_is_the_exit_status(self):
        import sandbox

        failed = [("tally_timecode", 0, "ValueError: no slot")]
        with mock.patch.object(sandbox.report, "resolve_run", return_value=Path("run")), \
                mock.patch.object(sandbox.ladder, "write_fixtures", return_value=failed) as write, mock.patch("builtins.print"):
            status = sandbox.cmd_ladder_fixtures(self.arguments())
        self.assertEqual(write.call_args.args[2], sorted(ladder.CIRCUITS))
        self.assertEqual(write.call_args.args[3], sorted(sandbox.corpus.CIRCUITS))
        self.assertEqual(status, 1)

    def test_an_unknown_corpus_circuit_is_refused(self):
        import sandbox

        with mock.patch("sys.stderr"), mock.patch.object(sandbox.ladder, "write_fixtures") as write:
            self.assertEqual(sandbox.cmd_ladder_fixtures(self.arguments(corpus="nope")), 2)
        write.assert_not_called()

    def test_the_command_is_listed_in_the_usage_text(self):
        import sandbox

        self.assertIn("ladder-fixtures", sandbox.__doc__)
        self.assertIn("KICAD_AI_LADDER_PLACEMENT", sandbox.__doc__)
        self.assertIn("KICAD_AI_LADDER_SOLVER", sandbox.__doc__)

    def test_the_backend_check_stands_its_leftover_parts_where_the_harness_does(self):
        from backend.schematic import ladder_check

        placed = [replace(s, position=(x, 20.0)) for s, x in zip(symbols()[:3], (50.0, 120.0, 0.0))]
        nets = [Connection("n", (("R1", "1"), ("U1", "2"))), Connection("m", (("V1", "2"), ("R1", "2")))]
        harness = ladder.others_column(placed, SimpleNamespace(other=("R1",)), gap=5.0, nets=nets)
        backend = ladder_check.others_row(placed, ["R1"], gap=5.0, nets=nets)
        self.assertEqual([(s.reference, s.position) for s in backend], [(s.reference, s.position) for s in harness])
        self.assertNotEqual(backend[2].position, placed[2].position)


if __name__ == "__main__":
    unittest.main()
