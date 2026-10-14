"""The schematic workflow the direct build runs on: the stage methods that draw a finished placement, and nothing else.

``build_direct`` (``direct_build.py``) places every part once, from a placer's answer, and then runs the stages after
placement: no-connect marks, routing of every net, text fields, net names, and the checks. This class is those stages. It
owns the connection to the MCP Pro server (``_write`` / ``_read``), the layout policy the server is told about, and the
acceptance checks (visual QA, connectivity gate, ERC, the exported netlist).

It was moved out of the classes of the retired trial loop (``McpProSchematicWorkflow``, ``SemanticGrammarScratchWorkflow``). The
direct path builds with the trial loop's ``compact`` profile only (guarded routing, tight power terminals, text fields and
net names written), so the profile checks of those classes are settled here and gone.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from backend.kicad.mcp_client import McpHttpClient
from backend.kicad.netlist import KiCadCliNetlistExporter
from backend.kicad.service import KiCadService

from . import timing
from .designators import split_unit, unit_reference
from .field_placement import plan_fields
from .intent import NetIntent, PinEndpoint, SchematicIntent
from .kicad_geometry import measured_scene_from_kicad, text_width_mm
from .layout_iteration import TIGHT_MARGIN_MM, LayoutPolicy
from .policy import DEFAULT_AUTHORING_POLICY, SchematicAuthoringPolicy
from .presentation_geometry import TEXT_KINDS as TEXT_KINDS_FOR_LABELS
from .shared_net_routing import plan_shared_net


@dataclass(frozen=True)
class SchematicWorkflowResult:
    references: tuple[str, ...]
    build: dict[str, Any]
    placement: dict[str, Any]
    flow_placements: tuple[dict[str, Any], ...]
    block_placements: tuple[dict[str, Any], ...]
    routes: tuple[dict[str, Any], ...]
    labels: tuple[dict[str, Any], ...]
    annotations: tuple[dict[str, Any], ...]
    junctions: dict[str, Any]
    fields: dict[str, Any]
    readability: dict[str, Any]
    visual_qa: dict[str, Any]
    cosmetic_score: dict[str, Any]
    cosmetic_fixes: tuple[dict[str, Any], ...]
    connectivity_gate: dict[str, Any] | None
    erc: dict[str, Any] | None
    netlist_verification: dict[str, Any]
    before_sha256: str
    after_sha256: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["references"] = list(self.references)
        payload["flow_placements"] = list(self.flow_placements)
        payload["block_placements"] = list(self.block_placements)
        payload["routes"] = list(self.routes)
        payload["labels"] = list(self.labels)
        payload["annotations"] = list(self.annotations)
        return payload


def _is_authoring_auxiliary(intent: SchematicIntent, reference: str) -> bool:
    symbol = next(item for item in intent.symbols if item.reference == reference)
    return symbol.authoring_auxiliary



# --- timing (KICAD_AI_TIMING=1; no-ops otherwise): stages are named at the method boundaries that already exist ------

TIMED_METHODS: dict[str, str] = {
    "_rebuild_at_mcp_placements": "placement/rebuild",
    "_apply_no_connects": "no_connects",
    "_route_semantic_nets": "routing",
    "_place_fields": "fields",
    "_run_cosmetic_loop": "cosmetic",
    "_verify_references": "checks/references",
}
# MCP tools that name a stage of their own inside whichever method calls them
TIMED_TOOLS: dict[str, str] = {
    "sch_add_pin_power_terminals": "power_flags", "sch_attach_power_flags": "power_flags",
    "sch_normalize_power_orientation": "power_flags",
    "sch_add_pin_labels": "labels", "sch_add_pin_net_labels": "labels", "sch_resolve_label_overlaps": "labels",
    "sch_add_missing_junctions": "junctions",
    "sch_visual_qa": "checks", "schematic_connectivity_gate": "checks", "run_erc": "checks",
    "sch_cosmetic_score": "checks",
}


def _timed_method(method: Any, stage_name: str) -> Any:
    @wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not timing.enabled() or timing.current().rsplit("/", 1)[-1] == stage_name.rsplit("/", 1)[-1]:
            return method(*args, **kwargs)    # off, or an override reached through super()
        with timing.stage(stage_name):
            return method(*args, **kwargs)
    return wrapper


def _timed_mcp(method: Any) -> Any:
    @wraps(method)
    def wrapper(self: Any, root: Path, schematic_file: Path, name: str, arguments: dict[str, Any]) -> Any:
        if not timing.enabled():
            return method(self, root, schematic_file, name, arguments)
        stage_name = TIMED_TOOLS.get(name)
        scope = timing.stage(stage_name) if stage_name and timing.current().rsplit("/", 1)[-1] != stage_name else ExitStack()
        with scope:
            started = time.perf_counter()
            try:
                return method(self, root, schematic_file, name, arguments)
            finally:
                timing.count(name, time.perf_counter() - started)
    return wrapper


class DirectWorkflow:
    """The stages that draw a placed circuit through MCP Pro and check it (``direct_build.build_direct`` runs them)."""

    # The server tools the stages call by name and cannot do without (the sheet is built, placed, routed, named and
    # checked through them). Not listed: `run_erc`, `schematic_connectivity_gate` and `project_set_design_intent`
    # (used when the server has them), the cosmetic fixers (`sch_align_to_grid` and the like, skipped when absent),
    # `sch_attach_power_flags`, `sch_add_wire_labels` and `sch_add_text` (each has a fallback, or the layout stands without).
    REQUIRED_WRITE_TOOLS = {
        "sch_build_circuit",
        "sch_set_layout_clearances",
        "sch_move_symbol",
        "sch_translate_references_to_margin",
        "sch_set_symbol_mirror",
        "sch_modify_field_orientation",
        "sch_mark_no_connect_pins",
        "sch_add_pin_labels",
        "sch_add_pin_net_labels",
        "sch_add_pin_power_terminals",
        "sch_route_relation_connection",
        "sch_add_missing_junctions",
        "sch_normalize_text_sizes",
        "sch_place_fields",
        "sch_set_sheet_size",
    }
    REQUIRED_QA_TOOLS = {
        "sch_get_symbols",
        "sch_measure_functional_blocks",
        "sch_visual_qa",
        "sch_cosmetic_score",
    }

    PAPER_LADDER = ("A4", "A3", "A2", "A1", "A0")
    FLAG_REACH_MM = 10.16
    LABEL_TEXT_MM = 1.27
    CAPTION_TEXT_MM = 2.0

    # The sheet as the stage that failed saw it, kept in the artifact folder (the file transaction puts the input back).
    FAILED_SHEET = "failed-state.kicad_sch"

    def __init__(
        self,
        client: McpHttpClient,
        kicad: KiCadService,
        policy: SchematicAuthoringPolicy = DEFAULT_AUTHORING_POLICY,
        *,
        netlist_exporter: KiCadCliNetlistExporter | None = None,
        artifact_root: Path,
    ) -> None:
        self.client = client
        self.kicad = kicad
        self.policy = policy
        self.netlist_exporter = netlist_exporter or KiCadCliNetlistExporter()
        self._artifact_root = artifact_root

    @staticmethod
    def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
        """Normalize structured MCP results and JSON-encoded tool payloads."""

        def parse_text(value: str) -> dict[str, Any] | None:
            stripped = value.strip()
            if not stripped or stripped[0] not in "[{":
                return None
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                return None
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}

        result_value = result.get("result")
        if isinstance(result_value, str):
            parsed = parse_text(result_value)
            if parsed is not None:
                content_text = result.get("_content_text")
                if isinstance(content_text, str) and content_text.strip():
                    parsed.setdefault("_content_text", content_text)
                return parsed

        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text_value = content[0].get("text")
            if isinstance(text_value, str):
                parsed = parse_text(text_value)
                if parsed is not None:
                    return parsed
                return {"text": text_value}

        return result

    @_timed_mcp
    def _write(
        self,
        root: Path,
        schematic_file: Path,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self._unwrap(
                self.client.call_schematic_write_tool(
                    str(root),
                    sch_file=str(schematic_file),
                    name=name,
                    arguments=arguments,
                )
            )
        except Exception:
            # A failed build runs in a file transaction that puts the input back, so this is the only moment the
            # placement a stage could not live with exists to be kept.
            try:
                shutil.copy2(schematic_file, self._artifact_root / self.FAILED_SHEET)
            except OSError:
                pass
            raise

    @staticmethod
    def _assert_authoring_result(name: str, result: dict[str, Any]) -> None:
        text_value = result.get("text")
        if not isinstance(text_value, str):
            return
        lowered = text_value.lower()
        failure_markers = (
            "reference not found",
            "pin not found",
            "was not found",
            "could not route",
            "no pin labels were added",
            "needs reference, pin, net",
        )
        if any(marker in lowered for marker in failure_markers):
            first_line = next(
                (line.strip() for line in text_value.splitlines() if line.strip()),
                text_value.strip(),
            )
            raise RuntimeError(f"{name} failed: {first_line}")

    @_timed_mcp
    def _read(
        self,
        root: Path,
        schematic_file: Path,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return self._unwrap(
            self.client.call_schematic_read_tool(
                str(root),
                sch_file=str(schematic_file),
                name=name,
                arguments=arguments,
            )
        )

    @staticmethod
    def _symbol_placements_from_listing(
        payload: dict[str, Any],
        expected: set[str],
    ) -> dict[str, dict[str, int | float]]:
        """Parse MCP Pro's authoritative placed-symbol listing.

        Placement is read back from MCP Pro and then fed into a final
        sch_build_circuit call. The companion never computes coordinates.
        """
        text_value = (
            payload.get("text")
            or payload.get("result")
            or payload.get("_content_text")
        )
        if not isinstance(text_value, str):
            raise RuntimeError("MCP Pro symbol placement readback is unavailable")

        pattern = re.compile(
            r"^-\s+(?P<ref>\S+).*?@\s*\("
            r"(?P<x>-?\d+(?:\.\d+)?),\s*(?P<y>-?\d+(?:\.\d+)?)\)"
            r"(?:\s+rot=(?P<rotation>-?\d+))?"
            r"\s+unit=(?P<unit>\d+)"
        )
        found: list[tuple[str, dict[str, int | float]]] = []
        for raw_line in text_value.splitlines():
            match = pattern.match(raw_line.strip())
            if not match:
                continue
            found.append((match.group("ref"), {
                "x_mm": float(match.group("x")),
                "y_mm": float(match.group("y")),
                "rotation": int(match.group("rotation") or 0) % 360,
                "unit": int(match.group("unit")),
            }))
        # a part drawn in several units is several lines under one reference: each is a placeable of its own (``U5:2``)
        lines_of = {reference: sum(1 for ref, _item in found if ref == reference) for reference, _item in found}
        placements: dict[str, dict[str, int | float]] = {}
        for reference, item in found:
            name = unit_reference(reference, int(item["unit"])) if lines_of[reference] > 1 else reference
            if name in expected:
                placements[name] = item

        missing = sorted(expected - placements.keys())
        if missing:
            raise RuntimeError(
                "MCP Pro placement readback is missing references: "
                + ", ".join(missing)
            )
        return placements

    def _rebuild_at_mcp_placements(
        self,
        root: Path,
        schematic_file: Path,
        intent: SchematicIntent,
        *,
        rotations: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Rebuild symbols at MCP Pro-selected final coordinates.

        MCP Pro's current placement operations can move symbol bodies without
        translating Reference/Value fields. Re-authoring the sheet at the
        selected coordinates makes the final symbol and its fields originate
        together, while preserving MCP Pro as the sole geometry authority.
        """
        listing = self._read(root, schematic_file, "sch_get_symbols", {})
        placements = self._symbol_placements_from_listing(
            listing,
            set(intent.references),
        )

        rotation_overrides = rotations or {}
        symbols: list[dict[str, object]] = []
        for item in intent.symbols:
            payload = item.to_mcp()
            payload.update(placements[item.reference])
            if item.reference in rotation_overrides:
                payload["rotation"] = int(rotation_overrides[item.reference]) % 360
            symbols.append(payload)

        result = self._write(
            root,
            schematic_file,
            "sch_build_circuit",
            {
                "symbols": symbols,
                "auto_layout": False,
                "snap_to_grid": True,
            },
        )
        self._assert_authoring_result("Final MCP placement rebuild", result)
        return result

    def _apply_no_connects(
        self,
        root: Path,
        schematic_file: Path,
        intent: SchematicIntent,
    ) -> dict[str, Any] | None:
        if not intent.no_connects:
            return None
        result = self._write(
            root,
            schematic_file,
            "sch_mark_no_connect_pins",
            {
                "entries": [
                    {
                        "reference": endpoint.reference,
                        "pin": endpoint.pin,
                    }
                    for endpoint in intent.no_connects
                ]
            },
        )
        self._assert_authoring_result("No-connect placement", result)
        return result

    def _power_flags_that_ride(self, intent: SchematicIntent) -> list[str]:
        """The power flags that will be moved onto a wire of their net once the wires are drawn.

        Until then a flag stands where its relation put it, which is under the pin whose ground symbol wanted that place:
        the stub went round it and the ground symbol hung off a dogleg (`auto-demo10`, the buck converter's C101). They are
        left out of what the terminal planner keeps clear of. Only the tight layout moves a flag onto a wire.
        """
        policy = getattr(self, "_layout_policy", None)
        if policy is None or not policy.tight_terminals:
            return []
        return sorted({
            endpoint.reference for net in intent.all_nets if net.kind in {"power", "ground"}
            for endpoint in net.endpoints if _is_authoring_auxiliary(intent, endpoint.reference)
        })

    def _bring_flags_within_reach(self, root: Path, schematic_file: Path, intent: SchematicIntent, net: Any) -> list[str]:
        """Before a net is wired, stand each of its power flags within reach of a pin tip of the net.

        A flag is placed beside a pin when the relations are placed, but the floorplan and the alignment move symbols
        and leave the flags (`excluded_authoring_auxiliaries`) where they were: `rect`'s PWR_FLAG ended 200 mm from the
        circuit, joined by a long wire (`timed-2`, LM317). A flag farther than the reach the attachment allows from every
        pin tip moves to the free grid point nearest a pin tip, 2.54-7.62 mm along the pin's outward direction.
        """
        riding = set(self._power_flags_that_ride(intent))  # moved onto their wire later; standing in front of a pin blocks its stub
        flags = [e.reference for e in net.endpoints if _is_authoring_auxiliary(intent, e.reference) and e.reference not in riding]
        hosts = [e for e in net.endpoints if not _is_authoring_auxiliary(intent, e.reference)]
        if not flags or not hosts:
            return []
        from backend.kicad.mcp_extended_server import _symbol_by_reference
        from kicad_mcp.tools.schematic import get_pin_outward_normals, get_pin_positions, parse_schematic_file
        from kicad_mcp.tools.schematic_constants import SCHEMATIC_GRID_MM

        data = parse_schematic_file(schematic_file)

        def origin(symbol: dict[str, Any]) -> tuple[float, float]:
            return (
                float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0),
                float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0),
            )

        tips: list[tuple[tuple[float, float], tuple[float, float]]] = []  # pin tip, outward direction
        for host in hosts:
            try:
                symbol = _symbol_by_reference(data, host.reference)
                library, name = str(symbol.get("lib_id", "")).split(":", 1)
                rotation = int(symbol.get("rotation", symbol.get("rot", 0)) or 0) % 360
                unit = int(symbol.get("unit", 1) or 1)
                mirror = str(symbol.get("mirror", "") or "")
                x, y = origin(symbol)
                point = get_pin_positions(library, name, x, y, rotation, unit, mirror=mirror)[str(host.pin)]
                normal = get_pin_outward_normals(library, name, rotation, unit)[str(host.pin)]
            except (KeyError, ValueError, FileNotFoundError, OSError):
                continue
            tips.append(((float(point[0]), float(point[1])), (float(normal[0]), float(normal[1]))))
        if not tips:
            return []
        boxes: dict[str, tuple[float, float, float, float]] = {}
        measured = self._read(
            root, schematic_file, "sch_measure_functional_blocks",
            {"blocks": [{"name": r, "label": r, "references": [r]} for r in intent.references if r not in flags]},
        )
        for item in measured.get("blocks", []):
            if isinstance(item, dict) and item.get("center"):
                cx, cy = item["center"]
                hw, hh = float(item["width_mm"]) / 2, float(item["height_mm"]) / 2
                boxes[str(item["name"])] = (cx - hw, cy - hh, cx + hw, cy + hh)
        grid = SCHEMATIC_GRID_MM
        margin = grid
        moved: list[str] = []
        for reference in flags:
            here = origin(_symbol_by_reference(data, reference))
            reach = min(abs(here[0] - t[0][0]) + abs(here[1] - t[0][1]) for t in tips)
            if reach <= self.FLAG_REACH_MM + 1e-6:
                continue
            best: tuple[float, tuple[float, float]] | None = None
            for (tx, ty), (nx, ny) in tips:
                for steps in range(2, 7):
                    distance = steps * grid
                    if distance < 2.54 - 1e-6 or distance > 7.62 + 1e-6:
                        continue
                    point = (round((tx + nx * distance) / grid) * grid, round((ty + ny * distance) / grid) * grid)
                    if any(
                        l - margin < point[0] < r + margin and t - margin < point[1] < b + margin
                        for l, t, r, b in boxes.values()
                    ):
                        continue
                    cost = abs(here[0] - point[0]) + abs(here[1] - point[1])
                    if best is None or cost < best[0]:
                        best = (cost, point)
                    break  # the nearest free step along this pin's direction
            if best is None:
                continue
            self._write(
                root, schematic_file, "sch_move_symbol",
                {"reference": reference, "x_mm": best[1][0], "y_mm": best[1][1], "snap_to_grid": True},
            )
            moved.append(reference)
        return moved

    def _route_semantic_nets(
        self,
        root: Path,
        schematic_file: Path,
        intent: SchematicIntent,
    ) -> tuple[dict[str, Any], ...]:
        """Apply routing intent to a placed circuit.

        Explicit routing='label' makes a net's pins labels on stubs; every other signal (routing='wire' or 'auto')
        remains a physical wire (the direct path has no macro partitions to cross). Power/ground use conventional
        power symbols, but PWR_FLAG authoring auxiliaries are wired locally to the
        semantic source connector instead of receiving duplicate power symbols.
        """
        # Freeze a read-only presentation snapshot at the placement/routing
        # boundary. It is diagnostic in Phase 2: glyph widths and wire-net
        # ownership need exact measurement before it can become a hard gate.
        if schematic_file.is_file():
            scene, coverage = measured_scene_from_kicad(schematic_file)
            snapshot = scene.to_dict(include_conflicts=False)
            snapshot["coverage"] = coverage
            (self._artifact_root / "pre-route-presentation-scene.json").write_text(
                json.dumps(snapshot, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        symbol_by_ref = {symbol.reference: symbol for symbol in intent.symbols}
        block_by_ref = {
            reference: block.name
            for block in intent.blocks
            for reference in block.members
        }

        decisions: list[dict[str, Any]] = []
        routes: list[dict[str, Any]] = []

        def label_net(net: NetIntent, endpoints: list[PinEndpoint]) -> None:
            if not endpoints:
                return
            scope = net.effective_scope
            connections = [
                {
                    "reference": endpoint.reference,
                    "pin": endpoint.pin,
                    "net": net.name,
                }
                for endpoint in endpoints
            ]
            policy = getattr(self, "_layout_policy", None)
            if policy is not None and policy.tight_terminals and scope in (
                "local", "global",
            ):
                # The stub and its label go through the same planner as power
                # terminals, so they keep clear of other pins, wires and text.
                result = self._write(
                    root,
                    schematic_file,
                    "sch_add_pin_net_labels",
                    {
                        "connections": connections,
                        "label_kind": scope,
                        "clearance_mm": 3.81,
                    },
                )
            else:
                result = self._write(
                    root,
                    schematic_file,
                    "sch_add_pin_labels",
                    {
                        "connections": connections,
                        "stub_mm": 5.08,
                        "global_labels": scope != "local",
                        "label_kind": scope,
                    },
                )
            self._assert_authoring_result(f"Label net {net.name}", result)
            routes.append(result)

        pending_local_connections: list[dict[str, Any]] = []

        def wire_net(net: NetIntent, endpoints: list[PinEndpoint]) -> None:
            if len(endpoints) < 2:
                return

            # Multi-terminal nets are *one* electrical hyperedge. Plan the
            # Steiner/MST topology before routing any of their branches.
            if len(endpoints) >= 3:
                scene, _coverage = measured_scene_from_kicad(schematic_file)
                topology = plan_shared_net(net, scene)
                pending_local_connections.extend(topology.edges)
                decisions.append({
                    "net": net.name,
                    "routing_engine": "shared_steiner_net_v1",
                    "plan": topology.as_dict(),
                    "electrical_gate": "strict_exported_kicad_netlist",
                })
                return

            remaining = list(endpoints)

            endpoint_tokens = {endpoint.token for endpoint in remaining}
            preferred_role: str | None = None
            if net.signal_class == "clock":
                preferred_role = "clock"
            elif net.signal_class == "reset":
                preferred_role = "reset_pullup"

            semantic_hub = (
                next(
                    (
                        endpoint
                        for endpoint in remaining
                        if preferred_role
                        and (
                            symbol_by_ref[endpoint.reference].role or ""
                        ) == preferred_role
                    ),
                    None,
                )
                if preferred_role
                else None
            )
            hub = semantic_hub or next(
                (
                    driver
                    for driver in net.drivers
                    if driver.token in endpoint_tokens
                ),
                remaining[0],
            )
            targets = [
                endpoint
                for endpoint in remaining
                if endpoint.token != hub.token
            ]
            for target in targets:
                pending_local_connections.append(
                    {
                        "id": f"{net.name}:{hub.token}->{target.token}",
                        "net": net.name,
                        "ref1": hub.reference,
                        "pin1": hub.pin,
                        "ref2": target.reference,
                        "pin2": target.pin,
                    }
                )


        def labelled(net: NetIntent) -> bool:
            """Will this net be drawn as labels on stubs? (Power and ground are drawn with power symbols.)"""
            return net.kind not in {"power", "ground"} and net.routing == "label"

        riding_flags = self._power_flags_that_ride(intent)
        terminal_options = {"ignore_references": riding_flags} if riding_flags else {}

        # A label wants a straight run beside its pin and its text clear of everything; a power symbol can hang
        # from a stub that turns. So the labels get their stubs first, and the power symbols find the room that
        # is left (the MCU's UART labels stood on end because a ground symbol had taken their corridor).
        nets_in_order = sorted(
            intent.all_nets, key=lambda net: 0 if labelled(net) else 1 if net.kind in {"power", "ground"} else 2,
        )

        for net in nets_in_order:
            endpoints = list(net.endpoints)
            self._bring_flags_within_reach(root, schematic_file, intent, net)
            if net.kind in {"power", "ground"}:
                auxiliary = [
                    endpoint
                    for endpoint in endpoints
                    if _is_authoring_auxiliary(intent, endpoint.reference)
                ]
                semantic = [
                    endpoint
                    for endpoint in endpoints
                    if not _is_authoring_auxiliary(intent, endpoint.reference)
                ]

                # Reserve one visible, obstacle-aware attachment for every
                # endpoint. Power flags are electrical participants, not a
                # license to stack two distinct symbol graphics at the same
                # pin coordinate.
                # The MCP terminal tool owns both the stub and terminal
                # geometry, irrespective of endpoint role.
                # With the tight layout a flag rides on a wire of its net instead: a stub and a symbol of its own
                # leave a floating pair beside the circuit for every flag. One that finds no free place on a wire
                # gets its own terminal after all.
                policy = getattr(self, "_layout_policy", None)
                flags_ride = policy is not None and policy.tight_terminals
                # a negative rail's symbol hangs from its pin
                hangs = net.hangs
                terminal_endpoints = list(semantic) if flags_ride else [*semantic, *auxiliary]
                flag_links = "wire_stub_power_terminal"
                if flags_ride and auxiliary:
                    flag_links = "attached_to_net_wire"
                    # terminals first: the flags attach to the wires they draw
                    if terminal_endpoints:
                        placed = self._write(
                            root, schematic_file, "sch_add_pin_power_terminals",
                            {
                                "connections": [
                                    {"reference": endpoint.reference, "pin": endpoint.pin, "net": net.name,
                                     **({"hanging": True} if hangs else {})}
                                    for endpoint in terminal_endpoints
                                ],
                                "clearance_mm": 3.81,
                                **terminal_options,
                            },
                        )
                        self._assert_authoring_result(f"Power terminals {net.name}", placed)
                        routes.append(placed)
                        terminal_endpoints = []
                    try:
                        attached = self._write(
                            root, schematic_file, "sch_attach_power_flags",
                            {"flags": [
                                {"reference": endpoint.reference, "hosts": [
                                    host.token for host in endpoints
                                    if not _is_authoring_auxiliary(intent, host.reference)
                                ]}
                                for endpoint in auxiliary
                            ]},
                        )
                    except Exception as error:  # a flag that cannot ride on a wire gets terminals of its own
                        attached = {"attached": [], "unplaced": [e.reference for e in auxiliary], "error": str(error)}
                    routes.append(attached)
                    stranded = set(attached.get("unplaced", ()))
                    terminal_endpoints = [endpoint for endpoint in auxiliary if endpoint.reference in stranded]
                if terminal_endpoints:
                    result = self._write(
                        root,
                        schematic_file,
                        "sch_add_pin_power_terminals",
                        {
                            "connections": [
                                {
                                    "reference": endpoint.reference,
                                    "pin": endpoint.pin,
                                    "net": net.name,
                                    **({"hanging": True} if hangs else {}),
                                }
                                for endpoint in terminal_endpoints
                            ],
                            "clearance_mm": 3.81,
                            **terminal_options,
                        },
                    )
                    self._assert_authoring_result(
                        f"Power terminals {net.name}", result
                    )
                    routes.append(result)

                auxiliary_links = [
                    {
                        "source": flag_links,
                        "auxiliary": endpoint.token,
                    }
                    for endpoint in auxiliary
                ]

                decisions.append(
                    {
                        "net": net.name,
                        "kind": net.kind,
                        "requested": net.routing,
                        "selected": "power_symbols",
                        "semantic_endpoints": [item.token for item in semantic],
                        "relation_power_groups": [],       # no relations are placed on the direct path; the keys keep
                        "auxiliary_endpoints": [item.token for item in auxiliary],     # the file's shape
                        "auxiliary_links": auxiliary_links,
                        "power_terminal_policy": "obstacle_aware",
                        "level_one_power_tokens": [],
                    }
                )
                continue

            blocks = {
                block_by_ref.get(endpoint.reference)
                for endpoint in endpoints
                if block_by_ref.get(endpoint.reference)
            }

            if net.routing == "wire":
                selected = "wire"
                reason = "explicit_wire"
            elif net.routing == "label":
                selected = "label"
                reason = "explicit_label"
            else:
                selected = "wire"
                reason = "auto_local_partition"

            if selected == "label":
                label_net(net, endpoints)
            else:
                wire_net(net, endpoints)

            decisions.append(
                {
                    "net": net.name,
                    "kind": net.kind,
                    "scope": net.effective_scope,
                    "requested": net.routing,
                    "selected": selected,
                    "reason": reason,
                    "blocks": sorted(block for block in blocks if block),
                    "partitions": [],
                    "endpoints": [endpoint.token for endpoint in endpoints],
                }
            )

        if pending_local_connections:
            # All nets are routed in one grouped libavoid call, so the router sees every pin, junction and obstacle
            # at once. Committing nets one at a time walled in the nets routed after them and failed the two harder
            # fixtures (FINDINGS section 6). The call is net-aware: a route may not touch another net's pin or
            # wire, and may cross one in the interior of both, which KiCad does not connect.
            route_participants = {
                str(item[key])
                for item in pending_local_connections
                for key in ("ref1", "ref2")
                if item.get(key)
            }
            result = self._write(
                root,
                schematic_file,
                "sch_route_relation_connection",
                {
                    "connections": pending_local_connections,
                    "avoid_refs": sorted(
                        set(symbol_by_ref) - route_participants
                    ),
                    "protect_wires": True,
                    "obstacle_boxes": getattr(
                        self, "_route_obstacle_boxes", False
                    ),
                    **terminal_options,
                },
            )
            self._assert_authoring_result(
                "Net-aware grouped local relation routing",
                result,
            )
            routes.append(result)

        text_normalization = self._write(
            root,
            schematic_file,
            "sch_normalize_text_sizes",
            {"apply": True},
        )
        self._assert_authoring_result(
            "Normalize routed label text sizes",
            text_normalization,
        )
        routes.append(text_normalization)

        (self._artifact_root / "routing-policy.json").write_text(
            json.dumps(
                {
                    "policy": {
                        "explicit_wire": "wire",
                        "explicit_label": "label",
                        "auto_same_partition": "wire",
                        "auto_cross_partition": "label",
                        "power_ground": "power_symbols_with_local_auxiliary_wires",
                    },
                    "decisions": decisions,
                    "text_normalization": text_normalization,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return tuple(routes)

    @staticmethod
    def _visual_qa_codes(payload: dict[str, Any] | None) -> set[str]:
        """Collect MCP Pro visual-QA finding codes across all sheets."""
        if not isinstance(payload, dict):
            return set()
        codes: set[str] = set()
        findings = payload.get("findings")
        if isinstance(findings, list):
            for finding in findings:
                if isinstance(finding, dict) and finding.get("code"):
                    codes.add(str(finding["code"]))
        sheets = payload.get("sheets")
        if isinstance(sheets, list):
            for sheet in sheets:
                if not isinstance(sheet, dict):
                    continue
                sheet_findings = sheet.get("findings")
                if not isinstance(sheet_findings, list):
                    continue
                for finding in sheet_findings:
                    if isinstance(finding, dict) and finding.get("code"):
                        codes.add(str(finding["code"]))
        return codes

    def _grow_sheet(self, root: Path, schematic_file: Path) -> dict[str, Any]:
        """Grow the paper until nothing stands off it, and change nothing else.

        ``sch_fix_readability`` grows the paper too, but it also places every text again by MCP Pro's own rule, which
        undid the engine's field placement on the buck converter of `auto-demo7` (the inductor's name and value, drawn
        along the part, one over the other).
        """
        grown: list[str] = []
        for _ in range(len(self.PAPER_LADDER) - 1):
            codes = self._visual_qa_codes(self._read(root, schematic_file, "sch_visual_qa", {}))
            if not codes & {"offsheet_symbol", "offsheet_label"}:
                break
            match = re.search(r'\(paper\s+"([^"]+)"', schematic_file.read_text(encoding="utf-8", errors="ignore"))
            current = match.group(1) if match else "A4"
            if current not in self.PAPER_LADDER or current == self.PAPER_LADDER[-1]:
                break
            target = self.PAPER_LADDER[self.PAPER_LADDER.index(current) + 1]
            self._write(root, schematic_file, "sch_set_sheet_size", {"paper": target})
            grown.append(target)
        return {"status": "sheet_grown" if grown else "not_needed", "papers": grown}

    def _planned_cosmetic_categories(self) -> frozenset[str]:
        # The terminal and flag planners turn power symbols and flags (a flag stands beside a vertical wire, a ground
        # symbol is entered from above). MCP Pro's normaliser turns every one back to 0 degrees, which put a flag on
        # its wire and its text upright across a connector (`led_switch`, `auto-v19`; the MCU in v16 to v18).
        return frozenset({"orientation"})

    def _run_cosmetic_loop(
        self,
        root: Path,
        schematic_file: Path,
        *,
        max_passes: int = 3,
        target_score: float = 95.0,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        fixer_by_category = {
            "grid": "sch_align_to_grid",
            "wiring": "sch_straighten_wires",
            "readability": "sch_resolve_label_overlaps",
            "orientation": "sch_normalize_power_orientation",
            "typography": "sch_normalize_text_sizes",
        }
        available = self.client.list_tools()
        actions: list[dict[str, Any]] = []

        score = self._read(root, schematic_file, "sch_cosmetic_score", {})
        for _ in range(max_passes):
            value = score.get("cosmetic_score")
            if isinstance(value, (int, float)) and float(value) >= target_score:
                break
            category = score.get("worst_category")
            if not isinstance(category, str):
                sheets = score.get("sheets")
                if isinstance(sheets, list) and sheets and isinstance(sheets[0], dict):
                    category = sheets[0].get("worst_category")
            if not isinstance(category, str):
                break

            tool = fixer_by_category.get(category)
            if category in self._planned_cosmetic_categories():
                actions.append({"category": category, "tool": tool, "status": "planned_by_engine"})
                break
            if not tool or tool not in available:
                actions.append(
                    {
                        "category": category,
                        "status": "unsupported",
                        "tool": tool,
                    }
                )
                break

            dry_run = self._write(root, schematic_file, tool, {"apply": False})
            if dry_run.get("connectivity_preserved") is False:
                actions.append(
                    {
                        "category": category,
                        "tool": tool,
                        "status": "refused",
                        "dry_run": dry_run,
                    }
                )
                break

            applied = self._write(root, schematic_file, tool, {"apply": True})
            actions.append(
                {
                    "category": category,
                    "tool": tool,
                    "status": "applied",
                    "dry_run": dry_run,
                    "result": applied,
                }
            )
            score = self._read(root, schematic_file, "sch_cosmetic_score", {})

        return score, tuple(actions)

    @staticmethod
    def _gate_status(payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("verdict", "status"):
            value = payload.get(key)
            if isinstance(value, str):
                return value.upper()
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("status")
            if isinstance(value, str):
                return value.upper()
        return None

    @classmethod
    def _assert_acceptance_gate(
        cls,
        name: str,
        payload: dict[str, Any] | None,
    ) -> None:
        status = cls._gate_status(payload)
        if status == "FAIL":
            summary = ""
            extra = ""
            if isinstance(payload, dict):
                summary_value = payload.get("summary") or payload.get("text")
                if isinstance(summary_value, str):
                    summary = summary_value.strip().splitlines()[0]
                details_value = payload.get("details")
                if isinstance(details_value, list):
                    lines = [str(item).strip() for item in details_value if str(item).strip()]
                    if lines:
                        extra = " | " + " ; ".join(lines[:6])
                elif isinstance(details_value, str) and details_value.strip():
                    extra = " | " + " ; ".join(details_value.strip().splitlines()[:6])
                if not extra:
                    content_text = payload.get("_content_text")
                    if isinstance(content_text, str) and content_text.strip():
                        lines = [
                            line.strip()
                            for line in content_text.splitlines()
                            if line.strip()
                        ]
                        failure_lines = [
                            line for line in lines
                            if line.startswith(("FAIL:", "BLOCKED:"))
                        ]
                        selected = failure_lines[:8] or lines[-8:]
                        if selected:
                            extra = " | " + " ; ".join(selected)
            detail = f": {summary}" if summary else ""
            raise RuntimeError(f"{name} failed{detail}{extra}")

    @staticmethod
    def _assert_erc_has_no_errors(payload: dict[str, Any] | None) -> None:
        if not isinstance(payload, dict):
            return

        def error_details() -> str:
            details: list[str] = []
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                violations = metadata.get("violations")
                if isinstance(violations, list):
                    for item in violations:
                        if not isinstance(item, dict):
                            continue
                        severity = str(item.get("severity", "")).lower()
                        if severity != "error":
                            continue
                        description = (
                            item.get("description")
                            or item.get("message")
                            or item.get("type")
                            or item.get("rule")
                        )
                        if description:
                            suffix_parts: list[str] = []
                            raw_items = item.get("items")
                            if isinstance(raw_items, list):
                                refs = [
                                    str(raw.get("ref"))
                                    for raw in raw_items
                                    if isinstance(raw, dict) and raw.get("ref")
                                ]
                                pins = [
                                    str(raw.get("pin"))
                                    for raw in raw_items
                                    if isinstance(raw, dict) and raw.get("pin")
                                ]
                                nets = [
                                    str(raw.get("net"))
                                    for raw in raw_items
                                    if isinstance(raw, dict) and raw.get("net")
                                ]
                                if refs:
                                    suffix_parts.append("refs=" + ",".join(refs))
                                if pins:
                                    suffix_parts.append("pins=" + ",".join(pins))
                                if nets:
                                    suffix_parts.append("nets=" + ",".join(nets))
                                item_descriptions = [
                                    str(raw.get("description") or raw.get("message")).strip()
                                    for raw in raw_items
                                    if isinstance(raw, dict)
                                    and (raw.get("description") or raw.get("message"))
                                ]
                                if item_descriptions:
                                    suffix_parts.append(
                                        "items=" + " / ".join(item_descriptions[:4])
                                    )
                            suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
                            details.append(str(description).strip() + suffix)
            findings = payload.get("findings")
            if isinstance(findings, list):
                for item in findings:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("severity", "")).lower() != "error":
                        continue
                    description = item.get("description") or item.get("message")
                    if description:
                        suffix_parts: list[str] = []
                        item_metadata = item.get("metadata")
                        if isinstance(item_metadata, dict):
                            refs = item_metadata.get("refs")
                            pins = item_metadata.get("pins")
                            nets = item_metadata.get("nets")
                            if isinstance(refs, list) and refs:
                                suffix_parts.append("refs=" + ",".join(map(str, refs)))
                            if isinstance(pins, list) and pins:
                                suffix_parts.append("pins=" + ",".join(map(str, pins)))
                            if isinstance(nets, list) and nets:
                                suffix_parts.append("nets=" + ",".join(map(str, nets)))
                        suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
                        details.append(str(description).strip() + suffix)
            if not details:
                content_text = payload.get("_content_text")
                if isinstance(content_text, str):
                    details.extend(
                        line.strip()
                        for line in content_text.splitlines()
                        if "error" in line.lower() and line.strip()
                    )
            unique = list(dict.fromkeys(detail for detail in details if detail))
            return " | " + " ; ".join(unique[:6]) if unique else ""

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            summary = metadata.get("summary")
            if isinstance(summary, dict):
                errors = summary.get("error", 0)
                if isinstance(errors, int) and errors > 0:
                    raise RuntimeError(
                        f"ERC failed with {errors} error(s){error_details()}"
                    )
        findings = payload.get("findings")
        if isinstance(findings, list):
            errors = [
                item
                for item in findings
                if isinstance(item, dict)
                and str(item.get("severity", "")).lower() == "error"
            ]
            if errors:
                raise RuntimeError(
                    f"ERC failed with {len(errors)} error finding(s){error_details()}"
                )

    def _place_fields(self, root: Path, schematic_file: Path, intent: SchematicIntent) -> bool:
        """Move reference and value text off wires, symbols and each other, once the wires are drawn.

        MCP Pro writes a reference above its symbol and a value below it, on the symbol's axis, which for a part in a
        vertical chain is on the wire (FINDINGS section 13). The text is placed with ``field_placement``; always True.
        """
        scene, _coverage = measured_scene_from_kicad(schematic_file)
        plan = plan_fields(scene)
        if plan.moves:
            self._write(
                root, schematic_file, "sch_place_fields",
                {"placements": [
                    {"reference": move.owner, "field": move.field, "x_mm": move.x, "y_mm": move.y, "angle": move.angle}
                    for move in plan.moves
                ]},
            )
        self._artifact_root.joinpath("field-placement.json").write_text(
            json.dumps({
                "fields": plan.fields, "moved": len(plan.moves), "solver": plan.solver,
                "collisions_before": plan.collisions_before, "collisions_after": plan.collisions_after,
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._write_names_and_captions(root, schematic_file, intent)
        return True

    def _write_names_and_captions(self, root: Path, schematic_file: Path, intent: SchematicIntent) -> None:
        """Write each meaningful net's name on its wire and a caption over each block (tight layout only).

        Engineers do both (38 of 40 measured human sheets name nets, 28 of 40 carry titles). A label whose anchor lies
        on a wire joins that wire's net, so nothing is rewired. Skipped with ``KICAD_AI_WIRE_LABELS=0``.
        """
        policy = getattr(self, "_layout_policy", None)
        if (
            policy is None or not policy.tight_terminals
            or os.getenv("KICAD_AI_WIRE_LABELS", "1").strip() == "0"
        ):
            return
        from backend.schematic.presentation_geometry import Bounds, GeometryKind
        from backend.schematic.wire_nets import wire_network
        from backend.schematic.wire_labels import nets_to_name, plan_caption, plan_wire_label

        scene, _coverage = measured_scene_from_kicad(schematic_file)
        network = wire_network(scene)
        wires = {i.id: i for i in scene.items if i.kind == GeometryKind.WIRE and i.segment is not None}
        pin_net = {e.token: n.name for n in intent.all_nets for e in n.endpoints if n.kind == "signal"}
        component_net: dict[int, str | None] = {}
        for index, component in enumerate(network.components):
            names = {pin_net[p] for p in component.pins if p in pin_net}
            component_net[index] = next(iter(names)) if len(names) == 1 else None
        solid = [
            i.bounds for i in scene.items
            if i.kind in {GeometryKind.BODY, GeometryKind.POWER_GRAPHIC} or i.kind in TEXT_KINDS_FOR_LABELS
        ]

        def wire_box(item: Any) -> Any:
            (ax, ay), (bx, by) = item.segment
            return Bounds(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))

        record: dict[str, Any] = {"nets": [], "captions": [], "skipped": []}
        label_writes: list[dict[str, Any]] = []
        placed: list[Any] = []
        for net in nets_to_name(intent):
            own = [
                wires[sid] for index, component in enumerate(network.components) if component_net[index] == net.name
                for sid in component.segments if sid in wires
            ]
            if not own:
                record["skipped"].append({"net": net.name, "reason": "no wire of its own"})
                continue
            own_ids = {i.id for i in own}
            others = [wire_box(i) for i in wires.values() if i.id not in own_ids]
            width = text_width_mm(net.name, self.LABEL_TEXT_MM)
            found = plan_wire_label(
                net.name, [tuple(map(tuple, i.segment)) for i in own], [*solid, *others, *placed],
                width, self.LABEL_TEXT_MM,
            )
            if found is None:
                record["skipped"].append({"net": net.name, "reason": "no clear stretch of wire"})
                continue
            (x, y), rotation = found
            label_writes.append({"net": net.name, "x_mm": x, "y_mm": y, "rotation": rotation})
            vertical = rotation == 90
            placed.append(Bounds(
                x - 0.5, y - width if vertical else y - self.LABEL_TEXT_MM,
                x + self.LABEL_TEXT_MM if vertical else x + width, y + (width / 2 if vertical else 0.5),
            ))
            record["nets"].append({"net": net.name, "point": [x, y], "rotation": rotation})

        text_writes: list[dict[str, Any]] = []
        for block in intent.blocks:
            members = set(block.members)
            if len(members) < 2:
                continue
            boxes = [
                i.bounds for i in scene.items
                if i.owner in members and i.kind in {GeometryKind.BODY, GeometryKind.REFERENCE, GeometryKind.VALUE}
            ]
            if not boxes:
                continue
            area = Bounds(
                min(b.left for b in boxes), min(b.top for b in boxes), max(b.right for b in boxes), max(b.bottom for b in boxes),
            )
            caption = (block.label or block.name).replace("_", " ").title()
            width = text_width_mm(caption, self.CAPTION_TEXT_MM)
            point = plan_caption(area, caption, [*solid, *placed], width, self.CAPTION_TEXT_MM)
            if point is None:
                record["skipped"].append({"block": block.name, "reason": "no clear place above the block"})
                continue
            text_writes.append({"text": caption, "x_mm": point[0], "y_mm": point[1], "size_mm": self.CAPTION_TEXT_MM})
            placed.append(Bounds(point[0], point[1] - self.CAPTION_TEXT_MM, point[0] + width, point[1]))
            record["captions"].append({"block": block.name, "text": caption, "point": list(point)})
        try:
            if label_writes:
                self._write(root, schematic_file, "sch_add_wire_labels", {"labels": label_writes})
            if text_writes:
                self._write(root, schematic_file, "sch_add_text", {"items": text_writes})
        except Exception as error:  # the layout stands without its names
            record["error"] = str(error)
        self._artifact_root.joinpath("wire-labels.json").write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8",
        )

    def _initial_layout_policy(self) -> LayoutPolicy:
        """The layout policy a build starts from; the environment can override it for experiments."""
        mode = os.getenv("KICAD_AI_LAYOUT_MODE", "").strip() or "tight"
        if mode == "legacy":
            # MCP Pro's symbol boxes for placement, but power terminals are still measured by body and pin tips.
            policy = LayoutPolicy.legacy_with_tight_terminals()
        elif mode == "tight":
            margin = os.getenv("KICAD_AI_LAYOUT_MARGIN_MM", "").strip()
            policy = LayoutPolicy.tight(float(margin) if margin else TIGHT_MARGIN_MM)
        else:
            raise ValueError("KICAD_AI_LAYOUT_MODE must be legacy or tight")
        boxes = os.getenv("KICAD_AI_STUB_OBSTACLES", "").strip().lower()
        return policy.with_stub_obstacles(boxes in {"1", "true", "yes"})

    def _apply_layout_policy(
        self, root: Path, schematic_file: Path, policy: LayoutPolicy,
    ) -> None:
        """Tell the MCP server how much room placement keeps, and the router how to treat earlier wires."""
        self._layout_policy = policy
        self._route_obstacle_boxes = policy.stub_obstacles
        self._write(
            root, schematic_file, "sch_set_layout_clearances", policy.as_arguments(),
        )

    def available(self) -> bool:
        if not self.client.schematic_writes_enabled():
            return False
        try:
            tools = self.client.list_tools()
        except Exception:
            return False
        return (self.REQUIRED_WRITE_TOOLS | self.REQUIRED_QA_TOOLS).issubset(tools)

    def _verify_references(self, root: Path, expected: set[str]) -> None:
        readback = self.kicad.read_project(root)
        actual = {
            str(node.get("ref"))
            for node in readback.get("project_graph", {}).get("nodes", [])
            if node.get("kind") == "component" and node.get("ref")
        }
        missing = sorted({split_unit(reference)[0] for reference in expected} - actual)   # a unit is its part's reference
        if missing:
            raise RuntimeError(
                "Post-workflow readback is missing references: " + ", ".join(missing)
            )


for _name, _stage in TIMED_METHODS.items():
    setattr(DirectWorkflow, _name, _timed_method(getattr(DirectWorkflow, _name), _stage))

