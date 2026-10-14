"""Regress presentation-safe power connections without MCP Pro or KiCad."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from backend.schematic import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.direct_workflow import DirectWorkflow


def power_intent() -> SchematicIntent:
    """A part on a supply rail and on ground, each rail carrying a PWR_FLAG authoring auxiliary."""
    return SchematicIntent(
        symbols=(
            SymbolIntent("U1", "Device", "R", "MCU"),
            SymbolIntent("C1", "Device", "C", "100nF"),
            SymbolIntent("#FLG01", "power", "PWR_FLAG", "PWR_FLAG", authoring_auxiliary=True),
            SymbolIntent("#FLG02", "power", "PWR_FLAG", "PWR_FLAG", authoring_auxiliary=True),
        ),
        nets=(
            NetIntent("+3V3", (PinEndpoint("U1", "1"), PinEndpoint("C1", "1"), PinEndpoint("#FLG01", "1")),
                      kind="power", routing="label", scope="global"),
            NetIntent("GND", (PinEndpoint("U1", "2"), PinEndpoint("C1", "2"), PinEndpoint("#FLG02", "1")),
                      kind="ground", routing="label", scope="global"),
        ),
    )


def workflow_with(root: Path, **state: object) -> DirectWorkflow:
    """A workflow whose MCP tool calls are recorded and whose authoring results are always accepted."""
    workflow = object.__new__(DirectWorkflow)
    workflow._artifact_root = root
    workflow._write = Mock(return_value={"status": "OK"})
    workflow._assert_authoring_result = Mock()
    for name, value in state.items():
        setattr(workflow, name, value)
    return workflow


class PowerTerminalRoutingTests(unittest.TestCase):
    def test_power_flags_use_visible_terminal_stubs_like_other_endpoints(self) -> None:
        intent = power_intent()
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            # Route authoring is mocked: this tests the integration contract,
            # not MCP's placement solver or electrical verification.
            # A layout whose flags do not ride on a wire of their net gives each its own terminal.
            workflow = workflow_with(
                artifact_root, _layout_policy=SimpleNamespace(tight_terminals=False),
                _bring_flags_within_reach=Mock(return_value=[]),
            )

            workflow._route_semantic_nets(
                artifact_root,
                artifact_root / "unused.kicad_sch",
                intent,
            )

            calls = [
                (call.args[2], call.args[3])
                for call in workflow._write.call_args_list
            ]
            tool_names = [name for name, _payload in calls]
            self.assertNotIn("sch_add_pin_power_symbols", tool_names)
            payloads = [
                payload
                for name, payload in calls
                if name == "sch_add_pin_power_terminals"
            ]
            self.assertTrue(payloads)
            connections = [
                connection
                for payload in payloads
                for connection in payload["connections"]
            ]
            self.assertEqual(
                1,
                sum(
                    c["reference"] == "#FLG01" and c["net"] == "+3V3"
                    for c in connections
                ),
            )
            self.assertEqual(
                1,
                sum(
                    c["reference"] == "#FLG02" and c["net"] == "GND"
                    for c in connections
                ),
            )
            self.assertTrue(
                all(payload["clearance_mm"] > 0 for payload in payloads)
            )
            self.assertTrue(
                any(not c["reference"].startswith("#") for c in connections)
            )
            report = json.loads(
                (artifact_root / "routing-policy.json").read_text(
                    encoding="utf-8"
                )
            )
            for net in ("+3V3", "GND"):
                decision = next(
                    item for item in report["decisions"] if item["net"] == net
                )
                self.assertEqual(
                    ["wire_stub_power_terminal"],
                    sorted(
                        {link["source"] for link in decision["auxiliary_links"]}
                    ),
                )

    def test_with_the_tight_layout_a_flag_rides_on_a_wire_of_its_net(self) -> None:
        intent = power_intent()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = workflow_with(
                root, _layout_policy=SimpleNamespace(tight_terminals=True),
                _bring_flags_within_reach=Mock(return_value=[]),
            )
            workflow._route_semantic_nets(root, root / "unused.kicad_sch", intent)

            calls = [(call.args[2], call.args[3]) for call in workflow._write.call_args_list]
            terminals = [c for name, payload in calls if name == "sch_add_pin_power_terminals" for c in payload["connections"]]
            self.assertEqual(sorted(c["reference"] for c in terminals), ["C1", "C1", "U1", "U1"])    # parts only
            attached = [payload for name, payload in calls if name == "sch_attach_power_flags"]
            self.assertEqual(
                sorted(flag["reference"] for payload in attached for flag in payload["flags"]), ["#FLG01", "#FLG02"],
            )
            report = json.loads((root / "routing-policy.json").read_text(encoding="utf-8"))
            for decision in report["decisions"]:
                self.assertEqual({link["source"] for link in decision["auxiliary_links"]}, {"attached_to_net_wire"})

    def test_a_negative_rail_asks_for_a_hanging_symbol(self) -> None:
        intent = SchematicIntent(
            symbols=(SymbolIntent("U1", "Device", "R", "1k"), SymbolIntent("U2", "Device", "R", "1k")),
            nets=(
                NetIntent("VEE", (PinEndpoint("U1", "4"),), kind="power", routing="label", scope="global",
                          polarity="negative"),
                NetIntent("VCC", (PinEndpoint("U2", "7"),), kind="power", routing="label", scope="global"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = workflow_with(root)
            workflow._route_semantic_nets(root, root / "unused.kicad_sch", intent)
            entries = {
                c["net"]: c
                for call in workflow._write.call_args_list if call.args[2] == "sch_add_pin_power_terminals"
                for c in call.args[3]["connections"]
            }
            self.assertTrue(entries["VEE"].get("hanging", False))
            self.assertNotIn("hanging", entries["VCC"])

    def test_router_is_told_which_flags_ride(self) -> None:
        for riding in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                intent = SchematicIntent(
                    symbols=(SymbolIntent("R1", "Device", "R", "1k"), SymbolIntent("R2", "Device", "R", "1k"),
                             SymbolIntent("#FLG1", "power", "PWR_FLAG", "PWR_FLAG", authoring_auxiliary=True)),
                    nets=(NetIntent("SIG", (PinEndpoint("R1", "1"), PinEndpoint("R2", "1"))),
                          NetIntent("GND", (PinEndpoint("R1", "2"), PinEndpoint("R2", "2"), PinEndpoint("#FLG1", "1")),
                                    kind="ground", routing="label", scope="global")),
                )
                workflow = workflow_with(
                    root, _layout_policy=SimpleNamespace(tight_terminals=riding),
                    _bring_flags_within_reach=Mock(return_value=[]),     # reads the sheet file, which is not there
                )
                workflow._route_semantic_nets(root, root / "unused.kicad_sch", intent)
                expected = workflow._power_flags_that_ride(intent)
                routed = [c.args[3] for c in workflow._write.call_args_list if c.args[2] == "sch_route_relation_connection"]
                self.assertTrue(routed, riding)
                if riding:
                    self.assertTrue(expected)
                    self.assertTrue(all(p["ignore_references"] == expected for p in routed))
                else:
                    self.assertEqual([], expected)
                    self.assertTrue(all("ignore_references" not in p for p in routed))

    def test_ignored_symbols_are_not_router_obstacles(self) -> None:
        from backend.kicad.mcp_extended_server import routing_obstacle_refs

        self.assertEqual(["R1", "U1"], routing_obstacle_refs({"R1", "#FLG1"}, {"U1"}, ["#FLG1"]))
        self.assertEqual(["#FLG1", "R1"], routing_obstacle_refs({"R1", "#FLG1"}, set(), None))
        self.assertEqual(["#FLG1"], routing_obstacle_refs(set(), {"#FLG1"}, ["#FLG1"]))


if __name__ == "__main__":
    unittest.main()
