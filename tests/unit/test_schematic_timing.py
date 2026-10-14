import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.schematic import timing


class TimingTests(unittest.TestCase):
    def setUp(self) -> None:
        timing.reset()

    def test_disabled_records_nothing(self) -> None:
        with patch.dict(os.environ, {"KICAD_AI_TIMING": ""}):
            with timing.stage("a"):
                timing.count("tool", 1.0)
            with tempfile.TemporaryDirectory() as tmp:
                timing.dump(Path(tmp) / "t.json")
                self.assertFalse((Path(tmp) / "t.json").exists())
            self.assertEqual(timing.snapshot()["stages"], {})
            self.assertEqual(timing.snapshot()["mcp_tools"], {})

    def test_nesting_aggregation_and_dump(self) -> None:
        with patch.dict(os.environ, {"KICAD_AI_TIMING": "1"}):
            with timing.stage("trial:01-compact"):
                for _ in range(2):
                    with timing.stage("placement"):
                        timing.count("sch_move_symbol", 0.5)
                        timing.count("sch_move_symbol", 0.25)
                        self.assertEqual(timing.current(), "trial:01-compact/placement")
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "t.json"
                timing.dump(out)
                data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["stages"]["trial:01-compact/placement"]["calls"], 2)
        self.assertAlmostEqual(data["stages"]["trial:01-compact/placement"]["mcp_seconds"], 1.5)
        self.assertEqual(data["stages"]["trial:01-compact"]["calls"], 1)
        self.assertEqual(data["mcp_tools"]["sch_move_symbol"], {"seconds": 1.5, "calls": 4})
        self.assertGreater(data["wall_seconds"], 0)

    def test_exception_closes_inner_stages(self) -> None:
        with patch.dict(os.environ, {"KICAD_AI_TIMING": "1"}):
            with self.assertRaises(ValueError):
                with timing.stage("a"), timing.stage("b"):
                    raise ValueError
            self.assertEqual(timing.current(), "")
            self.assertEqual(set(timing.snapshot()["stages"]), {"a", "a/b"})


if __name__ == "__main__":
    unittest.main()
