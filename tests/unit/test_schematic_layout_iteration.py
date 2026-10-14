"""Per-symbol room, and the settings of a layout policy."""
from __future__ import annotations

import unittest

from backend.schematic.layout_iteration import LOOSE_MARGIN_MM, LayoutPolicy


class LayoutPolicyTests(unittest.TestCase):
    def test_legacy_policy_has_nothing_to_loosen(self):
        self.assertIsNone(LayoutPolicy.legacy().loosened({"U1"}))
        self.assertEqual(LayoutPolicy.legacy().as_arguments()["mode"], "legacy")

    def test_loosening_raises_only_the_named_symbols(self):
        policy = LayoutPolicy.tight(1.27).loosened({"U1", "J3"})
        self.assertEqual(policy.margin("U1"), 2.54)
        self.assertEqual(policy.margin("J3"), 2.54)
        self.assertEqual(policy.margin("R1"), 1.27)
        self.assertEqual(dict(policy.margins), {"J3": 2.54, "U1": 2.54})

    def test_loosening_stops_at_the_ceiling(self):
        policy = LayoutPolicy.tight(1.27)
        for _ in range(2):
            policy = policy.loosened({"U1"})
        self.assertEqual(policy.margin("U1"), LOOSE_MARGIN_MM)
        self.assertIsNone(policy.loosened({"U1"}))

    def test_with_no_symbols_named_the_whole_sheet_loosens(self):
        policy = LayoutPolicy.tight(1.27).loosened({"U1"}).loosened(set())
        self.assertEqual(policy.default_mm, 2.54)
        self.assertEqual(policy.margin("U1"), 2.54)
        self.assertEqual(dict(policy.margins), {})       # a margin equal to the default carries no information

    def test_equal_policies_are_equal_whatever_the_route_to_them(self):
        a = LayoutPolicy.tight(1.27).loosened({"U1"}).loosened({"J3"})
        b = LayoutPolicy.tight(1.27).loosened({"J3"}).loosened({"U1"})
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_tightening_takes_room_from_the_named_symbols_down_to_a_floor(self):
        policy = LayoutPolicy.tight(1.27).tightened({"R2"})
        self.assertEqual(policy.margin("R2"), 0.0)
        self.assertIsNone(policy.tightened({"R2"}))

    def test_arguments_for_the_server_tool(self):
        arguments = LayoutPolicy.tight(1.27).loosened({"U1"}).as_arguments()
        self.assertEqual(arguments, {
            "mode": "tight", "default_mm": 1.27, "by_reference": {"U1": 2.54}, "tight_terminals": True,
        })

    def test_the_terminal_stage_is_tight_unless_the_layout_is_legacy_all_the_way_down(self):
        self.assertFalse(LayoutPolicy.legacy().tight_terminals)
        self.assertTrue(LayoutPolicy.tight(1.27).tight_terminals)
        fallback = LayoutPolicy.legacy_with_tight_terminals()
        self.assertEqual((fallback.mode, fallback.tight_terminals), ("legacy", True))
        self.assertNotEqual(fallback, LayoutPolicy.legacy())

    def test_changes_to_a_policy_keep_its_other_settings(self):
        policy = LayoutPolicy.tight(1.27).with_stub_obstacles(True).loosened({"U1"})
        self.assertTrue(policy.stub_obstacles)
        self.assertTrue(policy.tight_terminals)
        self.assertTrue(LayoutPolicy.tight(1.27).with_stub_obstacles(True).loosened(set()).stub_obstacles)
        self.assertTrue(LayoutPolicy.tight(1.27).tightened({"R2"}).tight_terminals)

    def test_invalid_policies_are_refused(self):
        with self.assertRaises(ValueError):
            LayoutPolicy(mode="loose")
        with self.assertRaises(ValueError):
            LayoutPolicy(mode="tight", default_mm=-1.0)


class PolicySettingsTests(unittest.TestCase):
    def test_inference_is_on_by_default_and_a_policy_without_it_is_a_different_policy(self):
        policy = LayoutPolicy.tight(1.27)
        self.assertTrue(policy.infer_relations)
        self.assertNotEqual(policy, policy.without_inference())
        self.assertIsNone(policy.without_inference().without_inference())
        self.assertFalse(policy.without_inference().describe()["infer_relations"])

    def test_the_other_ways_a_policy_changes_keep_the_inference_it_has(self):
        policy = LayoutPolicy.tight(1.27).without_inference()
        self.assertFalse(policy.loosened({"U1"}).infer_relations)
        self.assertFalse(policy.loosened(set()).infer_relations)
        self.assertFalse(policy.with_stub_obstacles(True).infer_relations)

    def test_refinement_is_on_by_default_and_a_policy_without_it_is_a_different_policy(self):
        policy = LayoutPolicy.tight(1.27)
        self.assertTrue(policy.refine)
        self.assertNotEqual(policy, policy.without_refinement())
        self.assertIsNone(policy.without_refinement().without_refinement())
        self.assertFalse(policy.without_refinement().describe()["refine"])

    def test_other_changes_keep_the_refinement_setting(self):
        policy = LayoutPolicy.tight(1.27).without_refinement()
        self.assertFalse(policy.loosened({"U1"}).refine)
        self.assertFalse(policy.with_stub_obstacles(True).refine)
        self.assertFalse(policy.without_inference().refine)


if __name__ == "__main__":
    unittest.main()
