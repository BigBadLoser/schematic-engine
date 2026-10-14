"""Symbol libraries are read once, and what is worked out from them is remembered."""
from __future__ import annotations

import os
import tempfile
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.kicad import library_cache

NEEDS_MCP_PRO = unittest.skipUnless(importlib.util.find_spec("kicad_mcp"), "needs kicad-mcp-pro (it parses the sheet)")


def module_for(library: Path):
    """A stand-in for MCP Pro's schematic module: a library lookup and the questions answered from a library."""
    calls = {"bounds": 0, "pins": 0}

    def lookup(name: str):
        return library if name == "Device" else None

    def bounds(library_name, symbol, x, y, rotation=0, unit=1):
        calls["bounds"] += 1
        return (x - 1.0, y - 2.0, x + 1.0, y + 2.0)

    def pins(library_name, symbol, x, y, rotation=0, unit=1, mirror=""):
        calls["pins"] += 1
        return {"1": (x, y - 3.81), "2": (x, y + 3.81)}

    def normals(library_name, symbol, rotation=0, unit=1, mirror=""):
        calls["pins"] += 1
        return {"1": (0.0, -1.0), "2": (0.0, 1.0)}

    return SimpleNamespace(
        _symbol_library_file=lookup, get_symbol_primitive_bounds=bounds, get_pin_positions=pins,
        get_pin_outward_normals=normals, calls=calls,
    )


class LibraryCacheTests(unittest.TestCase):
    def setUp(self):
        library_cache.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.library = Path(self.directory.name) / "Device.kicad_sym"
        self.library.write_text("(kicad_symbol_lib)", encoding="utf-8")

    def test_a_library_is_read_from_disk_once(self):
        module = module_for(self.library)
        library_cache.install(module)
        reads = []
        original = Path.read_text

        def counting(path, *args, **kwargs):
            reads.append(str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", counting):
            first = module._symbol_library_file("Device").read_text(encoding="utf-8", errors="ignore")
            second = module._symbol_library_file("Device").read_text(encoding="utf-8", errors="ignore")
        self.assertEqual(first, second)
        self.assertEqual(len(reads), 1)

    def test_a_library_that_changed_on_disk_is_read_again(self):
        module = module_for(self.library)
        library_cache.install(module)
        before = module._symbol_library_file("Device").read_text(encoding="utf-8")
        self.library.write_text("(kicad_symbol_lib (version 2))", encoding="utf-8")
        stamp = self.library.stat().st_mtime_ns + 2_000_000_000
        os.utime(self.library, ns=(stamp, stamp))
        after = module._symbol_library_file("Device").read_text(encoding="utf-8")
        self.assertNotEqual(before, after)

    def test_an_unknown_library_is_still_none(self):
        module = module_for(self.library)
        library_cache.install(module)
        self.assertIsNone(module._symbol_library_file("Nothing"))

    def test_an_answer_derived_from_a_library_is_worked_out_once(self):
        module = module_for(self.library)
        library_cache.install(module)
        for _ in range(5):
            self.assertEqual(module.get_pin_positions("Device", "R", 10.0, 20.0, 90), {"1": (10.0, 16.19), "2": (10.0, 23.81)})
            module.get_symbol_primitive_bounds("Device", "R", 10.0, 20.0, 0, 1)
        self.assertEqual(module.calls, {"bounds": 1, "pins": 1})

    def test_a_different_question_is_a_different_answer(self):
        module = module_for(self.library)
        library_cache.install(module)
        module.get_pin_positions("Device", "R", 10.0, 20.0, 0)
        module.get_pin_positions("Device", "R", 10.0, 20.0, 90)
        module.get_pin_positions("Device", "R", 12.0, 20.0, 0)
        module.get_pin_positions("Device", "R", 10.0, 20.0, 0, mirror="x")
        self.assertEqual(module.calls["pins"], 4)

    def test_what_comes_back_may_be_changed_without_changing_what_is_remembered(self):
        module = module_for(self.library)
        library_cache.install(module)
        first = module.get_pin_positions("Device", "R", 0.0, 0.0)
        first["1"] = (99.0, 99.0)
        self.assertEqual(module.get_pin_positions("Device", "R", 0.0, 0.0)["1"], (0.0, -3.81))

    def test_installing_twice_changes_nothing_more(self):
        module = module_for(self.library)
        library_cache.install(module)
        once = (module._symbol_library_file, module.get_pin_positions, module.get_symbol_primitive_bounds)
        library_cache.install(module)
        self.assertEqual(once, (module._symbol_library_file, module.get_pin_positions, module.get_symbol_primitive_bounds))

    def test_a_symbol_in_no_known_library_is_asked_again_not_remembered_as_an_answer(self):
        module = module_for(self.library)
        library_cache.install(module)
        module.get_pin_positions("Missing", "R", 0.0, 0.0)
        module.get_pin_positions("Missing", "R", 0.0, 0.0)
        self.assertEqual(module.calls["pins"], 1)           # the answer (whatever it is) is the same each time


@NEEDS_MCP_PRO
class InstalledInTheServerTests(unittest.TestCase):
    def test_the_extended_server_installs_it_on_the_real_mcp_pro_module(self):
        import kicad_mcp.tools.schematic as schematic

        library_cache.install(schematic)
        self.assertTrue(getattr(schematic.get_pin_positions, "_library_cached", False))
        self.assertTrue(getattr(schematic._symbol_library_file, "_library_cached", False))
        found = schematic.get_pin_positions("Device", "R", 0.0, 0.0)
        self.assertEqual(sorted(found), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
