"""A tool the engine calls by name must be on the client's allow-list, or the stage that calls it fails at run time."""
from __future__ import annotations

import re
from pathlib import Path
from unittest import TestCase

from backend.kicad.mcp_client import GUARDED_WRITE_TOOLS, READ_ONLY_TOOLS

ROOT = Path(__file__).resolve().parents[2]
SOURCES = sorted((ROOT / "backend" / "schematic").glob("*.py"))
CALL = re.compile(r'(?:self|workflow)\._(write|read)\(\s*root,\s*schematic_file,\s*"([a-z_]+)"', re.S)


def called() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for source in SOURCES:
        for kind, name in CALL.findall(source.read_text(encoding="utf-8")):
            found.setdefault((kind, name), source.name)
    return found


class EngineToolAllowListTests(TestCase):
    """`auto-v25`: the paper was to be grown with `sch_set_sheet_size`, which the guarded client refuses unless it is listed."""

    def test_the_scan_finds_the_calls(self):
        names = {name for _kind, name in called()}
        self.assertIn("sch_move_symbol", names)
        self.assertIn("sch_set_sheet_size", names)
        self.assertIn("sch_visual_qa", names)

    def test_every_tool_the_engine_writes_with_is_a_guarded_write_tool(self):
        refused = {name: source for (kind, name), source in called().items() if kind == "write" and name not in GUARDED_WRITE_TOOLS}
        self.assertEqual(refused, {})

    def test_every_tool_the_engine_reads_with_is_a_read_only_tool(self):
        refused = {name: source for (kind, name), source in called().items() if kind == "read" and name not in READ_ONLY_TOOLS}
        self.assertEqual(refused, {})
