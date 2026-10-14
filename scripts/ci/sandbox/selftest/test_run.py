"""Run orchestration with a fake engine: real snapshots, overlays, manifests and scoring.

No MCP Pro, KiCad, Node or network. The server and fixture processes are replaced;
everything around them (snapshot, patching, environment, scoring, comparison) is real.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import bootstrap, env, report, run  # noqa: E402

BLANK_SHEET = (
    '(kicad_sch (version 20250114) (generator "eeschema") (paper "A4") (lib_symbols)\n'
    ' (wire (pts (xy 10 10) (xy 20 10)) (stroke (width 0) (type default)) (uuid "a"))\n)\n'
)


def fake_fixture(src, child_env, fixture, artifact, log_path, timeout, command=None):
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "mcu_smoke.kicad_sch").write_text(BLANK_SHEET, encoding="utf-8")
    (artifact / "report.json").write_text(json.dumps({
        "fixture": fixture,
        "workflow": {"cosmetic_score": {"cosmetic_score": 96.0}, "netlist_verification": {"status": "PASS"}},
    }), encoding="utf-8")
    (artifact / "quality-selection.json").write_text(json.dumps({"selected_profile": "compact"}), encoding="utf-8")
    fake_fixture.seen_env = child_env
    return {"fixture": fixture, "returncode": 0, "seconds": 0.1, "artifact": str(artifact), "log": str(log_path)}


def recorded_run():
    """Stand-ins for the recorded run a build takes its placement from: the run resolves, every fixture has no parts."""
    return (
        mock.patch.object(report, "resolve_run", return_value=Path("recorded-run")),
        mock.patch.object(run, "recorded_placement", return_value=({}, {})),
    )


class ParallelPartsTests(unittest.TestCase):
    def test_job_cap(self):
        self.assertEqual(run.effective_jobs(8, 3), 3)
        self.assertEqual(run.effective_jobs(2, 5), 2)
        self.assertEqual(run.effective_jobs(0, 5), 1)

    def test_queue_results_keep_fixture_order(self):
        import time

        def work(port, item):
            time.sleep(0.05 * (5 - item))  # later items finish first
            return {"item": item, "port": port}

        results = run.run_queue(list(range(5)), [0, 1], work)
        self.assertEqual([r["item"] for r in results], [0, 1, 2, 3, 4])
        self.assertTrue({r["port"] for r in results} <= {0, 1})

    def test_worker_error_is_raised(self):
        def work(port, item):
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            run.run_queue([1, 2], [0, 1], work)

    def test_parallel_run_records_jobs_and_orders_results(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        seen = {}

        def fixture_runner(src, child_env, fixture, artifact, log_path, timeout, command):
            seen[fixture] = command
            return fake_fixture(src, child_env, fixture, artifact, log_path, timeout)

        resolve, placement = recorded_run()
        with mock.patch.object(env, "RUNS_DIR", Path(directory.name) / "runs"), \
                mock.patch.object(run, "readiness", return_value=[]), \
                mock.patch.object(run, "run_fixture", fixture_runner), \
                mock.patch.object(run, "tool_versions", return_value={}), \
                resolve, placement, contextlib.redirect_stdout(io.StringIO()):
            run_dir = run.run_experiment(
                label="t", fixtures=["rc_filter", "linear_regulator"], overlays=[],
                extra_env={}, timeout=5.0, jobs=5, placement_from="recorded-run",
            )
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["jobs"], 2)
        self.assertEqual(manifest["mode"], "direct")
        self.assertEqual([r["fixture"] for r in manifest["results"]], ["rc_filter", "linear_regulator"])
        for fixture, command in seen.items():                  # every build runs the direct smoke on its placement
            self.assertTrue(command[1].endswith("schematic_direct_smoke.py"), fixture)
            self.assertTrue(command[3].endswith("placement.json"), fixture)

class OrchestrationTests(unittest.TestCase):
    def run_fake(self, **overrides):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runs = Path(directory.name) / "runs"
        options = dict(
            label="t", fixtures=["rc_filter"], overlays=[], extra_env={}, timeout=5.0,
            placement_from="recorded-run",
        )
        options.update(overrides)
        resolve, placement = recorded_run()
        with mock.patch.object(env, "RUNS_DIR", runs), \
                mock.patch.object(run, "readiness", return_value=[]), \
                mock.patch.object(run, "run_fixture", fake_fixture), \
                mock.patch.object(run, "tool_versions", return_value={"python": "test"}), \
                resolve, placement, contextlib.redirect_stdout(io.StringIO()):
            run_dir = run.run_experiment(**options)
        return run_dir

    def test_run_directory_has_manifest_kpis_and_a_patched_snapshot(self):
        run_dir = self.run_fake(overlays=["e9_no_direct_junction", "e3_dump_libavoid"], extra_env={"SBX_NO_DIRECT_JUNCTION": "1"})
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([o["name"] for o in manifest["overlays"]], ["e9_no_direct_junction", "e3_dump_libavoid"])
        self.assertEqual(manifest["extra_env"], {"SBX_NO_DIRECT_JUNCTION": "1"})
        self.assertGreater(manifest["snapshot"]["files"], 50)
        self.assertEqual(manifest["results"][0]["selected_profile"], "compact")
        self.assertEqual(manifest["results"][0]["netlist"], "PASS")
        patched = (run_dir / "src/backend/kicad/mcp_extended_server.py").read_text(encoding="utf-8")
        self.assertIn("SANDBOX OVERLAY e9_no_direct_junction", patched)
        self.assertIn("SANDBOX OVERLAY e3_dump_libavoid", patched)
        kpis = json.loads((run_dir / "kpi.json").read_text(encoding="utf-8"))
        self.assertEqual(kpis["rc_filter"]["wire_length_mm"], 10.0)
        self.assertTrue((run_dir / "artifacts/schematic-engine-rc_filter/report.json").is_file())

    def test_the_repository_itself_is_not_modified(self):
        server = REPO / "backend/kicad/mcp_extended_server.py"
        before = server.read_bytes()
        self.run_fake(overlays=["e9_no_direct_junction"])
        self.assertEqual(server.read_bytes(), before)
        self.assertNotIn(b"SANDBOX OVERLAY", before)

    def test_fixture_environment_carries_overlay_knobs_and_the_dump_directory(self):
        run_dir = self.run_fake(extra_env={"SBX_NO_DIRECT_JUNCTION": "7"})
        seen = fake_fixture.seen_env
        self.assertEqual(seen["SBX_NO_DIRECT_JUNCTION"], "7")
        self.assertEqual(seen["SBX_DUMP_DIR"], str(run_dir / "dumps"))
        self.assertEqual(seen["PYTHONPATH"], str(run_dir / "src"))
        self.assertEqual(seen["KICAD_AI_ENABLE_SCHEMATIC_WRITES"], "1")
        self.assertEqual(seen["PYTHONNOUSERSITE"], "1")

    def test_not_ready_sandbox_refuses_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory) / "runs"
            resolve, placement = recorded_run()
            with mock.patch.object(env, "RUNS_DIR", runs), \
                    mock.patch.object(run, "readiness", return_value=["thing missing"]), resolve, placement:
                with self.assertRaises(run.SandboxError):
                    run.run_experiment(label="t", fixtures=["rc_filter"], overlays=[], extra_env={}, timeout=1, placement_from="recorded-run")
            self.assertFalse(runs.exists())

    def test_two_runs_can_be_compared(self):
        first = self.run_fake(label="a")
        second = self.run_fake(label="b", overlays=["e9_no_direct_junction"])
        text = report.compare(first, second)
        self.assertIn("### rc_filter", text)
        self.assertIn("wire mm", text)
        self.assertIn("overlays=['e9_no_direct_junction']", text)


class LadderPlacementTests(unittest.TestCase):
    """The ladder placement source: harness symbols become poses; parts the ladder leaves keep the engine's."""

    def test_ladder_symbols_become_poses_shifted_to_the_margin_and_other_parts_stay(self):
        from types import SimpleNamespace as NS
        from sbx import ladder
        def sym(ref, x, y, rot=0, mirror=""):
            return NS(reference=ref, position=(x, y), orientation=NS(rotation=rot, mirror=mirror))
        engine = [sym("R1", 50.0, 60.0), sym("U1", 80.0, 60.0), sym("#FLG1", 1.0, 1.0)]
        moved = [sym("R1", -10.0, -20.0, 90), sym("U1", 80.0, 60.0)]   # U1 is "other": the ladder left it
        with mock.patch.object(ladder, "engine_pose", return_value=engine),                 mock.patch.object(ladder, "roles_for", return_value=object()),                 mock.patch.object(ladder, "layout_step", return_value=(moved, "ladder", None)),                 mock.patch.object(ladder, "roles_summary", return_value="chain a: R1"),                 mock.patch("scripts.schematic_validation_fixtures.build_fixture", return_value=object()):
            poses, layout, text, note = run.ladder_placement({"refine": {"args": [None, []]}}, "demo")
        self.assertEqual(note, "ladder")
        self.assertEqual(sorted(poses), ["R1", "U1"])                  # flags dropped
        self.assertEqual(poses["R1"]["position"], [12.7, 12.7])       # minimum x and y at the margin
        self.assertEqual(poses["R1"]["rotation"], 90)
        self.assertEqual(poses["U1"]["position"], [102.7, 92.7])      # engine's pose, shifted with the rest
        self.assertEqual(layout["R1"]["position"], [12.7, 12.7])
        self.assertEqual(text, "chain a: R1")


class VenvTests(unittest.TestCase):
    def test_create_venv_produces_the_interpreter_path_the_sandbox_expects(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv"
            with mock.patch.object(env, "VENV_DIR", target):
                with contextlib.redirect_stdout(io.StringIO()):
                    bootstrap.create_venv(Path(sys.executable))
                    self.assertTrue(env.venv_python().is_file())
                    bootstrap.create_venv(Path(sys.executable))  # idempotent
                self.assertTrue(env.interpreter_version(env.venv_python()))


if __name__ == "__main__":
    unittest.main()
