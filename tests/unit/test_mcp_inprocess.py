from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.kicad.mcp_client import McpConnectionError, McpHttpClient, make_client

NEEDS_MCP_PRO = unittest.skipUnless(importlib.util.find_spec("kicad_mcp"), "needs kicad-mcp-pro (it is the server)")
SHEET = Path(__file__).resolve().parents[1] / "fixtures" / "schematic_engine" / "engine_smoke.kicad_sch"

# What McpHttpClient returns for a read tool over streamable-http: the tool's structuredContent plus the joined text blocks.
READ_SHAPE = {"result", "_content_text"}


class FactoryTests(unittest.TestCase):
    def test_http_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"KICAD_AI_MCP_INPROCESS": ""}):
            self.assertIs(type(make_client()), McpHttpClient)

    def test_switch_selects_the_in_process_client(self) -> None:
        from backend.kicad.mcp_inprocess import McpInProcessClient

        with mock.patch.dict(os.environ, {"KICAD_AI_MCP_INPROCESS": "1"}):
            self.assertIsInstance(make_client(), McpInProcessClient)


@NEEDS_MCP_PRO
class InProcessClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from backend.kicad.mcp_inprocess import McpInProcessClient

        cls._env = mock.patch.dict(os.environ, {"KICAD_AI_ENABLE_SCHEMATIC_WRITES": "1"})
        cls._env.start()
        cls.client = McpInProcessClient()
        cls.directory = Path(tempfile.mkdtemp(prefix="kicad-ai-inproc-"))
        cls.sheet = cls.directory / "sheet.kicad_sch"
        shutil.copy2(SHEET, cls.sheet)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._env.stop()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def test_lists_the_tools_the_workflow_needs(self) -> None:
        from backend.kicad.mcp_client import SCHEMATIC_WORKFLOW_REQUIRED_TOOLS

        self.assertEqual(self.client.probe(SCHEMATIC_WORKFLOW_REQUIRED_TOOLS)["missing_required_tools"], [])

    def test_read_tool_returns_the_http_shape(self) -> None:
        result = self.client.call_schematic_read_tool(
            str(self.directory), sch_file=str(self.sheet), name="sch_list_sheets", arguments={},
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(set(result), READ_SHAPE)
        self.assertIsInstance(result["result"], str)
        self.assertIn("sch_list_sheets", self.client.runtime_state()["successful_tools"])

    def test_unknown_tool_raises_the_http_error_type(self) -> None:
        with self.assertRaises(McpConnectionError):
            self.client.call_schematic_read_tool(
                str(self.directory), sch_file=str(self.sheet), name="no_such_tool", arguments={},
            )
        with self.assertRaises(McpConnectionError):
            self.client.call_schematic_write_tool(
                str(self.directory), sch_file=str(self.sheet), name="no_such_tool", arguments={},
            )

    def test_a_failing_tool_raises_with_the_tools_message(self) -> None:
        with self.assertRaises(McpConnectionError) as caught:
            self.client.call_schematic_write_tool(
                str(self.directory), sch_file=str(self.sheet), name="sch_add_wire", arguments={"bad": 1},
            )
        self.assertIn("x1_mm", str(caught.exception))

    def test_a_write_reaches_the_file(self) -> None:
        before = self.sheet.read_text(encoding="utf-8").count("(wire")
        self.client.add_schematic_wire(
            str(self.directory), sch_file=str(self.sheet), x1_mm=100.0, y1_mm=100.0, x2_mm=110.0, y2_mm=100.0,
        )
        self.assertEqual(self.sheet.read_text(encoding="utf-8").count("(wire"), before + 1)

    def test_works_from_inside_a_running_event_loop(self) -> None:
        import asyncio

        async def inside() -> dict:
            return self.client.call_schematic_read_tool(
                str(self.directory), sch_file=str(self.sheet), name="sch_list_sheets", arguments={},
            )

        self.assertEqual(set(asyncio.run(inside())), READ_SHAPE)


if __name__ == "__main__":
    unittest.main()
