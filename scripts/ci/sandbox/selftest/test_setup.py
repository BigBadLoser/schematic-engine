"""Pin drift guard, bootstrap safety and CLI wiring (no network)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
if str(SANDBOX) not in sys.path:
    sys.path.insert(0, str(SANDBOX))

import sandbox  # noqa: E402
from sbx import bootstrap, env  # noqa: E402


class PinDriftTests(unittest.TestCase):
    """The sandbox must run what the pinned engine image runs; these fail when the two drift apart."""

    def setUp(self):
        self.pins = env.load_pins()
        self.dockerfile = (REPO / "scripts/ci/Dockerfile.engine").read_text(encoding="utf-8")

    def test_python_pins_match_the_ci_image(self):
        for name, version in self.pins["pip"].items():
            self.assertIn(f"{name}=={version}", self.dockerfile)

    def test_elk_bundle_pin_matches_the_ci_image(self):
        bundle = self.pins["elkjs_bundle"]
        self.assertIn(bundle["url"], self.dockerfile)
        self.assertIn(bundle["git_blob_sha1"], self.dockerfile)

    def test_node_dependencies_are_exact_versions(self):
        manifest = json.loads((SANDBOX / "package.json").read_text(encoding="utf-8"))
        for name, version in manifest["dependencies"].items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+(-[\w.]+)?$", name)


class BootstrapTests(unittest.TestCase):
    def test_git_blob_hash_matches_git(self):
        self.assertEqual(bootstrap.git_blob_sha1(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")

    def test_plan_marks_exactly_the_downloading_steps_as_network(self):
        steps = bootstrap.plan(env.load_pins(), Path("python"))
        self.assertEqual([s.key for s in steps], ["venv", "pip", "npm", "elk", "freeze"])
        self.assertEqual([s.network for s in steps], [False, True, True, True, False])

    def test_plan_description_names_sources_and_hash(self):
        text = bootstrap.describe(bootstrap.plan(env.load_pins(), Path("python")))
        self.assertIn("--only-binary=:all:", text)
        self.assertIn("--ignore-scripts", text)
        self.assertIn(env.load_pins()["elkjs_bundle"]["git_blob_sha1"], text)

    def test_bootstrap_without_yes_never_executes(self):
        with mock.patch.object(bootstrap, "execute", side_effect=AssertionError("executed")), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(sandbox.cmd_bootstrap(argparse.Namespace(yes=False)), 0)
        self.assertIn("Nothing was downloaded", printed.getvalue())


class CliTests(unittest.TestCase):
    def test_parser_knows_every_documented_command(self):
        parser = sandbox.build_parser()
        for argv in (
            ["doctor"], ["bootstrap"], ["overlays", "--check"], ["probes", "--only", "pin_audit"],
            ["run", "--label", "x", "--overlay", "e9_no_direct_junction", "--env", "A=B"],
            ["kpi", "somewhere"], ["rescore", "x"], ["compare", "a", "b"], ["analyze-dumps", "x"], ["terminals", "x"],
            ["clean"],
        ):
            self.assertTrue(parser.parse_args(argv).handler)

    def test_env_pairs_are_parsed_and_malformed_ones_rejected(self):
        self.assertEqual(sandbox._parse_env(["A=1", "B=x=y"]), {"A": "1", "B": "x=y"})
        with self.assertRaises(SystemExit):
            sandbox._parse_env(["oops"])


if __name__ == "__main__":
    unittest.main()
