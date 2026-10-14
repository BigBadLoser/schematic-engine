from __future__ import annotations

import unittest

from backend.kicad.cpsat_relation_solver import (
    PlacementCandidate,
    solve_relation_group,
)


class CpSatRelationSolverTests(unittest.TestCase):
    def test_members_must_choose_same_junction(self) -> None:
        result = solve_relation_group(
            required_members=("R1", "SW1"),
            junction_costs={"a": 0, "b": 10},
            candidates=[
                PlacementCandidate("r_a", "R1", "a", 1),
                PlacementCandidate("r_b", "R1", "b", 0),
                PlacementCandidate("s_a", "SW1", "a", 1),
                PlacementCandidate("s_b", "SW1", "b", 0),
            ],
        )
        self.assertEqual(result.junction_id, "a")
        self.assertEqual(set(result.candidate_ids), {"r_a", "s_a"})

    def test_pairwise_conflict_forces_alternate_rotation(self) -> None:
        result = solve_relation_group(
            required_members=("R1", "SW1"),
            junction_costs={"j": 0},
            candidates=[
                PlacementCandidate("r0", "R1", "j", 0),
                PlacementCandidate("r1", "R1", "j", 4),
                PlacementCandidate("s0", "SW1", "j", 0),
                PlacementCandidate("s1", "SW1", "j", 5),
            ],
            incompatible_pairs=[("r0", "s0")],
        )
        self.assertNotEqual(set(result.candidate_ids), {"r0", "s0"})
        self.assertEqual(len(result.candidate_ids), 2)

    def test_infeasible_group_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "infeasible"):
            solve_relation_group(
                required_members=("R1", "SW1"),
                junction_costs={"j": 0},
                candidates=[
                    PlacementCandidate("r", "R1", "j", 0),
                    PlacementCandidate("s", "SW1", "j", 0),
                ],
                incompatible_pairs=[("r", "s")],
            )

    def test_member_with_all_candidates_filtered_out_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "SW1 has no candidates"):
            solve_relation_group(
                required_members=("R1", "SW1"),
                junction_costs={"j": 0},
                candidates=[PlacementCandidate("r", "R1", "j", 0)],
            )

    def test_unrequested_member_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected members: SW1"):
            solve_relation_group(
                required_members=("R1",),
                junction_costs={"j": 0},
                candidates=[PlacementCandidate("s", "SW1", "j", 0)],
            )

    def test_members_without_a_common_feasible_junction_raise(self) -> None:
        with self.assertRaisesRegex(ValueError, "infeasible"):
            solve_relation_group(
                required_members=("R1", "SW1"),
                junction_costs={"a": 0, "b": 0},
                candidates=[
                    PlacementCandidate("r", "R1", "a", 0),
                    PlacementCandidate("s", "SW1", "b", 0),
                ],
            )


if __name__ == "__main__":
    unittest.main()

