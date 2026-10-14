"""Run the placement solvers of a recorded run again: the pure parts (no engine run, no MCP Pro, no KiCad)."""
from __future__ import annotations

import pickle
import sys
import tempfile
import unittest
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import placement_replay  # noqa: E402
from backend.schematic.orientation import Orientation  # noqa: E402
from backend.schematic.placement_refinement import (  # noqa: E402
    Connection, OrientedGeometry, PinGeometry, PlacedSymbol, rotate_geometry,
)

UP, DOWN, LEFT, RIGHT = (0.0, -1.0), (0.0, 1.0), (-1.0, 0.0), (1.0, 0.0)


def upright_transistor() -> OrientedGeometry:
    pins = {"1": PinGeometry((-5.08, 0.0), LEFT), "2": PinGeometry((0.0, -5.08), UP), "3": PinGeometry((0.0, 5.08), DOWN)}
    return OrientedGeometry(pins, (-5.08, -5.08, 3.0, 5.08))


def circuit() -> dict[str, dict]:
    """A transistor whose source points down, and an inductor to its right and above it: turned, one straight wire."""
    transistor = PlacedSymbol(
        "Q1", (40.64, 50.8), Orientation(0), {Orientation(0): upright_transistor()}, movable=True,
    )
    inductor = PlacedSymbol(
        "L1", (76.2, 38.1), Orientation(0),
        {Orientation(0): OrientedGeometry({"1": PinGeometry((-3.81, 0.0), LEFT)}, (-3.81, -1.0, 3.81, 1.0))},
    )
    nets = [Connection("SW", (("Q1", "3"), ("L1", "1")), "wire")]
    refine = {"solver": "refine", "args": ([transistor, inductor], nets), "kwargs": {}}
    return {"refine": refine}


def write_dumps(directory: Path, calls: list[tuple[str, dict]]) -> None:
    (directory / "dumps").mkdir(parents=True)
    for number, (name, call) in enumerate(calls):
        (directory / "dumps" / f"placement-{number:03d}-{name}-{1000 + number}.pkl").write_bytes(pickle.dumps(call))


class LoadTests(unittest.TestCase):
    def test_the_calls_of_each_circuit_are_read_back_together_in_the_order_they_were_made(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            calls = [(name, {"solver": name, "args": (), "kwargs": {}}) for name in ("seed", "align", "refine") * 2]
            write_dumps(run, calls)
            found = placement_replay.load_circuits(run)
            self.assertEqual(len(found), 2)
            self.assertEqual(sorted(found[0]), ["align", "refine", "seed"])

    def test_a_circuit_the_seeding_skipped_still_starts_a_group(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            write_dumps(run, [(name, {"solver": name, "args": (), "kwargs": {}}) for name in ("align", "refine", "align", "refine")])
            self.assertEqual([sorted(c) for c in placement_replay.load_circuits(run)], [["align", "refine"]] * 2)

    def test_circuits_built_side_by_side_are_told_apart_by_their_references(self):
        """With `--jobs N` the dumps of different fixtures interleave in time."""
        def symbol(reference: str) -> PlacedSymbol:
            return PlacedSymbol(reference, (0.0, 0.0), Orientation(0), {Orientation(0): upright_transistor()})

        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            calls = []
            for name in ("seed", "align", "refine"):
                for reference in ("Q1", "Q2"):
                    calls.append((name, {"solver": name, "args": ([symbol(reference)], []), "kwargs": {}}))
            write_dumps(run, calls)
            found = placement_replay.load_circuits(run)
            self.assertEqual(len(found), 2)
            for circuit in found:
                self.assertEqual(sorted(circuit), ["align", "refine", "seed"])
                self.assertEqual(len({c["args"][0][0].reference for c in circuit.values()}), 1)

    def test_a_run_without_dumps_says_which_overlay_to_use(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FileNotFoundError, "e11_placement_inputs"):
                placement_replay.load_circuits(Path(directory))


class ReplayTests(unittest.TestCase):
    def test_a_part_of_three_pins_can_be_given_every_orientation(self):
        symbols = placement_replay.widen_orientations(circuit()["refine"]["args"][0], 4)
        self.assertEqual(len(symbols[0].geometry), 12)          # the transistor
        self.assertEqual(len(symbols[1].geometry), 1)           # the inductor is not movable

    def test_the_replay_turns_the_part_when_it_is_allowed_to_and_not_when_it_is_not(self):
        fixed = placement_replay.replay(circuit(), orient=0)
        widened = placement_replay.replay(circuit(), orient=4)
        self.assertGreater(fixed.rows[-1].bends, 0)             # upright, the source points away from the inductor
        self.assertEqual(widened.rows[-1].bends, 0)
        self.assertLess(widened.rows[-1].total, fixed.rows[-1].total)

    def test_weights_can_be_changed_by_name(self):
        self.assertEqual(placement_replay.parse_weights("bend=8, extent=0.1"), {"bend": 8.0, "extent": 0.1})
        self.assertEqual(placement_replay.parse_weights(None), {})
        costly = placement_replay.replay(circuit(), weights={"bend": 100.0}, orient=0)
        cheap = placement_replay.replay(circuit(), weights={"bend": 0.0}, orient=0)
        self.assertLessEqual(costly.rows[-1].bends, cheap.rows[-1].bends)

    def test_the_report_and_the_drawing_name_every_symbol(self):
        result = placement_replay.replay(circuit(), orient=4)
        text = placement_replay.format_replay(result)
        self.assertIn("Q1", text)
        self.assertIn("L1", text)
        drawing = placement_replay.svg(result, "after")
        self.assertTrue(drawing.startswith("<svg"))
        self.assertIn("polyline", drawing)


if __name__ == "__main__":
    unittest.main()
