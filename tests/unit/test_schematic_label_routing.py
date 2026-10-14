"""Net labels go through the checked planner when the layout policy asks for tight terminals."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from backend.schematic import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.direct_workflow import DirectWorkflow
from backend.schematic.layout_iteration import LayoutPolicy


def labelled_intent(scope: str = "local"):
    """Two parts joined by one explicitly labelled net, so only the label path runs."""
    net = NetIntent("UART_TX", (PinEndpoint("U1", "20"), PinEndpoint("J2", "2")), routing="label", scope=scope)
    return SchematicIntent(
        symbols=(SymbolIntent("U1", "Device", "R", "MCU"), SymbolIntent("J2", "Device", "R", "UART")),
        nets=(net,),
    ), net


def run_route(policy, scope: str = "local"):
    intent, net = labelled_intent(scope)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workflow = object.__new__(DirectWorkflow)
        workflow._artifact_root = root
        workflow._write = Mock(return_value={"status": "OK"})
        workflow._assert_authoring_result = Mock()
        if policy is not None:
            workflow._layout_policy = policy
        workflow._route_semantic_nets(root, root / "unused.kicad_sch", intent)
        calls = [(call.args[2], call.args[3]) for call in workflow._write.call_args_list]
    return net, [call for call in calls if call[0] in ("sch_add_pin_labels", "sch_add_pin_net_labels")]


class LabelToolSelectionTests(unittest.TestCase):
    def test_tight_terminals_use_the_checked_label_tool(self):
        net, calls = run_route(LayoutPolicy.tight())
        self.assertEqual([name for name, _ in calls], ["sch_add_pin_net_labels"])
        payload = calls[0][1]
        self.assertEqual(payload["label_kind"], "local")
        self.assertEqual(
            [(c["reference"], c["pin"], c["net"]) for c in payload["connections"]],
            [(e.reference, e.pin, net.name) for e in net.endpoints],
        )
        self.assertNotIn("stub_mm", payload)       # the planner chooses the stub, not a caller-supplied length

    def test_the_global_scope_is_passed_through(self):
        _net, calls = run_route(LayoutPolicy.tight(), scope="global")
        self.assertEqual(calls[0][1]["label_kind"], "global")

    def test_the_legacy_layout_keeps_mcp_pros_label_tool(self):
        for policy in (None, LayoutPolicy.legacy()):
            _net, calls = run_route(policy)
            self.assertEqual([name for name, _ in calls], ["sch_add_pin_labels"], policy)
            self.assertEqual(calls[0][1]["stub_mm"], 5.08)

    def test_the_guarded_fallback_on_mcp_pros_boxes_also_uses_the_checked_tool(self):
        _net, calls = run_route(LayoutPolicy.legacy_with_tight_terminals())
        self.assertEqual([name for name, _ in calls], ["sch_add_pin_net_labels"])


if __name__ == "__main__":
    unittest.main()
