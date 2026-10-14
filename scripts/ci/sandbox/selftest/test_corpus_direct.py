"""Corpus circuits through the direct build: fixture registration, the placement source, the libraries handed to the build,
the bench's sheets (mocks and temporary folders; no ``.work`` reads, no KiCad, no MCP server)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import bench, corpus, demos, ladder, redraw, run  # noqa: E402
from scripts import schematic_direct_smoke as smoke  # noqa: E402
from scripts import schematic_validation_fixtures as catalogue  # noqa: E402
from backend.schematic.intent import SchematicIntent, SymbolIntent  # noqa: E402
from backend.schematic.orientation import Orientation  # noqa: E402
from backend.schematic.placement_refinement import OrientedGeometry, PinGeometry, PlacedSymbol  # noqa: E402


def part(reference: str, x: float, y: float, width: float = 10.0) -> PlacedSymbol:
    upright = Orientation(0)
    pins = {"1": PinGeometry((-width / 2, 0.0), (-1.0, 0.0)), "2": PinGeometry((width / 2, 0.0), (1.0, 0.0))}
    return PlacedSymbol(reference, (x, y), upright, {upright: OrientedGeometry(pins, (-width / 2, -2.0, width / 2, 2.0))})


class FixtureTests(unittest.TestCase):
    def test_every_corpus_circuit_is_a_fixture_with_its_connector_roles(self):
        self.assertEqual(set(catalogue.CORPUS_FIXTURES), {corpus.fixture_key(k) for k in corpus.CIRCUITS})
        for key, entry in corpus.CIRCUITS.items():
            intent = catalogue.build_fixture(corpus.fixture_key(key))
            roles = {s.reference: s.role for s in intent.symbols}
            for reference in entry.input_connectors:
                self.assertEqual(roles[reference], "input_connector")
            for reference in entry.output_connectors:
                self.assertEqual(roles[reference], "output_connector")
            self.assertTrue(intent.blocks)

    def test_a_registered_name_builds_and_an_unknown_one_is_refused(self):
        with mock.patch.dict(catalogue.CORPUS_FIXTURES, {"corpus_x": lambda: "built"}):
            self.assertEqual(catalogue.build_fixture("corpus_x"), "built")
        with self.assertRaisesRegex(ValueError, "Unknown schematic fixture"):
            catalogue.build_fixture("corpus_nonexistent")

    def test_the_engine_does_not_import_the_data_module(self):
        for path in (REPO / "backend").rglob("*.py"):
            self.assertNotIn("schematic_corpus_circuits", path.read_text(encoding="utf-8", errors="ignore"), path)

    def test_the_renderer_writes_roles_and_the_function_name(self):
        circuit = demos.Circuit("k", (), (), (), (), ())
        text = demos.render_function(circuit, "x", {"J1": "input_connector"}, "corpus_k")
        self.assertIn("def corpus_k()", text)
        part_circuit = demos.Circuit("k", (NS(reference="J1", library="Connector", symbol="Conn", value="Conn"),), (), (), (), ())
        self.assertIn("role='input_connector'", demos.render_function(part_circuit, "", {"J1": "input_connector"}))


class OthersColumnTests(unittest.TestCase):
    def test_leftover_parts_stand_in_a_row_below_the_placed_ones(self):
        symbols = [part("R1", 50.0, 20.0), part("R2", 70.0, 20.0), part("J1", 0.0, 0.0, 6.0), part("J2", 0.0, 0.0, 6.0)]
        moved = {s.reference: s for s in ladder.others_column(symbols, NS(other=("J1", "J2")), gap=5.0)}
        self.assertEqual(moved["R1"].position, (50.0, 20.0))                       # placed parts stay
        self.assertEqual(moved["J1"].position[1] - 2.0, 22.0 + 3 * 5.0)           # top three gaps under R1/R2's boxes (bottom 22)
        self.assertEqual(moved["J1"].position[0] - 3.0, 45.0)                      # no nets: left-aligned with the frame (45)
        self.assertEqual(moved["J2"].position[0] - moved["J1"].position[0], 6.0 + 2 * 5.0)   # side by side, two gaps apart
        self.assertEqual(moved["J2"].position[1], moved["J1"].position[1])

    def test_a_leftover_stands_under_the_parts_it_connects_to(self):
        symbols = [part("R1", 50.0, 20.0), part("R2", 120.0, 20.0), part("J1", 0.0, 0.0, 6.0)]
        nets = [NS(net="n", pins=(("J1", "1"), ("R2", "1")))]
        moved = {s.reference: s for s in ladder.others_column(symbols, NS(other=("J1",)), gap=5.0, nets=nets)}
        self.assertEqual(moved["J1"].position[0], 120.0)

    def test_without_leftovers_nothing_moves(self):
        symbols = [part("R1", 1.0, 2.0)]
        self.assertEqual(ladder.others_column(symbols, NS(other=())), symbols)


class PlacementSourceTests(unittest.TestCase):
    def test_a_corpus_fixture_is_placed_from_library_geometry_with_no_recorded_run(self):
        symbols = [part("R1", 0.0, 0.0), part("R2", 20.0, 0.0), part("J1", 0.0, 0.0)]
        placed = [replace(symbols[0], position=(-30.0, -10.0)), replace(symbols[1], position=(-10.0, -10.0)), symbols[2]]
        roles = NS(other=("J1",))
        entry = corpus.CIRCUITS["piaa_power"]
        built = NS(intent=SchematicIntent(()))
        with mock.patch.object(corpus, "build", return_value=built) as build, \
                mock.patch.object(ladder, "corpus_inputs", return_value=(symbols, [], {}, {})) as inputs, \
                mock.patch.object(ladder, "ladder_pose", return_value=ladder.LadderResult(placed, roles, "ladder")), \
                mock.patch.object(ladder, "roles_summary", return_value="chain a: R1"):
            poses, layout, text, note = run.corpus_placement("corpus_piaa_power", Path("libs"))
        self.assertEqual(note, "ladder")
        build.assert_called_once_with(entry)
        self.assertEqual(inputs.call_args.args[2], Path("libs"))
        self.assertEqual(sorted(poses), ["J1", "R1", "R2"])                      # every part has a pose, the leftover too
        self.assertEqual(min(p["position"][0] for p in poses.values()), run.SHEET_MARGIN_MM)
        self.assertEqual(min(p["position"][1] for p in poses.values()), run.SHEET_MARGIN_MM)
        self.assertGreater(poses["J1"]["position"][1], poses["R2"]["position"][1])  # below
        self.assertEqual(sorted(layout), ["J1", "R1", "R2"])
        self.assertEqual(text, "chain a: R1")

    def test_only_corpus_circuits_are_corpus_fixtures(self):
        self.assertTrue(run.is_corpus("corpus_piaa_power"))
        self.assertFalse(run.is_corpus("demo_buck"))
        self.assertFalse(run.is_corpus("corpus_nonexistent"))

    def test_a_build_without_a_recorded_run_is_for_corpus_ladder_runs_only(self):
        for fixtures, placement in ((["demo_buck"], "ladder"), (["corpus_piaa_power"], "engine")):
            with self.assertRaisesRegex(run.SandboxError, "--placement-from"):
                run.run_experiment(label="t", fixtures=fixtures, overlays=[], extra_env={}, timeout=1,
                                   placement=placement)


class LibraryTests(unittest.TestCase):
    SHEET = '(kicad_sch (lib_symbols (symbol "Diode:D" (symbol "D_1_1" (pin passive line (at 0 0 0) (length 1) (name "A") (number "1"))))))'

    def test_a_renamed_library_is_recorded_for_the_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock"
            stock.mkdir()
            (stock / "Diode.kicad_sym").write_text('(kicad_symbol_lib (symbol "Other"))', encoding="utf-8")   # lacks "D"
            sheet = root / "sheets" / "a.kicad_sch"
            sheet.parent.mkdir()
            sheet.write_text(self.SHEET, encoding="utf-8")
            destination = root / "libs"
            corpus.prepare_project(sheet, destination, stock)
            self.assertEqual(json.loads((destination / corpus.NICKNAMES_FILE).read_text(encoding="utf-8")), {"Diode": "Diode_sbx"})
            self.assertIn('(name "Diode_sbx")', (destination / "sym-lib-table").read_text(encoding="utf-8"))

    def test_the_smoke_runner_puts_the_table_in_the_project_and_renames_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            libs, project = Path(directory) / "libs", Path(directory) / "project"
            libs.mkdir()
            project.mkdir()
            (libs / "sym-lib-table").write_text("(sym_lib_table)", encoding="utf-8")
            (libs / corpus.NICKNAMES_FILE).write_text('{"Diode": "Diode_sbx"}', encoding="utf-8")
            intent = SchematicIntent((SymbolIntent("D1", "Diode", "D", "D"), SymbolIntent("R1", "Device", "R", "1k")))
            result = smoke.prepare_libraries(libs, project, intent)
            self.assertEqual((project / "sym-lib-table").read_text(encoding="utf-8"), "(sym_lib_table)")
            self.assertEqual([s.library for s in result.symbols], ["Diode_sbx", "Device"])


class BenchSheetTests(unittest.TestCase):
    def test_a_corpus_circuits_sheets_are_the_human_one_and_the_runs_own(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            human = root / "human.kicad_sch"
            human.write_text("(kicad_sch)", encoding="utf-8")
            run_dir = root / "2026-x"
            built = run_dir / "artifacts" / "schematic-engine-corpus_piaa_power"
            built.mkdir(parents=True)
            (built / "mcu_smoke.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
            with mock.patch.object(corpus, "sheet_file", return_value=human):
                found = redraw.sheets_of(run_dir, "corpus_piaa_power")
                self.assertEqual(sorted(found), ["engine", "human"])
                self.assertEqual(found["human"], human)
                self.assertEqual(bench.demo_keys(run_dir), ["corpus_piaa_power"])
            self.assertEqual(bench.row_name("corpus_piaa_power", run_dir), "ladder x")
            self.assertTrue(bench.row_name("demo_buck", run_dir).startswith("engine"))


if __name__ == "__main__":
    unittest.main()
