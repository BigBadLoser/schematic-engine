"""The stage methods of ``DirectWorkflow``, against a fake MCP client."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from backend.schematic import PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.direct_workflow import DirectWorkflow


class FakeClient:
    """Records every tool call; a read answers with ``reads[name]`` (a value or a function of the arguments)."""

    def __init__(self, schematic: Path, tools: set[str] | None = None) -> None:
        self.schematic = schematic
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.reads: dict[str, Any] = {}
        self.tools = tools if tools is not None else (
            DirectWorkflow.REQUIRED_WRITE_TOOLS | DirectWorkflow.REQUIRED_QA_TOOLS
            | {"sch_normalize_text_sizes", "sch_align_to_grid", "sch_straighten_wires", "schematic_connectivity_gate", "run_erc"}
        )

    @staticmethod
    def schematic_writes_enabled() -> bool:
        return True

    def list_tools(self) -> set[str]:
        return self.tools

    def call_schematic_write_tool(self, root: str, *, sch_file: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments, True))
        if name == "sch_set_sheet_size":                    # what the tool does to the file
            text = self.schematic.read_text()
            self.schematic.write_text(re.sub(r'\(paper "[^"]*"\)', f'(paper "{arguments["paper"]}")', text))
        return {"tool": name}

    def call_schematic_read_tool(self, root: str, *, sch_file: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments, False))
        answer = self.reads.get(name, {"tool": name, "status": "PASS"})
        return answer(arguments) if callable(answer) else answer

    def writes(self, name: str) -> list[dict[str, Any]]:
        return [args for called, args, written in self.calls if written and called == name]


class FakeKiCad:
    def __init__(self, refs: set[str]) -> None:
        self.refs = refs

    def read_project(self, root: Path) -> dict[str, Any]:
        return {"project_graph": {"nodes": [{"kind": "component", "ref": ref} for ref in sorted(self.refs)]}}


class DirectWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sheet = self.root / "fixture.kicad_sch"
        self.sheet.write_bytes(b'ORIGINAL (paper "A4")')
        self.client = FakeClient(self.sheet)
        self.workflow = DirectWorkflow(
            self.client, FakeKiCad({"R1"}), artifact_root=self.root,    # type: ignore[arg-type]
        )

    # --- the connection to the server ---------------------------------------------------------------------------

    def test_parses_mcp_symbol_placements_without_generating_coordinates(self) -> None:
        placements = DirectWorkflow._symbol_placements_from_listing(
            {
                "result": (
                    "Symbols (2 total):\n"
                    "- R1 1k Device:R @ (152.40, 139.70) rot=90 unit=1\n"
                    "Power symbols:\n"
                    "- #FLG01 PWR_FLAG @ (177.80, 175.26) unit=1"
                )
            },
            {"R1", "#FLG01"},
        )
        self.assertEqual(
            placements,
            {
                "R1": {"x_mm": 152.4, "y_mm": 139.7, "rotation": 90, "unit": 1},
                "#FLG01": {"x_mm": 177.8, "y_mm": 175.26, "rotation": 0, "unit": 1},
            },
        )

    def test_a_listing_missing_a_reference_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "R2"):
            DirectWorkflow._symbol_placements_from_listing(
                {"result": "Symbols (1 total):\n- R1 1k Device:R @ (1.00, 2.00) rot=0 unit=1"}, {"R1", "R2"},
            )

    def test_unwraps_json_result_payload(self) -> None:
        parsed = DirectWorkflow._unwrap({"result": '{"cosmetic_score": 96.0, "status": "WARN"}'})
        self.assertEqual(parsed["cosmetic_score"], 96.0)
        self.assertEqual(parsed["status"], "WARN")

    def test_unwrap_preserves_mcp_text_diagnostics(self) -> None:
        parsed = DirectWorkflow._unwrap(
            {
                "result": '{"metadata":{"summary":{"error":2}}}',
                "_content_text": "ERC summary:\n- [error] first finding\n- [error] second finding",
            }
        )
        self.assertEqual(parsed["metadata"]["summary"]["error"], 2)
        self.assertIn("first finding", parsed["_content_text"])

    def test_a_failed_write_keeps_the_sheet_the_stage_saw(self) -> None:
        def refuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("stage failed")

        self.client.call_schematic_write_tool = refuse      # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "stage failed"):
            self.workflow._write(self.root, self.sheet, "sch_move_symbol", {})
        self.assertEqual((self.root / DirectWorkflow.FAILED_SHEET).read_bytes(), self.sheet.read_bytes())

    def test_the_server_must_offer_every_tool_the_stages_call(self) -> None:
        self.assertTrue(self.workflow.available())
        self.client.tools = self.client.tools - {"sch_visual_qa"}
        self.assertFalse(self.workflow.available())

    def test_every_required_tool_is_one_the_guarded_client_lets_through(self) -> None:
        from backend.kicad.mcp_client import GUARDED_WRITE_TOOLS, READ_ONLY_TOOLS

        self.assertFalse(DirectWorkflow.REQUIRED_WRITE_TOOLS - GUARDED_WRITE_TOOLS)
        self.assertFalse(DirectWorkflow.REQUIRED_QA_TOOLS - READ_ONLY_TOOLS)

    def test_references_missing_from_the_project_readback_are_refused(self) -> None:
        self.workflow._verify_references(self.root, {"R1"})
        with self.assertRaisesRegex(RuntimeError, "C1"):
            self.workflow._verify_references(self.root, {"R1", "C1"})

    # --- the layout policy --------------------------------------------------------------------------------------

    def test_the_layout_policy_is_tight_unless_the_environment_says_otherwise(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for name in ("KICAD_AI_LAYOUT_MODE", "KICAD_AI_LAYOUT_MARGIN_MM", "KICAD_AI_STUB_OBSTACLES"):
                os.environ.pop(name, None)
            policy = self.workflow._initial_layout_policy()
        self.assertEqual((policy.mode, policy.tight_terminals, policy.stub_obstacles), ("tight", True, False))
        with patch.dict(os.environ, {"KICAD_AI_LAYOUT_MODE": "legacy", "KICAD_AI_STUB_OBSTACLES": "1"}):
            policy = self.workflow._initial_layout_policy()
        self.assertEqual((policy.mode, policy.tight_terminals, policy.stub_obstacles), ("legacy", True, True))
        with patch.dict(os.environ, {"KICAD_AI_LAYOUT_MODE": "loose"}):
            with self.assertRaises(ValueError):
                self.workflow._initial_layout_policy()

    def test_the_server_is_told_the_policy_and_the_router_its_obstacle_setting(self) -> None:
        with patch.dict(os.environ, {"KICAD_AI_LAYOUT_MODE": "tight", "KICAD_AI_STUB_OBSTACLES": "1"}):
            policy = self.workflow._initial_layout_policy()
        self.workflow._apply_layout_policy(self.root, self.sheet, policy)
        self.assertEqual(self.client.writes("sch_set_layout_clearances"), [policy.as_arguments()])
        self.assertTrue(self.workflow._route_obstacle_boxes)

    # --- the paper -----------------------------------------------------------------------------------------------

    def _offsheet(self, until: int) -> None:
        """Visual QA finds something off the sheet until the paper has been grown ``until`` times."""
        def qa(arguments: dict[str, Any]) -> dict[str, Any]:
            if len(self.client.writes("sch_set_sheet_size")) >= until:
                return {"status": "PASS", "findings": []}
            return {"status": "WARN", "sheets": [{"status": "WARN", "findings": [{"code": "offsheet_symbol", "level": "WARN"}]}]}

        self.client.reads["sch_visual_qa"] = qa

    def test_the_paper_grows_one_size_at_a_time_while_something_stands_off_it(self) -> None:
        self._offsheet(until=2)
        grown = self.workflow._grow_sheet(self.root, self.sheet)
        self.assertEqual(grown, {"status": "sheet_grown", "papers": ["A3", "A2"]})
        self.assertEqual([a["paper"] for a in self.client.writes("sch_set_sheet_size")], ["A3", "A2"])
        self.assertNotIn("sch_fix_readability", [name for name, _args, _write in self.client.calls])

    def test_a_sheet_with_nothing_off_it_keeps_its_paper(self) -> None:
        self.client.reads["sch_visual_qa"] = {"status": "PASS", "findings": []}
        self.assertEqual(self.workflow._grow_sheet(self.root, self.sheet), {"status": "not_needed", "papers": []})
        self.assertEqual(self.client.writes("sch_set_sheet_size"), [])

    def test_the_paper_stops_at_the_largest_size(self) -> None:
        self.sheet.write_bytes(b'ORIGINAL (paper "A0")')
        self._offsheet(until=99)
        self.assertEqual(self.workflow._grow_sheet(self.root, self.sheet)["papers"], [])

    # --- the cosmetic loop ---------------------------------------------------------------------------------------

    def _scores(self, first: dict[str, Any], then: dict[str, Any]) -> None:
        def score(arguments: dict[str, Any]) -> dict[str, Any]:
            return first if sum(1 for name, _a, _w in self.client.calls if name == "sch_cosmetic_score") == 1 else then

        self.client.reads["sch_cosmetic_score"] = score

    def test_a_cosmetic_fixer_uses_dry_run_then_apply(self) -> None:
        self._scores(
            {"cosmetic_score": 90.0, "status": "WARN", "worst_category": "typography"},
            {"cosmetic_score": 96.0, "status": "WARN", "worst_category": "documentation"},
        )
        score, fixes = self.workflow._run_cosmetic_loop(self.root, self.sheet)
        self.assertEqual(self.client.writes("sch_normalize_text_sizes"), [{"apply": False}, {"apply": True}])
        self.assertEqual(score["cosmetic_score"], 96.0)
        self.assertEqual([fix["status"] for fix in fixes], ["applied"])

    def test_the_orientation_fixer_never_runs_because_the_planners_turn_power_symbols(self) -> None:
        # MCP Pro's normaliser turns every power symbol and flag back to 0 degrees, which undoes the planners
        # that turn them on purpose (a flag standing beside a vertical wire).
        self._scores(
            {"cosmetic_score": 90.0, "status": "WARN", "worst_category": "orientation"},
            {"cosmetic_score": 90.0, "status": "WARN", "worst_category": "orientation"},
        )
        _score, fixes = self.workflow._run_cosmetic_loop(self.root, self.sheet)
        self.assertEqual(self.client.writes("sch_normalize_power_orientation"), [])
        self.assertEqual([fix["status"] for fix in fixes], ["planned_by_engine"])

    def test_a_fixer_that_would_cut_a_connection_is_refused(self) -> None:
        def write(root: str, *, sch_file: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.client.calls.append((name, arguments, True))
            return {"connectivity_preserved": False}

        self.client.call_schematic_write_tool = write      # type: ignore[method-assign]
        self._scores({"cosmetic_score": 90.0, "worst_category": "grid"}, {"cosmetic_score": 90.0})
        _score, fixes = self.workflow._run_cosmetic_loop(self.root, self.sheet)
        self.assertEqual([fix["status"] for fix in fixes], ["refused"])
        self.assertEqual(self.client.writes("sch_align_to_grid"), [{"apply": False}])

    # --- marks and checks ----------------------------------------------------------------------------------------

    def test_explicit_no_connects_are_delegated_to_mcp(self) -> None:
        intent = SchematicIntent(
            symbols=(SymbolIntent("R1", "Device", "R", "1k"),), no_connects=(PinEndpoint("R1", "2"),),
        )
        self.assertIsNone(self.workflow._apply_no_connects(self.root, self.sheet, SchematicIntent(
            symbols=(SymbolIntent("R1", "Device", "R", "1k"),),
        )))
        result = self.workflow._apply_no_connects(self.root, self.sheet, intent)
        self.assertEqual(result, {"tool": "sch_mark_no_connect_pins"})
        self.assertEqual(self.client.writes("sch_mark_no_connect_pins"), [{"entries": [{"reference": "R1", "pin": "2"}]}])

    def test_a_failed_gate_names_why(self) -> None:
        DirectWorkflow._assert_acceptance_gate("Gate", {"verdict": "PASS"})
        DirectWorkflow._assert_acceptance_gate("Gate", None)
        with self.assertRaisesRegex(RuntimeError, r"Gate failed: floating pin"):
            DirectWorkflow._assert_acceptance_gate("Gate", {"status": "FAIL", "summary": "floating pin\nmore"})

    def test_erc_errors_are_refused_and_warnings_are_not(self) -> None:
        DirectWorkflow._assert_erc_has_no_errors({"metadata": {"summary": {"error": 0, "warning": 3}}})
        DirectWorkflow._assert_erc_has_no_errors(None)
        with self.assertRaisesRegex(RuntimeError, "ERC failed with 2 error"):
            DirectWorkflow._assert_erc_has_no_errors({"metadata": {"summary": {"error": 2}}})
        with self.assertRaisesRegex(RuntimeError, "ERC failed with 1 error finding"):
            DirectWorkflow._assert_erc_has_no_errors({"findings": [{"severity": "error", "description": "pin conflict"}]})

    def test_auxiliary_identity_is_declared_not_inferred_from_library_name(self) -> None:
        from dataclasses import replace
        from backend.schematic.direct_workflow import _is_authoring_auxiliary

        flag = SymbolIntent("#FLG01", "power", "PWR_FLAG", "PWR_FLAG", authoring_auxiliary=True)
        intent = SchematicIntent(symbols=(SymbolIntent("R1", "Device", "R", "1k"), flag))
        self.assertTrue(_is_authoring_auxiliary(intent, "#FLG01"))
        self.assertFalse(_is_authoring_auxiliary(intent, "R1"))
        untagged = replace(intent, symbols=(intent.symbols[0], replace(flag, authoring_auxiliary=False)))
        self.assertFalse(_is_authoring_auxiliary(untagged, "#FLG01"))
        renamed = replace(intent, symbols=(intent.symbols[0], replace(flag, library="Other", symbol_name="AUX")))
        self.assertTrue(_is_authoring_auxiliary(renamed, "#FLG01"))

    def test_no_circuit_reference_is_named_in_the_workflow(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "backend" / "schematic" / "direct_workflow.py").read_text(encoding="utf-8")
        for ref in ("U1", "J2"):
            self.assertNotIn(f'"{ref}"', source)

    def test_visual_qa_codes_are_collected_across_sheets(self) -> None:
        self.assertEqual(DirectWorkflow._visual_qa_codes(None), set())
        self.assertEqual(
            DirectWorkflow._visual_qa_codes({
                "findings": [{"code": "a"}], "sheets": [{"findings": [{"code": "b"}, {"level": "WARN"}]}],
            }),
            {"a", "b"},
        )


if __name__ == "__main__":
    unittest.main()
