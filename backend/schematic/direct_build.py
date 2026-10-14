"""Build a sheet from a finished placement, once: the stages after placement, with no trials, no gate and no retries.

A placer that returns one placement per circuit needs only what follows placement, done once. ``build_direct`` takes the
position and orientation of every part and runs

    create -> place -> terminals_and_routes -> fields -> checks

each inside ``timing.stage`` (``KICAD_AI_TIMING=1``). It runs on a ``DirectWorkflow`` (``direct_workflow.py``): its stage
methods draw the placement, the layout policy is the ``compact`` profile's (guarded routing, tight power terminals). Names
and captions are written by ``_place_fields`` itself, so their time is in ``fields`` (the label tools appear as
``fields/labels``).
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from backend.kicad.netlist import parse_kicad_xml
from backend.kicad.transaction import SchematicFileTransaction

from . import timing
from .connectivity import verify_intent_netlist
from .designators import split_unit, unit_reference
from .intent import SchematicIntent, SymbolIntent, expand_units
from .kicad_geometry import measured_scene_from_kicad
from .placement_refinement import Placement
from .direct_workflow import SchematicWorkflowResult

PROFILE = "compact"
MARGIN_MM = 12.7           # the margin the trial loop translates the sheet to after placing parts


def _symbol_entries(item: SymbolIntent, units_of: Mapping[str, list[int]]) -> list[dict[str, Any]]:
    """The symbols ``sch_build_circuit`` is given for ``item``: one, or one per unit when the part is placed in units."""
    if item.reference not in units_of:
        return [item.to_mcp()]
    return [replace(item, reference=unit_reference(item.reference, unit)).to_mcp() for unit in units_of[item.reference]]


def _unit_pins(
    workflow: Any, root: Path, schematic_file: Path, units_of: Mapping[str, list[int]],
) -> dict[str, dict[int, set[str]]]:
    """The pins each unit of each multi-unit part draws, as the server reports them (``sch_symbol_geometry`` of ``U5:2``)."""
    names = [unit_reference(part, unit) for part, units in units_of.items() for unit in units]
    reply = workflow._read(root, schematic_file, "sch_symbol_geometry", {"references": names})
    payload = json.loads(reply["result"]) if isinstance(reply.get("result"), str) else reply
    symbols = payload["symbols"]
    return {
        part: {unit: set(symbols[unit_reference(part, unit)]["orientations"][0]["pins"]) for unit in units}
        for part, units in units_of.items()
    }


def _apply_placement(
    workflow: Any, root: Path, schematic_file: Path, intent: SchematicIntent, placement: Mapping[str, Placement],
) -> dict[str, Any]:
    """Position and orientation of every placed reference, as ``_refine_free_placement`` applies a refinement's.

    Positions go through ``sch_move_symbol`` in sorted reference order; rotations and mirrors enter the orientation plan
    the final rebuild reads. A power flag (authoring auxiliary) that hangs on one moved part follows it by its move.
    """
    symbols = {symbol.reference: symbol for symbol in intent.symbols}
    auxiliary = {reference for reference, symbol in symbols.items() if symbol.authoring_auxiliary}
    placed = {r: p for r, p in placement.items() if r in symbols and r not in auxiliary and not r.startswith("#")}
    ignored = sorted(set(placement) - set(placed))
    missing = sorted(set(symbols) - auxiliary - set(placed))
    if missing:
        raise ValueError("placement is missing references: " + ", ".join(missing))

    listing = workflow._read(root, schematic_file, "sch_get_symbols", {})
    created = workflow._symbol_placements_from_listing(listing, set(symbols))
    for reference in sorted(placed):
        position = placed[reference].position
        written = workflow._write(
            root, schematic_file, "sch_move_unit" if split_unit(reference)[1] is not None else "sch_move_symbol",
            {"reference": reference, "x_mm": position[0], "y_mm": position[1], "snap_to_grid": True},
        )
        workflow._assert_authoring_result(f"direct placement {reference}", written)

    # the power flags of a part that moved go with it
    moved_by = {
        reference: (placed[reference].position[0] - created[reference]["x_mm"],
                    placed[reference].position[1] - created[reference]["y_mm"])
        for reference in placed
    }
    followers: list[str] = []
    for net in intent.all_nets:
        partners = {endpoint.reference for endpoint in net.endpoints if endpoint.reference in moved_by}
        flags = [endpoint.reference for endpoint in net.endpoints if endpoint.reference in auxiliary]
        if len(partners) != 1 or not flags:
            continue
        dx, dy = moved_by[next(iter(partners))]
        for flag in flags:
            workflow._write(
                root, schematic_file, "sch_move_symbol",
                {"reference": flag, "x_mm": created[flag]["x_mm"] + dx, "y_mm": created[flag]["y_mm"] + dy,
                 "snap_to_grid": True},
            )
            followers.append(flag)
    workflow._write(
        root, schematic_file, "sch_translate_references_to_margin",
        {"references": list(intent.references), "margin_mm": MARGIN_MM},
    )

    rotations = {reference: p.orientation.rotation % 360 for reference, p in sorted(placed.items())}
    mirrors = {reference: p.orientation.mirror for reference, p in sorted(placed.items()) if p.orientation.mirror}
    orientation = {"rotations": rotations, "mirrors": mirrors, "constraints": [], "source": "direct_build"}

    # once the orientations are known: rebuild at the placed coordinates with the rotations, then mirror, then keep the
    # text of a half-turned part upright
    build = workflow._rebuild_at_mcp_placements(root, schematic_file, intent, rotations=rotations)
    if mirrors:
        workflow._write(root, schematic_file, "sch_set_symbol_mirror", {"mirrors": mirrors})
    upside_down = sorted(r for r, rotation in rotations.items() if rotation == 180)
    orientation_fields = None
    if upside_down:
        orientation_fields = workflow._write(
            root, schematic_file, "sch_modify_field_orientation",
            {"references": upside_down, "fields": ["Reference", "Value"], "angle": 0},
        )
    return {
        "build": build, "orientation": orientation, "orientation_fields": orientation_fields,
        "ignored_references": ignored, "followers": followers,
    }


def _checks(workflow: Any, root: Path, schematic_file: Path, intent: SchematicIntent, fields_placed: bool) -> dict[str, Any]:
    """The acceptance checks and the ``checks/attribution`` export; any failure raises."""
    tools = workflow.client.list_tools()
    initial = workflow._read(root, schematic_file, "sch_visual_qa", {})
    codes = workflow._visual_qa_codes(initial)
    triggers = {"text_overlap", "offsheet_symbol", "offsheet_label"}
    if fields_placed:
        triggers -= {"text_overlap"}
    if codes & triggers:
        readability = (
            workflow._grow_sheet(root, schematic_file) if fields_placed
            else workflow._write(root, schematic_file, "sch_fix_readability", {"max_passes": 3})
        )
        fields = {"status": "paper_grown" if fields_placed else "delegated_to_sch_fix_readability",
                  "trigger_codes": sorted(codes & triggers)}
    else:
        readability = {"status": "not_needed", "trigger_codes": sorted(codes)}
        fields = {"status": "preserved", "reason": "no visual-QA text-overlap finding"}
    cosmetic, cosmetic_fixes = workflow._run_cosmetic_loop(root, schematic_file)
    visual_qa = workflow._read(root, schematic_file, "sch_visual_qa", {})
    connectivity = (
        workflow._read(root, schematic_file, "schematic_connectivity_gate", {})
        if "schematic_connectivity_gate" in tools else None
    )
    erc = workflow._read(root, schematic_file, "run_erc", {}) if "run_erc" in tools else None
    workflow._assert_acceptance_gate("Schematic visual QA", visual_qa)
    workflow._assert_acceptance_gate("Schematic connectivity gate", connectivity)
    workflow._assert_erc_has_no_errors(erc)
    workflow._verify_references(
        root, {symbol.reference for symbol in intent.symbols if not symbol.reference.startswith("#")},
    )
    netlist_xml = workflow.netlist_exporter.export_xml(root, schematic_file)
    verification = verify_intent_netlist(intent, parse_kicad_xml(netlist_xml))
    # the evidence the checks/attribution stage leaves: which saved wire belongs to which net
    scene, coverage = measured_scene_from_kicad(schematic_file, netlist_xml=netlist_xml)
    attributed = scene.to_dict(include_conflicts=False)
    attributed["coverage"] = coverage
    Path(workflow._artifact_root).joinpath("post-route-attributed-scene.json").write_text(
        json.dumps(attributed, indent=2, sort_keys=True), encoding="utf-8",
    )
    return {
        "fields": fields, "readability": readability, "visual_qa": visual_qa, "cosmetic_score": cosmetic,
        "cosmetic_fixes": cosmetic_fixes, "connectivity_gate": connectivity, "erc": erc,
        "netlist_verification": verification, "netlist_xml": netlist_xml,
    }


def build_direct(
    workflow: Any, root: Path, schematic_file: Path, intent: SchematicIntent, placement: Mapping[str, Placement],
) -> dict[str, Any]:
    """Build ``intent`` at ``placement`` once. Returns ``{"result": SchematicWorkflowResult, "record": dict}``.

    Raises on the first stage that fails; the file transaction puts the input sheet back.
    """
    root = root.resolve()
    schematic_file = schematic_file.resolve()
    artifact_root = Path(workflow._artifact_root)
    if not workflow.available():
        raise RuntimeError("MCP Pro schematic workflow is unavailable")
    timing.reset()
    state: dict[str, Any] = {}
    # A multi-unit part is placed unit by unit (spec 4c): the placement names its units (``U5:1``, ``U5:2``); a single-unit
    # circuit has none, and nothing below differs from a build of plain references.
    units_of: dict[str, list[int]] = {}
    for placed_name in placement:
        part, unit = split_unit(placed_name)
        if unit is not None:
            units_of.setdefault(part, []).append(unit)
    units_of = {part: sorted(units) for part, units in units_of.items()}
    whole = intent

    def execute() -> dict[str, Any]:
        nonlocal intent
        with timing.stage("create"):
            policy = workflow._initial_layout_policy()
            workflow._apply_layout_policy(root, schematic_file, policy)
            if "project_set_design_intent" in workflow.client.list_tools():
                workflow._write(root, schematic_file, "project_set_design_intent", workflow.policy.mcp_design_intent(intent))
            created = workflow._write(
                root, schematic_file, "sch_build_circuit",
                {"symbols": [entry for item in intent.symbols for entry in _symbol_entries(item, units_of)],
                 "auto_layout": True, "snap_to_grid": True},
            )
            state["layout"] = policy.describe()
            if units_of:
                intent = expand_units(whole, _unit_pins(workflow, root, schematic_file, units_of))
        with timing.stage("place"):
            placed = _apply_placement(workflow, root, schematic_file, intent, placement)
        with timing.stage("terminals_and_routes"):
            no_connects = workflow._apply_no_connects(root, schematic_file, intent)
            routes = workflow._route_semantic_nets(root, schematic_file, intent)
            junctions = workflow._write(root, schematic_file, "sch_add_missing_junctions", {})
        with timing.stage("fields"):      # includes the net names and block captions (`_write_names_and_captions`)
            fields_placed = workflow._place_fields(root, schematic_file, intent)
        with timing.stage("checks"):
            checks = _checks(workflow, root, schematic_file, intent, fields_placed)
        return {
            "created": created, "placed": placed, "no_connects": no_connects, "routes": routes,
            "junctions": junctions, "checks": checks, "fields_placed": fields_placed,
        }

    try:
        outcome, before, after = SchematicFileTransaction(root, schematic_file).run(execute)
    finally:
        timing.dump(artifact_root / "timing.json")
    checks = outcome["checks"]
    placed = outcome["placed"]
    result = SchematicWorkflowResult(
        references=intent.references,
        build=outcome["created"],
        placement={}, flow_placements=(), block_placements=(),
        routes=outcome["routes"], labels=(),
        annotations=(
            placed["orientation"], {"pin_refinements": []},
            {},                                  # block frames: the direct path draws none
            {"no_connects": outcome["no_connects"]} if outcome["no_connects"] is not None else {},
        ),
        junctions=outcome["junctions"], fields=checks["fields"], readability=checks["readability"],
        visual_qa=checks["visual_qa"], cosmetic_score=checks["cosmetic_score"], cosmetic_fixes=checks["cosmetic_fixes"],
        connectivity_gate=checks["connectivity_gate"], erc=checks["erc"],
        netlist_verification=checks["netlist_verification"], before_sha256=before, after_sha256=after,
    )
    record = {
        "mode": "direct",
        "profile": PROFILE,
        "layout": state.get("layout"),
        "stages": ["create", "place", "terminals_and_routes", "fields", "checks"],
        "placement": {
            reference: {
                "position": list(p.position), "rotation": p.orientation.rotation, "mirror": p.orientation.mirror,
            }
            for reference, p in sorted(placement.items())
        },
        "placement_ignored": placed["ignored_references"],
        "power_flags_moved": placed["followers"],
        "checks": {
            "visual_qa": result.visual_qa.get("status"),
            "connectivity_gate": (result.connectivity_gate or {}).get("status"),
            "erc": (result.erc or {}).get("status"),
            "netlist_verification": (result.netlist_verification or {}).get("status"),
            "cosmetic_score": (result.cosmetic_score or {}).get("cosmetic_score"),
        },
    }
    (artifact_root / "direct-build.json").write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return {"result": result, "record": record, "netlist_xml": checks["netlist_xml"]}
