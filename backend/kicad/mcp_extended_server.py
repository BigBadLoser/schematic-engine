from __future__ import annotations

import argparse
import json
import logging
import os
import re
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.schematic.designators import split_unit, unit_reference
from backend.schematic.symbol_edit import find_symbol_block


_RELATIONS = {"left_of", "right_of", "above", "below"}


_log = logging.getLogger(__name__)

_REROUTABLE_PREFIXES = (
    "libavoid route intersects symbol body",
    "Net-aware route intersects",
)


def should_reroute_failure(message: str) -> bool:
    """True when a branch validation failure may be cured by routing it again with wider body obstacles."""
    return message.startswith(_REROUTABLE_PREFIXES)


def routing_obstacle_refs(
    avoid: set[str], participants: set[str], ignore: list[str] | None = None,
) -> list[str]:
    """Symbols the router must keep clear of, less the ignored ones (riding power flags)."""
    skip = {str(item) for item in ignore or ()}
    return sorted((set(avoid) - skip) | set(participants))


def widen_obstacle_nodes(nodes: list[dict[str, Any]], widen: float) -> None:
    """Grow every obstacle:: node by `widen` on each side, in place."""
    for node in nodes:
        if str(node["id"]).startswith("obstacle::"):
            node["x"] -= widen
            node["y"] -= widen
            node["width"] += 2.0 * widen
            node["height"] += 2.0 * widen


def failure_with_repair_counts(failure: ValueError, rerouted: int, repaired: int) -> ValueError:
    return ValueError(f"{failure} (after {rerouted} reroute, {repaired} diagonal repairs)")


def _symbol_order_key(item: dict[str, Any]) -> tuple[str, float, float]:
    return (
        str(item.get("reference", "")),
        float(item.get("x", item.get("x_mm", 0.0)) or 0.0),
        float(item.get("y", item.get("y_mm", 0.0)) or 0.0),
    )


def _symbol_by_reference(data: dict[str, Any], reference: str) -> dict[str, Any]:
    symbols = sorted(
        (*data.get("symbols", []), *data.get("power_symbols", [])),
        key=_symbol_order_key,   # a multi-unit part's first match must not depend on the sheet's UUID order
    )
    # "U5:2" names one unit of a multi-unit part (the placeable the layout works with); a plain reference is its first unit
    part, unit = split_unit(reference)
    match = None
    if unit is not None:
        match = next(
            (item for item in symbols
             if str(item.get("reference", "")) == part and int(item.get("unit", 1) or 1) == unit),
            None,
        )
    if match is None:
        match = next(
            (item for item in symbols if str(item.get("reference", "")) == reference),
            None,
        )
    if match is None:
        raise ValueError(f"Reference not found: {reference}")
    return match


def _placeable_name(item: dict[str, Any], instances: dict[str, int]) -> str:
    """A placed symbol's name in the terminal planner's maps: its reference, or ``U5:2`` when the reference is drawn in
    several units (each unit is a symbol of its own with its own body and pins)."""
    reference = str(item["reference"])
    if instances.get(reference, 1) > 1:
        return unit_reference(reference, int(item.get("unit", 1) or 1))
    return reference


def build_extended_server(profile: str = "schematic_authoring"):
    """Build MCP Pro plus companion-specific schematic composition primitives.

    This module extends the MCP writer surface; it does not write KiCad files
    independently. The relative-placement primitive resolves semantic relations
    with MCP Pro's own layout constants and delegates mutation to sch_move_symbol.
    """
    from kicad_mcp.server import build_server
    import kicad_mcp.tools.schematic as _mcp_schematic
    from backend.kicad.library_cache import install as cache_symbol_libraries
    from backend.kicad.power_references import install as install_power_references

    # MCP Pro parses a whole symbol library for every bound or pin it is asked for; a cluster of candidate placements
    # asks hundreds of times, and one call outlasted the client's patience (backend/kicad/library_cache.py)
    cache_symbol_libraries(_mcp_schematic)
    # power symbols get #PWR01, #PWR02, ... instead of a random number (backend/kicad/power_references.py)
    install_power_references(_mcp_schematic)
    # the wire scan is quadratic in the sheet's size (backend/kicad/scan_speed.py)
    from backend.kicad.scan_speed import install as install_scan_speed
    install_scan_speed(_mcp_schematic)
    from kicad_mcp.tools.schematic import (
        _append_before_sheet_instances,
        _fmt_mm,
        _get_schematic_file,
        _load_layout_design_intent,
        _placed_symbol_bbox,
        _placed_symbol_key,
        _placed_symbol_mirrors,
        _sexpr_string,
        _symbol_bbox_bounds as _mcp_symbol_bbox_bounds,
        _transactional_write_to_schematic,
        get_pin_outward_normals,
        get_pin_positions,
        new_uuid,
        parse_schematic_file as _mcp_parse_schematic_file,
    )
    from kicad_mcp.tools.schematic_constants import (
        AUTO_LAYOUT_COLUMN_SPACING_MM,
        AUTO_LAYOUT_ROW_SPACING_MM,
        SCHEMATIC_GRID_MM,
    )
    from backend.schematic.orientation import (
        MIRRORS, QUARTER_TURN_GRIDS, ROTATIONS, Orientation, PinTarget, choose_orientation, extension_penalty,
        mirror_box,
    )

    def parse_schematic_file(path: Any) -> dict[str, Any]:
        """MCP Pro's parse, with each placed symbol's mirror axis added as ``mirror``.

        KiCad's ``(mirror x|y)`` moves a symbol's pins and body about its anchor. MCP Pro's geometry helpers take it as
        an argument but its parser leaves it out of the symbol, so every tool that reads a mirrored symbol's pins here
        would have placed its terminals and routes where the pins are not.
        """
        data = _mcp_parse_schematic_file(path)
        try:
            mirrors = _placed_symbol_mirrors(Path(path).read_text(encoding="utf-8"))
        except OSError:
            return data
        if mirrors:
            for symbol in data.get("symbols", []):
                axis = mirrors.get(_placed_symbol_key(symbol))
                if axis:
                    symbol["mirror"] = axis
        return data

    # How much room placement keeps around a symbol. "legacy" is MCP Pro's fixed
    # 20.32 x 15.24 mm minimum box, the geometry the engine shipped with. "tight"
    # is the symbol's body and pin tips plus a margin, per reference, so a symbol
    # that a later stage struggled with can be given more room without loosening
    # the rest of the sheet. The workflow chooses with sch_set_layout_clearances
    # before each build; every placement, sizing and compaction tool below reads
    # the result through _symbol_bbox_bounds.
    layout_clearances: dict[str, Any] = {
        "mode": "legacy", "default_mm": 1.27, "by_reference": {},
        "tight_terminals": False,
    }
    relative_symbol_bounds: dict[
        tuple[str, int, int, str], tuple[float, float, float, float]
    ] = {}

    def _symbol_bbox_bounds(
        symbol: dict[str, Any],
    ) -> tuple[float, float, float, float]:
        if layout_clearances["mode"] != "tight":
            return _mcp_symbol_bbox_bounds(symbol)
        margin = float(
            layout_clearances["by_reference"].get(
                str(symbol.get("reference", "")),
                layout_clearances["default_mm"],
            )
        )
        x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
        y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
        mirror = str(symbol.get("mirror", "") or "")
        key = (
            str(symbol.get("lib_id", "") or ""),
            int(round(float(symbol.get("rotation", 0.0) or 0.0))),
            int(symbol.get("unit", 1) or 1),
            mirror,
        )
        relative = relative_symbol_bounds.get(key)
        if relative is None:
            box = _placed_symbol_bbox(
                {**symbol, "x": 0.0, "y": 0.0, "x_mm": 0.0, "y_mm": 0.0}
            )
            relative = (
                float(box.x_min), float(box.y_min),
                float(box.x_max), float(box.y_max),
            )
            # MCP Pro's box is of the symbol unmirrored: flip it about the anchor
            relative = mirror_box(relative, (0.0, 0.0), mirror)
            relative_symbol_bounds[key] = relative
        return (
            x + relative[0] - margin,
            y + relative[1] - margin,
            x + relative[2] + margin,
            y + relative[3] + margin,
        )

    server = build_server(profile, defer_registration=False)

    async def call_checked(name: str, arguments: dict[str, Any]) -> Any:
        """Call another tool and raise if it failed.

        MCP Pro's call_tool turns a tool's exception into an error *result*
        instead of raising, so a caller that ignores the result carries on as if
        the tool had worked. Here that left a pull-up's pin floating after its
        power terminal found no exit. The message of the failure is kept, so
        the caller's own error reads as the tool's would have.
        """
        result = await server.call_tool(name, arguments)
        if getattr(result, "is_error", False):
            texts = [
                str(getattr(item, "text", ""))
                for item in (getattr(result, "content", None) or [])
            ]
            raise ValueError(" ".join(text for text in texts if text) or f"{name} failed")
        return result

    @server.tool()
    async def sch_set_layout_clearances(
        mode: str = "legacy",
        default_mm: float = 1.27,
        by_reference: dict[str, float] | None = None,
        tight_terminals: bool | None = None,
    ) -> str:
        """Choose how much room placement keeps around symbols in later calls.

        ``legacy`` keeps MCP Pro's fixed 20.32 x 15.24 mm minimum box. ``tight``
        sizes each symbol to its body and pin tips plus ``default_mm``, or the
        margin given for its reference in ``by_reference``. Placement, block
        sizing and compaction all read the same bounds, so they stay consistent.
        Power terminals use body-and-pin-tip bounds when ``tight_terminals`` is
        set, which defaults to on for ``tight`` and off for ``legacy``.
        """
        if mode not in ("legacy", "tight"):
            raise ValueError("mode must be 'legacy' or 'tight'")
        margins = {
            str(reference): float(value)
            for reference, value in (by_reference or {}).items()
        }
        if float(default_mm) < 0 or any(value < 0 for value in margins.values()):
            raise ValueError("layout margins cannot be negative")
        layout_clearances["mode"] = mode
        layout_clearances["default_mm"] = float(default_mm)
        layout_clearances["by_reference"] = margins
        layout_clearances["tight_terminals"] = (
            mode == "tight" if tight_terminals is None else bool(tight_terminals)
        )
        return json.dumps(layout_clearances, sort_keys=True)

    @server.tool()
    async def sch_move_relative(
        reference: str,
        anchor_ref: str,
        relation: str,
        steps: int = 1,
    ) -> str:
        """Move a symbol relative to another using MCP Pro layout spacing.

        Supported relations are left_of, right_of, above, and below. The caller
        supplies semantic intent only; this MCP-side tool computes the final
        coordinate and delegates the write to MCP Pro's sch_move_symbol.
        """
        if relation not in _RELATIONS:
            raise ValueError(
                "relation must be one of: " + ", ".join(sorted(_RELATIONS))
            )
        if steps < 1 or steps > 8:
            raise ValueError("steps must be between 1 and 8")
        if reference == anchor_ref:
            raise ValueError("reference and anchor_ref must differ")

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        _symbol_by_reference(data, reference)
        anchor = _symbol_by_reference(data, anchor_ref)
        x = float(anchor.get("x", anchor.get("x_mm", 0.0)) or 0.0)
        y = float(anchor.get("y", anchor.get("y_mm", 0.0)) or 0.0)

        if relation == "left_of":
            x -= AUTO_LAYOUT_COLUMN_SPACING_MM * steps
        elif relation == "right_of":
            x += AUTO_LAYOUT_COLUMN_SPACING_MM * steps
        elif relation == "above":
            y -= AUTO_LAYOUT_ROW_SPACING_MM * steps
        elif relation == "below":
            y += AUTO_LAYOUT_ROW_SPACING_MM * steps

        await call_checked(
            "sch_move_symbol",
            {
                "reference": reference,
                "x_mm": x,
                "y_mm": y,
                "snap_to_grid": True,
            },
        )

        updated = parse_schematic_file(schematic)
        placed = _symbol_by_reference(updated, reference)
        actual_x = float(placed.get("x", placed.get("x_mm", 0.0)) or 0.0)
        actual_y = float(placed.get("y", placed.get("y_mm", 0.0)) or 0.0)
        return (
            f"Placed {reference} {relation} {anchor_ref} using MCP Pro spacing "
            f"at ({actual_x:.2f}, {actual_y:.2f}) mm."
        )

    @server.tool()
    async def sch_move_block_relative(
        subject_refs: list[str],
        anchor_refs: list[str],
        relation: str,
        spacing_mm: float | None = None,
    ) -> str:
        """Move a functional block rigidly relative to another block.

        Block bounds use MCP Pro's pin-aware symbol bounding boxes. The subject
        block is translated as one rigid group; internal member spacing is
        preserved. When spacing_mm is omitted, the project's functional spacing
        design intent is used.
        """
        if relation not in _RELATIONS:
            raise ValueError(
                "relation must be one of: " + ", ".join(sorted(_RELATIONS))
            )
        subject_refs = [str(ref).strip() for ref in subject_refs if str(ref).strip()]
        anchor_refs = [str(ref).strip() for ref in anchor_refs if str(ref).strip()]
        if not subject_refs or not anchor_refs:
            raise ValueError("subject_refs and anchor_refs must be non-empty")
        if set(subject_refs) & set(anchor_refs):
            raise ValueError("subject and anchor blocks must not share references")

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)

        def block_bounds(references: list[str]) -> tuple[float, float, float, float]:
            bounds = [
                _symbol_bbox_bounds(_symbol_by_reference(data, reference))
                for reference in references
            ]
            return (
                min(item[0] for item in bounds),
                min(item[1] for item in bounds),
                max(item[2] for item in bounds),
                max(item[3] for item in bounds),
            )

        subject_bounds = block_bounds(subject_refs)
        anchor_bounds = block_bounds(anchor_refs)
        if spacing_mm is None:
            try:
                spacing = float(_load_layout_design_intent().functional_spacing_mm)
            except Exception:
                spacing = 5.0
        else:
            spacing = float(spacing_mm)
        spacing = max(SCHEMATIC_GRID_MM, spacing)

        sx1, sy1, sx2, sy2 = subject_bounds
        ax1, ay1, ax2, ay2 = anchor_bounds
        subject_cx = (sx1 + sx2) / 2.0
        subject_cy = (sy1 + sy2) / 2.0
        anchor_cx = (ax1 + ax2) / 2.0
        anchor_cy = (ay1 + ay2) / 2.0

        dx = 0.0
        dy = 0.0
        if relation == "left_of":
            dx = (ax1 - spacing) - sx2
            dy = anchor_cy - subject_cy
        elif relation == "right_of":
            dx = (ax2 + spacing) - sx1
            dy = anchor_cy - subject_cy
        elif relation == "above":
            dx = anchor_cx - subject_cx
            dy = (ay1 - spacing) - sy2
        elif relation == "below":
            dx = anchor_cx - subject_cx
            dy = (ay2 + spacing) - sy1

        # Quantize the group translation once so every member receives exactly
        # the same grid-aligned delta and the internal block geometry stays rigid.
        dx = round(dx / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
        dy = round(dy / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM

        moved: list[dict[str, Any]] = []
        for reference in subject_refs:
            symbol = _symbol_by_reference(data, reference)
            x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
            y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
            await call_checked(
                "sch_move_symbol",
                {
                    "reference": reference,
                    "x_mm": x + dx,
                    "y_mm": y + dy,
                    "snap_to_grid": True,
                },
            )
            moved.append(
                {
                    "reference": reference,
                    "from": [round(x, 4), round(y, 4)],
                    "to": [round(x + dx, 4), round(y + dy, 4)],
                }
            )

        return json.dumps(
            {
                "relation": relation,
                "spacing_mm": spacing,
                "translation_mm": [round(dx, 4), round(dy, 4)],
                "subject_bounds_before": [round(value, 4) for value in subject_bounds],
                "anchor_bounds": [round(value, 4) for value in anchor_bounds],
                "moved": moved,
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_move_block_to_layout_offset(
        subject_refs: list[str],
        anchor_refs: list[str],
        offset_x_mm: float,
        offset_y_mm: float,
        move_refs: list[str] | None = None,
    ) -> str:
        """Place one rigid functional block at an ELK-style offset from an anchor.

        The caller supplies only a macro-layout vector. MCP Pro measures the
        actual current KiCad block bounds, maps that vector onto the anchor
        block's real center, snaps one rigid translation to the schematic grid,
        and delegates all symbol mutation to sch_move_symbol.
        """
        subject_refs = [str(ref).strip() for ref in subject_refs if str(ref).strip()]
        anchor_refs = [str(ref).strip() for ref in anchor_refs if str(ref).strip()]
        move_targets = (
            [str(ref).strip() for ref in move_refs if str(ref).strip()]
            if move_refs is not None
            else list(subject_refs)
        )
        if not subject_refs or not anchor_refs or not move_targets:
            raise ValueError("subject_refs, anchor_refs, and move_refs must be non-empty")
        if set(subject_refs) & set(anchor_refs):
            raise ValueError("subject and anchor blocks must not share references")

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)

        def block_bounds(references: list[str]) -> tuple[float, float, float, float]:
            bounds = [
                _symbol_bbox_bounds(_symbol_by_reference(data, reference))
                for reference in references
            ]
            return (
                min(item[0] for item in bounds),
                min(item[1] for item in bounds),
                max(item[2] for item in bounds),
                max(item[3] for item in bounds),
            )

        subject_bounds = block_bounds(subject_refs)
        anchor_bounds = block_bounds(anchor_refs)
        sx1, sy1, sx2, sy2 = subject_bounds
        ax1, ay1, ax2, ay2 = anchor_bounds
        subject_cx = (sx1 + sx2) / 2.0
        subject_cy = (sy1 + sy2) / 2.0
        anchor_cx = (ax1 + ax2) / 2.0
        anchor_cy = (ay1 + ay2) / 2.0

        target_cx = anchor_cx + float(offset_x_mm)
        target_cy = anchor_cy + float(offset_y_mm)
        dx = target_cx - subject_cx
        dy = target_cy - subject_cy
        dx = round(dx / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
        dy = round(dy / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM

        moved: list[dict[str, Any]] = []
        for reference in move_targets:
            symbol = _symbol_by_reference(data, reference)
            x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
            y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
            await call_checked(
                "sch_move_symbol",
                {
                    "reference": reference,
                    "x_mm": x + dx,
                    "y_mm": y + dy,
                    "snap_to_grid": True,
                },
            )
            moved.append(
                {
                    "reference": reference,
                    "from": [round(x, 4), round(y, 4)],
                    "to": [round(x + dx, 4), round(y + dy, 4)],
                }
            )

        return json.dumps(
            {
                "offset_mm": [round(float(offset_x_mm), 4), round(float(offset_y_mm), 4)],
                "translation_mm": [round(dx, 4), round(dy, 4)],
                "subject_bounds_before": [round(value, 4) for value in subject_bounds],
                "anchor_bounds": [round(value, 4) for value in anchor_bounds],
                "move_refs": move_targets,
                "moved": moved,
            },
            sort_keys=True,
        )

    @server.tool()
    def sch_measure_functional_blocks(
        blocks: list[dict[str, Any]],
    ) -> str:
        """Measure current functional-block geometry using MCP Pro bounds."""
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        measured: list[dict[str, Any]] = []
        for block in blocks:
            name = str(block.get("name", "")).strip()
            refs = [
                str(ref).strip()
                for ref in block.get("references", [])
                if str(ref).strip()
            ]
            if not name or not refs:
                continue
            bounds = [
                _symbol_bbox_bounds(_symbol_by_reference(data, reference))
                for reference in refs
            ]
            x1 = min(item[0] for item in bounds)
            y1 = min(item[1] for item in bounds)
            x2 = max(item[2] for item in bounds)
            y2 = max(item[3] for item in bounds)
            measured.append(
                {
                    "name": name,
                    "references": refs,
                    "bounds": [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)],
                    "center": [round((x1 + x2) / 2.0, 4), round((y1 + y2) / 2.0, 4)],
                    "width_mm": round(x2 - x1, 4),
                    "height_mm": round(y2 - y1, 4),
                }
            )
        return json.dumps({"blocks": measured}, sort_keys=True)

    @server.tool()
    async def sch_translate_references_to_margin(
        references: list[str],
        margin_mm: float = 12.7,
    ) -> str:
        """Translate a finished composition into positive sheet coordinates.

        The entire reference set receives one rigid, grid-aligned translation.
        No relative symbol or block geometry is changed.
        """
        refs = [str(ref).strip() for ref in references if str(ref).strip()]
        if not refs:
            raise ValueError("references must be non-empty")
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        bounds = [
            _symbol_bbox_bounds(_symbol_by_reference(data, reference))
            for reference in refs
        ]
        x1 = min(item[0] for item in bounds)
        y1 = min(item[1] for item in bounds)
        margin = max(SCHEMATIC_GRID_MM, float(margin_mm))
        dx = max(0.0, margin - x1)
        dy = max(0.0, margin - y1)
        dx = round(dx / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
        dy = round(dy / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM

        moved: list[dict[str, Any]] = []
        if dx or dy:
            for reference in refs:
                symbol = _symbol_by_reference(data, reference)
                x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
                y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
                await call_checked(
                    "sch_move_unit" if split_unit(reference)[1] is not None else "sch_move_symbol",
                    {
                        "reference": reference,
                        "x_mm": x + dx,
                        "y_mm": y + dy,
                        "snap_to_grid": True,
                    },
                )
                moved.append(
                    {
                        "reference": reference,
                        "from": [round(x, 4), round(y, 4)],
                        "to": [round(x + dx, 4), round(y + dy, 4)],
                    }
                )
        return json.dumps(
            {
                "margin_mm": margin,
                "translation_mm": [round(dx, 4), round(dy, 4)],
                "bounds_before": [round(item, 4) for item in (
                    min(value[0] for value in bounds),
                    min(value[1] for value in bounds),
                    max(value[2] for value in bounds),
                    max(value[3] for value in bounds),
                )],
                "moved": moved,
            },
            sort_keys=True,
        )

    def terminal_sheet(clearance_mm: float, ignore: frozenset[str] = frozenset()) -> SimpleNamespace:
        """The sheet as terminal placement sees it, plus what the current call has added to it.

        The symbols in ``ignore`` are not on it: a power flag that will ride on a wire afterwards stood where its relation
        had put it, under the pin whose ground symbol wanted that place, and sent the stub round it (`auto-demo10`, the
        buck converter's C101).

        With tight layout clearances a symbol is its body and pin tips: MCP Pro's
        legacy symbol box is 20.32 x 15.24 mm at minimum, which pushed terminals
        10 to 13 mm from their pins and left no candidate in a dense layout
        (FINDINGS section 3). The legacy layout keeps the legacy box.
        """
        from backend.schematic.kicad_geometry import measured_scene_from_kicad
        from backend.schematic.presentation_geometry import GeometryKind, TEXT_KINDS

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        tight = bool(layout_clearances["tight_terminals"])

        def terminal_bounds(item: dict[str, Any]) -> tuple[float, float, float, float]:
            if not tight:
                return _mcp_symbol_bbox_bounds(item)
            box = _placed_symbol_bbox(item)
            return mirror_box(
                (float(box.x_min), float(box.y_min), float(box.x_max), float(box.y_max)),
                (float(item.get("x", item.get("x_mm", 0.0)) or 0.0), float(item.get("y", item.get("y_mm", 0.0)) or 0.0)),
                str(item.get("mirror", "") or ""),
            )

        symbol_entries = [
            item
            for item in [*data.get("symbols", []), *data.get("power_symbols", [])]
            if str(item.get("reference", "")).strip() and str(item.get("reference", "")).strip() not in ignore
        ]
        symbol_entries.sort(key=_symbol_order_key)
        instances: dict[str, int] = {}
        for item in symbol_entries:
            instances[str(item["reference"])] = instances.get(str(item["reference"]), 0) + 1
        # Every pin tip, wire and text box already on the sheet: a stub that
        # touches the first two would join two nets, and text is an obstacle
        # too (a power symbol's Value text, a symbol's reference and value, pin
        # names and labels).
        from backend.schematic.field_placement import routing_text_obstacles
        scene, _coverage = measured_scene_from_kicad(schematic)
        scene = routing_text_obstacles(scene, tight)
        parts = [part for part in scene.items if part.owner not in ignore]
        return SimpleNamespace(
            data=data,
            tight=tight,
            clearance=max(SCHEMATIC_GRID_MM, float(clearance_mm)),
            bounds_by_ref={
                _placeable_name(item, instances): terminal_bounds(item)
                for item in symbol_entries
            },
            pin_tips={
                part.id[: -len(":terminal")]: (part.bounds.left, part.bounds.top)
                for part in parts
                if part.kind == GeometryKind.PIN_ENDPOINT
                and part.id.endswith(":terminal")
            },
            wires=[
                part.segment for part in parts
                if part.kind == GeometryKind.WIRE and part.segment is not None
            ],
            text_boxes=[
                part.bounds for part in parts if part.kind in TEXT_KINDS
            ],
            # What this call has added so far, starting with the labels already on the sheet: a label is fixed text (the
            # text of a symbol's fields is moved afterwards, a label's is not), and a net without a library power symbol
            # gets its labels one pin to a call, so a stub planned later must not run through one placed earlier
            # (`linear_regulator`: C1's stub ran through U1's VIN label).
            footprints=[part.bounds for part in parts if part.kind == GeometryKind.NET_LABEL],
            segments=[],
            pin_points=[],
            occupied=[],
        )

    def power_end_symbol(net: str) -> Any:
        """What a power symbol of this net occupies: it reaches away from its own pin, so a ground symbol hangs
        down the page and a supply symbol stands up."""
        from backend.schematic.terminal_placement import power_symbol_footprint

        try:
            pin_normal = get_pin_outward_normals("power", net, 0, 1)
            pin_normal_y = float(pin_normal.get("1", (0.0, -1.0))[1])
        except (KeyError, ValueError, FileNotFoundError, OSError):
            pin_normal_y = -1.0
        return power_symbol_footprint(net, pin_normal_y)

    def resolve_terminal_pin(
        data: dict[str, Any], reference: str, pin: str,
    ) -> tuple[float, float, float, float]:
        """A pin's position and the direction it points out of its symbol."""
        symbol = _symbol_by_reference(data, reference)
        lib_id = str(symbol.get("lib_id", ""))
        if ":" not in lib_id:
            raise ValueError(f"Symbol {reference} has no KiCad library ID")
        library, symbol_name = lib_id.split(":", 1)
        x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
        y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
        rotation = int(symbol.get("rotation", symbol.get("rot", 0)) or 0) % 360
        unit = int(symbol.get("unit", 1) or 1)
        mirror = str(symbol.get("mirror", "") or "")
        pins = get_pin_positions(library, symbol_name, x, y, rotation, unit, mirror=mirror)
        if pin not in pins:
            raise ValueError(f"Pin {reference}.{pin} was not found")
        px, py = (float(value) for value in pins[pin])

        normals = get_pin_outward_normals(library, symbol_name, rotation, unit, mirror=mirror)
        nx, ny = normals.get(pin, (0.0, 0.0))
        nx, ny = float(nx), float(ny)
        if abs(nx) + abs(ny) < 0.5:
            dx, dy = px - x, py - y
            if abs(dx) >= abs(dy) and abs(dx) > 1e-6:
                nx, ny = (1.0 if dx > 0 else -1.0), 0.0
            elif abs(dy) > 1e-6:
                nx, ny = 0.0, (1.0 if dy > 0 else -1.0)
            else:
                nx, ny = 0.0, -1.0
        return px, py, nx, ny

    def plan_terminal(
        sheet: SimpleNamespace,
        reference: str,
        pin: str,
        net: str,
        point: tuple[float, float],
        normal: tuple[float, float],
        end_symbol: Any,
    ) -> Any:
        """Plan the stub and end point of one terminal, or say which pin has no exit."""
        from backend.schematic.presentation_geometry import Bounds
        from backend.schematic.terminal_placement import choose_safe_terminal

        own_token = f"{reference}.{pin}"
        source_bbox = sheet.bounds_by_ref.get(reference)
        if source_bbox is not None:
            # A symbol's primitive bounds can reach past the tip of the pin the stub leaves (an LED's cathode end by 0.76 mm,
            # `corpus_piaa_power` D18): beyond the tip is air, not the part, and a stub that starts at the tip and leaves
            # along the pin's normal is never inside it. The box stops at the tip on the side the pin faces.
            left, top, right, bottom = source_bbox
            if normal[0] > 0.5:
                right = min(right, point[0])
            elif normal[0] < -0.5:
                left = max(left, point[0])
            if normal[1] > 0.5:
                bottom = min(bottom, point[1])
            elif normal[1] < -0.5:
                top = max(top, point[1])
            source_bbox = (left, top, right, bottom)
        try:
            return choose_safe_terminal(
                source=point,
                preferred_normal=normal,
                own_bounds=Bounds(*source_bbox) if source_bbox is not None else None,
                foreign_bounds=tuple(
                    Bounds(*bbox)
                    for other_ref, bbox in sheet.bounds_by_ref.items()
                    if other_ref != reference
                ),
                foreign_pin_points=tuple(
                    [p for token, p in sheet.pin_tips.items() if token != own_token]
                    + sheet.pin_points
                ),
                existing_wire_segments=tuple([*sheet.wires, *sheet.segments]),
                occupied_terminals=tuple(sheet.occupied),
                grid_mm=SCHEMATIC_GRID_MM,
                clearance_mm=sheet.clearance,
                end_symbol=end_symbol,
                avoid_boxes=tuple(sheet.footprints),
                soft_boxes=tuple(sheet.text_boxes),
            )
        except ValueError as error:
            raise ValueError(
                f"No unobstructed power terminal exit from {own_token} "
                f"at ({point[0]:.2f}, {point[1]:.2f}) for net {net}; reposition "
                f"its source or nearby geometry ({error})"
            ) from error

    def remember_terminal(
        sheet: SimpleNamespace, plan_segments: Any, position: tuple[float, float], footprint: Any,
    ) -> None:
        sheet.segments.extend(plan_segments)
        sheet.pin_points.append((float(position[0]), float(position[1])))
        sheet.occupied.append(position)
        if footprint is not None:
            sheet.footprints.append(footprint)

    @server.tool()
    def sch_add_wire_labels(labels: list[dict[str, Any]]) -> str:
        """Write each net's name on a point of its wire, in one transaction.

        A local label whose anchor lies on a wire joins that wire's net; nothing else is drawn. Each entry needs
        ``net``, ``x_mm``, ``y_mm`` and ``rotation`` (0, 90, 180 or 270).
        """
        from kicad_mcp.tools.schematic import label_block

        blocks: list[str] = []
        for entry in labels:
            net = str(entry.get("net", "")).strip()
            if not net:
                raise ValueError("wire labels need a net name")
            rotation = int(entry.get("rotation", 0)) % 360
            if rotation not in (0, 90, 180, 270):
                raise ValueError("wire label rotation must be 0, 90, 180 or 270")
            blocks.append(label_block(
                net, float(entry["x_mm"]), float(entry["y_mm"]), rotation, kind="label",
            ))

        def mutator(current: str) -> str:
            updated = current
            for block in blocks:
                updated = _append_before_sheet_instances(updated, block)
            return updated

        if blocks:
            _transactional_write_to_schematic(mutator)
        return json.dumps({"labels": len(blocks)}, sort_keys=True)

    @server.tool()
    def sch_add_text(items: list[dict[str, Any]]) -> str:
        """Write free text blocks (captions): each needs ``text``, ``x_mm``, ``y_mm`` and optional ``size_mm``."""
        blocks: list[str] = []
        for entry in items:
            text = str(entry.get("text", "")).strip()
            if not text:
                raise ValueError("text items need text")
            size = float(entry.get("size_mm", 1.27))
            blocks.append(
                f"\t(text {_sexpr_string(text)}\n"
                "\t\t(exclude_from_sim no)\n"
                f"\t\t(at {_fmt_mm(float(entry['x_mm']))} {_fmt_mm(float(entry['y_mm']))} 0)\n"
                "\t\t(effects\n"
                "\t\t\t(font\n"
                f"\t\t\t\t(size {_fmt_mm(size)} {_fmt_mm(size)})\n"
                "\t\t\t\t(bold yes)\n"
                "\t\t\t)\n"
                "\t\t\t(justify left bottom)\n"
                "\t\t)\n"
                f'\t\t(uuid "{new_uuid()}")\n'
                "\t)"
            )

        def mutator(current: str) -> str:
            updated = current
            for block in blocks:
                updated = _append_before_sheet_instances(updated, block)
            return updated

        if blocks:
            _transactional_write_to_schematic(mutator)
        return json.dumps({"texts": len(blocks)}, sort_keys=True)

    @server.tool()
    async def sch_add_pin_net_labels(
        connections: list[dict[str, Any]],
        label_kind: str = "local",
        clearance_mm: float = 3.81,
    ) -> str:
        """Put each pin on its net with a checked stub and a net label.

        Replaces MCP Pro's sch_add_pin_labels for the tight layout. That tool
        lengthens and staggers a stub until its label clears other terminals,
        without checking the wire it draws against other pins, wires or text
        (FINDINGS section 9: one lengthened stub shorted GND to a UART net).
        Here the stub and the label go through the same planner as power
        terminals, and the wires and labels are written with MCP Pro's own block
        builders in one transaction.
        """
        from kicad_mcp.tools.schematic import label_block, wire_block
        from backend.schematic.kicad_geometry import text_width_mm
        from backend.schematic.terminal_placement import LabelEnd

        if label_kind not in ("local", "global"):
            raise ValueError("label_kind must be 'local' or 'global'")
        label_size = 1.524        # what label_block writes
        sheet = terminal_sheet(clearance_mm)
        blocks: list[str] = []
        diagnostics: list[dict[str, Any]] = []
        for entry in connections:
            reference = str(entry.get("reference", "")).strip()
            pin = str(entry.get("pin", "")).strip()
            net = str(entry.get("net", entry.get("name", ""))).strip()
            if not reference or not pin or not net:
                raise ValueError("net label entries need reference, pin, and net")
            px, py, nx, ny = resolve_terminal_pin(sheet.data, reference, pin)
            end = LabelEnd(
                text_width_mm=text_width_mm(net, label_size),
                size_mm=label_size,
                flagged=label_kind == "global",
            )
            plan = plan_terminal(sheet, reference, pin, net, (px, py), (nx, ny), end)
            (ax, ay), (bx, by) = plan.segments[-1]
            ux, uy = (bx > ax) - (bx < ax), (by > ay) - (by < ay)
            rotation = 0 if ux > 0 else 180 if ux < 0 else 90 if uy < 0 else 270
            for (x1, y1), (x2, y2) in plan.segments:
                blocks.append(wire_block(x1, y1, x2, y2))
            blocks.append(label_block(
                net, plan.position[0], plan.position[1], rotation,
                kind="label" if label_kind == "local" else "global_label",
            ))
            remember_terminal(
                sheet, plan.segments, plan.position, end.footprint_at(plan.position, (ux, uy)),
            )
            diagnostics.append({
                "reference": reference, "pin": pin, "net": net,
                "pin_position": [round(px, 4), round(py, 4)],
                "label_position": [round(plan.position[0], 4), round(plan.position[1], 4)],
                "rotation": rotation,
                "strategy": plan.strategy,
                "length_mm": plan.length_mm,
            })

        def mutator(current: str) -> str:
            updated = current
            for block in blocks:
                updated = _append_before_sheet_instances(updated, block)
            return updated

        if blocks:
            _transactional_write_to_schematic(mutator)
        return json.dumps(
            {"label_kind": label_kind, "labels": diagnostics}, sort_keys=True,
        )

    @server.tool()
    async def sch_add_pin_power_terminals(
        connections: list[dict[str, Any]],
        clearance_mm: float = 3.81,
        ignore_references: list[str] | None = None,
    ) -> str:
        """Place obstacle-aware power terminals using MCP Pro geometry and writers.

        Terminal positions are selected from real pin geometry and current symbol
        bounds. All mutations still delegate to MCP Pro's sch_add_wire and
        sch_add_power_symbol tools. A stub never touches another pin or another
        wire; when no terminal fits, the call fails with the pin that has no
        exit instead of drawing an unchecked wire. ``ignore_references`` names
        symbols that are not obstacles (power flags that will be moved onto a wire).
        """
        ignore = frozenset(str(reference) for reference in ignore_references or ())
        sheet = terminal_sheet(clearance_mm, ignore)
        tight_layout = sheet.tight
        diagnostics: list[dict[str, Any]] = []

        def snap(value: float) -> float:
            return round(value / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM

        for entry in connections:
            reference = str(entry.get("reference", "")).strip()
            pin = str(entry.get("pin", "")).strip()
            net = str(entry.get("net", entry.get("name", ""))).strip()
            if not reference or not pin or not net:
                raise ValueError("power terminal entries need reference, pin, and net")

            # A semantic power net does not imply a KiCad power-library symbol
            # exists with the same name. VIN, VSYS and arbitrary supply names
            # must still create real electrical connectivity. Use a global
            # net label if the symbol library cannot resolve the rail name.
            try:
                has_library_power_symbol = bool(
                    get_pin_positions("power", net, 0.0, 0.0, 0, 1)
                )
            except (KeyError, ValueError, FileNotFoundError, OSError):
                has_library_power_symbol = False
            if not has_library_power_symbol:
                tool, payload = (
                    ("sch_add_pin_net_labels", {
                        "connections": [{"reference": reference, "pin": pin, "net": net}],
                        "label_kind": "global",
                        "clearance_mm": clearance_mm,
                    }) if tight_layout else
                    ("sch_add_pin_labels", {
                        "connections": [{"reference": reference, "pin": pin, "net": net}],
                        "stub_mm": 5.08,
                        "global_labels": True,
                        "label_kind": "global",
                    })
                )
                result = await call_checked(tool, payload)
                diagnostics.append(
                    {
                        "reference": reference,
                        "pin": pin,
                        "net": net,
                        "strategy": "global_label_no_power_library_symbol",
                        "fallback": False,
                        "result": str(result),
                    }
                )
                # The label call wrote to the sheet; the next terminal must see it.
                sheet = terminal_sheet(clearance_mm, ignore)
                continue

            px, py, nx, ny = resolve_terminal_pin(sheet.data, reference, pin)

            # The symbol at the end of the stub reaches away from its own pin,
            # so a ground symbol hangs down and a supply symbol stands up. The
            # layout that keeps MCP Pro's boxes (the rollback baseline) plans
            # the stub and its end point only, as it always did.
            end_symbol = power_end_symbol(net) if tight_layout else None
            # A negative rail's symbol hangs from its pin, upside down, as a ground symbol does.
            symbol_rotation = 0
            if end_symbol is not None and entry.get("hanging"):
                from backend.schematic.terminal_placement import hung_from_pin

                end_symbol, symbol_rotation = hung_from_pin(end_symbol, True)

            try:
                plan = plan_terminal(
                    sheet, reference, pin, net, (px, py), (nx, ny), end_symbol,
                )
            except ValueError:
                if tight_layout:
                    raise
                # The legacy layout is the rollback baseline and keeps its
                # last resort: the straight 20.32 mm wire, flagged unchecked.
                terminal = (snap(px + nx * 20.32), snap(py + ny * 20.32))
                chosen = (
                    terminal,
                    [(px, py, terminal[0], terminal[1])],
                    "fallback_direct_20.32",
                    True,
                )
            else:
                chosen = (
                    plan.position,
                    [(a[0], a[1], b[0], b[1]) for a, b in plan.segments],
                    plan.strategy,
                    False,
                )

            terminal, segments, strategy, fallback = chosen
            for x1, y1, x2, y2 in segments:
                await call_checked(
                    "sch_add_wire",
                    {
                        "x1_mm": float(x1),
                        "y1_mm": float(y1),
                        "x2_mm": float(x2),
                        "y2_mm": float(y2),
                        "snap_to_grid": True,
                    },
                )
            await call_checked(
                "sch_add_power_symbol",
                {
                    "name": net,
                    "x_mm": float(terminal[0]),
                    "y_mm": float(terminal[1]),
                    "rotation": symbol_rotation,
                    "snap_to_grid": True,
                },
            )
            if symbol_rotation:
                # the text keeps angle 0 and sits past the symbol, so it reads the right way up
                from backend.schematic.symbol_edit import upright_value_text

                anchor = (snap(terminal[0]), snap(terminal[1]))
                _transactional_write_to_schematic(
                    lambda current: upright_value_text(current, f"power:{net}", anchor[0], anchor[1])
                )
            remember_terminal(
                sheet,
                [((x1, y1), (x2, y2)) for x1, y1, x2, y2 in segments],
                terminal,
                end_symbol.footprint_at(terminal) if end_symbol is not None else None,
            )
            diagnostics.append(
                {
                    "reference": reference,
                    "pin": pin,
                    "net": net,
                    "pin_position": [round(px, 4), round(py, 4)],
                    "outward_normal": [nx, ny],
                    "terminal_position": [
                        round(terminal[0], 4),
                        round(terminal[1], 4),
                    ],
                    "strategy": strategy,
                    "fallback": fallback,
                    **({"hanging": True} if symbol_rotation else {}),
                    "segments": [
                        [round(value, 4) for value in segment]
                        for segment in segments
                    ],
                }
            )

        return json.dumps(
            {
                "clearance_mm": sheet.clearance,
                "terminals": diagnostics,
                "fallback_count": sum(
                    1 for item in diagnostics if item["fallback"]
                ),
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_route_pin_bus(
        reference: str,
        pin: str,
        taps: list[dict[str, str]],
    ) -> str:
        """Route a compact orthogonal fan bus from one pin to several tap pins.

        This mirrors SKiDL's snapped decoupling-cap bus idea but resolves every
        coordinate from MCP Pro's actual KiCad pin geometry and delegates every
        mutation to MCP Pro's sch_add_wire tool.
        """
        if not taps:
            raise ValueError("taps must be non-empty")

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)

        def pin_position(ref: str, pin_num: str) -> tuple[float, float]:
            symbol = _symbol_by_reference(data, ref)
            lib_id = str(symbol.get("lib_id", ""))
            if ":" not in lib_id:
                raise ValueError(f"Symbol {ref} has no KiCad library ID")
            library, symbol_name = lib_id.split(":", 1)
            x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
            y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
            rotation = int(
                symbol.get("rotation", symbol.get("rot", 0)) or 0
            ) % 360
            unit = int(symbol.get("unit", 1) or 1)
            positions = get_pin_positions(
                library, symbol_name, x, y, rotation, unit
            )
            if pin_num not in positions:
                raise ValueError(f"Pin {ref}.{pin_num} was not found")
            px, py = positions[pin_num]
            return float(px), float(py)

        target = pin_position(reference, pin)
        tap_positions = [
            (
                str(item["reference"]),
                str(item["pin"]),
                pin_position(str(item["reference"]), str(item["pin"])),
            )
            for item in taps
        ]

        def snap(value: float) -> float:
            return round(value / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM

        tx, ty = target
        xs = [position[0] for _ref, _pin, position in tap_positions]
        ys = [position[1] for _ref, _pin, position in tap_positions]
        x_spread = max(xs) - min(xs) if xs else 0.0
        y_spread = max(ys) - min(ys) if ys else 0.0

        segments: list[tuple[float, float, float, float]] = []
        if y_spread >= x_spread:
            trunk_x = snap(sum(xs) / len(xs))
            all_y = [ty, *ys]
            segments.append((tx, ty, trunk_x, ty))
            segments.append((trunk_x, min(all_y), trunk_x, max(all_y)))
            for _ref, _pin, (px, py) in tap_positions:
                segments.append((trunk_x, py, px, py))
            orientation = "vertical"
        else:
            trunk_y = snap(sum(ys) / len(ys))
            all_x = [tx, *xs]
            segments.append((tx, ty, tx, trunk_y))
            segments.append((min(all_x), trunk_y, max(all_x), trunk_y))
            for _ref, _pin, (px, py) in tap_positions:
                segments.append((px, trunk_y, px, py))
            orientation = "horizontal"

        written: list[list[float]] = []
        seen: set[tuple[float, float, float, float]] = set()
        for x1, y1, x2, y2 in segments:
            segment = (
                snap(x1), snap(y1), snap(x2), snap(y2)
            )
            if (
                abs(segment[0] - segment[2]) < 1e-6
                and abs(segment[1] - segment[3]) < 1e-6
            ):
                continue
            reverse = (
                segment[2], segment[3], segment[0], segment[1]
            )
            if segment in seen or reverse in seen:
                continue
            seen.add(segment)
            await call_checked(
                "sch_add_wire",
                {
                    "x1_mm": segment[0],
                    "y1_mm": segment[1],
                    "x2_mm": segment[2],
                    "y2_mm": segment[3],
                    "snap_to_grid": True,
                },
            )
            written.append([round(value, 4) for value in segment])

        return json.dumps(
            {
                "reference": reference,
                "pin": pin,
                "target": [round(tx, 4), round(ty, 4)],
                "taps": [
                    {
                        "reference": ref,
                        "pin": pin_num,
                        "position": [
                            round(position[0], 4),
                            round(position[1], 4),
                        ],
                    }
                    for ref, pin_num, position in tap_positions
                ],
                "orientation": orientation,
                "segments": written,
            },
            sort_keys=True,
        )

    def _relation_direction(name: str) -> tuple[float, float]:
        directions = {
            "left": (-1.0, 0.0),
            "right": (1.0, 0.0),
            "above": (0.0, -1.0),
            "below": (0.0, 1.0),
        }
        try:
            return directions[name]
        except KeyError as error:
            raise ValueError(
                "direction must be left, right, above, or below"
            ) from error

    def _relation_pin_positions(
        symbol: dict[str, Any],
        rotation: int,
    ) -> dict[str, tuple[float, float]]:
        lib_id = str(symbol.get("lib_id", ""))
        if ":" not in lib_id:
            raise ValueError(
                f"Symbol {symbol.get('reference', '?')} has no KiCad library ID"
            )
        library, symbol_name = lib_id.split(":", 1)
        x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
        y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
        unit = int(symbol.get("unit", 1) or 1)
        return {
            str(pin): (float(point[0]), float(point[1]))
            for pin, point in get_pin_positions(
                library,
                symbol_name,
                x,
                y,
                int(rotation) % 360,
                unit,
                mirror=str(symbol.get("mirror", "") or ""),
            ).items()
        }

    def _choose_extension_rotation(
        symbol: dict[str, Any],
        node_pin: str,
        far_pin: str | None,
        extend_toward: str | None,
    ) -> int:
        current = int(
            symbol.get("rotation", symbol.get("rot", 0)) or 0
        ) % 360
        if not far_pin or not extend_toward:
            return current
        dx_want, dy_want = _relation_direction(extend_toward)
        best: tuple[float, int] | None = None
        for rotation in (0, 90, 180, 270):
            pins = _relation_pin_positions(symbol, rotation)
            if node_pin not in pins or far_pin not in pins:
                continue
            nx, ny = pins[node_pin]
            fx, fy = pins[far_pin]
            dx = fx - nx
            dy = fy - ny
            length = (dx * dx + dy * dy) ** 0.5
            if length <= 1e-9:
                continue
            score = (dx / length) * dx_want + (dy / length) * dy_want
            tie = -min(
                (rotation - current) % 360,
                (current - rotation) % 360,
            ) / 1000.0
            candidate = (score + tie, rotation)
            if best is None or candidate > best:
                best = candidate
        if best is None:
            raise ValueError(
                f"Could not orient pins {node_pin}/{far_pin} on "
                f"{symbol.get('reference', '?')}"
            )
        return best[1]

    def _virtual_symbol_bounds(
        symbol: dict[str, Any],
        rotation: int,
        x: float,
        y: float,
    ) -> tuple[float, float, float, float]:
        virtual = dict(symbol)
        virtual["rotation"] = int(rotation) % 360
        virtual["x"] = float(x)
        virtual["y"] = float(y)
        virtual["x_mm"] = float(x)
        virtual["y_mm"] = float(y)
        return _symbol_bbox_bounds(virtual)

    def _bounds_conflict(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
        padding: float = 1.27,
    ) -> bool:
        ax1, ay1, ax2, ay2 = first
        bx1, by1, bx2, by2 = second
        return not (
            ax2 + padding <= bx1
            or bx2 + padding <= ax1
            or ay2 + padding <= by1
            or by2 + padding <= ay1
        )

    def _turn_distance(first: int, second: int) -> int:
        first = int(first) % 360
        second = int(second) % 360
        return min((first - second) % 360, (second - first) % 360)

    def _compress_orthogonal_points(
        points: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        compact: list[tuple[float, float]] = []
        for point in points:
            snapped = (
                round(float(point[0]) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM,
                round(float(point[1]) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM,
            )
            if compact and (
                abs(compact[-1][0] - snapped[0]) < 1e-9
                and abs(compact[-1][1] - snapped[1]) < 1e-9
            ):
                continue
            compact.append(snapped)

        changed = True
        while changed and len(compact) >= 3:
            changed = False
            reduced = [compact[0]]
            for index in range(1, len(compact) - 1):
                prior = reduced[-1]
                current = compact[index]
                following = compact[index + 1]
                if (
                    abs(prior[0] - current[0]) < 1e-9
                    and abs(current[0] - following[0]) < 1e-9
                ) or (
                    abs(prior[1] - current[1]) < 1e-9
                    and abs(current[1] - following[1]) < 1e-9
                ):
                    changed = True
                    continue
                reduced.append(current)
            reduced.append(compact[-1])
            compact = reduced
        return compact

    def _segment_hits_bounds(
        first: tuple[float, float],
        second: tuple[float, float],
        bounds: tuple[float, float, float, float],
        padding: float = 0.635,
    ) -> bool:
        x1, y1 = first
        x2, y2 = second
        left, top, right, bottom = bounds
        left -= padding
        top -= padding
        right += padding
        bottom += padding

        if abs(y1 - y2) < 1e-9:
            low, high = sorted((x1, x2))
            return top < y1 < bottom and high > left and low < right
        if abs(x1 - x2) < 1e-9:
            low, high = sorted((y1, y2))
            return left < x1 < right and high > top and low < bottom
        return True

    def _orthogonal_route_candidate(
        start: tuple[float, float],
        end: tuple[float, float],
        obstacles: list[tuple[float, float, float, float]],
    ) -> tuple[list[tuple[float, float]], float] | None:
        sx, sy = start
        ex, ey = end
        clearance = SCHEMATIC_GRID_MM * 2.0

        raw_candidates: list[list[tuple[float, float]]] = []
        if abs(sx - ex) < 1e-9 or abs(sy - ey) < 1e-9:
            raw_candidates.append([start, end])
        raw_candidates.extend(
            [
                [start, (ex, sy), end],
                [start, (sx, ey), end],
            ]
        )

        detour_x = {sx, ex}
        detour_y = {sy, ey}
        for left, top, right, bottom in obstacles:
            detour_x.add(left - clearance)
            detour_x.add(right + clearance)
            detour_y.add(top - clearance)
            detour_y.add(bottom + clearance)
        for x in sorted(detour_x):
            raw_candidates.append([start, (x, sy), (x, ey), end])
        for y in sorted(detour_y):
            raw_candidates.append([start, (sx, y), (ex, y), end])

        best: tuple[float, list[tuple[float, float]]] | None = None
        seen: set[tuple[tuple[float, float], ...]] = set()
        for raw in raw_candidates:
            points = _compress_orthogonal_points(raw)
            key = tuple(points)
            if key in seen or len(points) < 2:
                continue
            seen.add(key)
            blocked = False
            for first, second in zip(points, points[1:], strict=False):
                if any(
                    _segment_hits_bounds(first, second, bounds)
                    for bounds in obstacles
                ):
                    blocked = True
                    break
            if blocked:
                continue

            length = sum(
                abs(second[0] - first[0]) + abs(second[1] - first[1])
                for first, second in zip(points, points[1:], strict=False)
            )
            bends = max(0, len(points) - 2)
            # Bends dominate a few grid steps of extra wire. This makes
            # placement move before routing starts to snake around components.
            score = length + bends * SCHEMATIC_GRID_MM * 6.0
            candidate = (score, points)
            if best is None or candidate[0] < best[0]:
                best = candidate

        if best is None:
            return None
        return best[1], best[0]

    def _relation_base_position(
        subject: dict[str, Any],
        subject_pin: str,
        rotation: int,
        anchor: dict[str, Any],
        anchor_pin_position: tuple[float, float],
        side: str,
        distance_steps: int,
    ) -> tuple[float, float]:
        sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
        sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
        pins = _relation_pin_positions(subject, rotation)
        if subject_pin not in pins:
            raise ValueError(
                f"Pin {subject.get('reference', '?')}.{subject_pin} was not found"
            )
        px, py = pins[subject_pin]
        pin_rel_x, pin_rel_y = px - sx, py - sy
        subject_bounds = _virtual_symbol_bounds(subject, rotation, sx, sy)
        rel_left = subject_bounds[0] - sx
        rel_top = subject_bounds[1] - sy
        rel_right = subject_bounds[2] - sx
        rel_bottom = subject_bounds[3] - sy
        anchor_bounds = _symbol_bbox_bounds(anchor)
        gap = SCHEMATIC_GRID_MM * max(1, distance_steps)
        ax, ay = anchor_pin_position

        if side == "left":
            return anchor_bounds[0] - gap - rel_right, ay - pin_rel_y
        if side == "right":
            return anchor_bounds[2] + gap - rel_left, ay - pin_rel_y
        if side == "above":
            return ax - pin_rel_x, anchor_bounds[1] - gap - rel_bottom
        if side == "below":
            return ax - pin_rel_x, anchor_bounds[3] + gap - rel_top
        raise ValueError("side must resolve before placement")

    def _extension_penalty(
        subject: dict[str, Any],
        node_pin: str,
        far_pin: str | None,
        rotation: int,
        extend_toward: str | None,
        weight: float = 2.0,
    ) -> float:
        if not far_pin or not extend_toward:
            return 0.0
        pins = _relation_pin_positions(subject, rotation)
        if node_pin not in pins or far_pin not in pins:
            return SCHEMATIC_GRID_MM * 20.0
        # Rail direction is a preference, not an orientation command: at the default weight a quarter turn costs more.
        return extension_penalty(
            pins[node_pin], pins[far_pin], _relation_direction(extend_toward),
            grid_mm=SCHEMATIC_GRID_MM, weight=weight,
        )

    def _pin_flow_direction(
        subject: dict[str, Any],
        node_pin: str,
        far_pin: str | None,
        rotation: int,
    ) -> str | None:
        if not far_pin:
            return None
        pins = _relation_pin_positions(subject, rotation)
        if node_pin not in pins or far_pin not in pins:
            return None
        nx, ny = pins[node_pin]
        fx, fy = pins[far_pin]
        dx, dy = fx - nx, fy - ny
        if abs(dx) >= abs(dy) and abs(dx) > 1e-9:
            return "right" if dx > 0 else "left"
        if abs(dy) > 1e-9:
            return "below" if dy > 0 else "above"
        return None

    def _resolved_side_from_pin(
        symbol: dict[str, Any],
        pin: str,
        rotation: int,
    ) -> str:
        lib_id = str(symbol.get("lib_id", ""))
        if ":" not in lib_id:
            raise ValueError("anchor symbol has no KiCad library ID")
        library, symbol_name = lib_id.split(":", 1)
        unit = int(symbol.get("unit", 1) or 1)
        normals = get_pin_outward_normals(
            library,
            symbol_name,
            int(rotation) % 360,
            unit,
            mirror=str(symbol.get("mirror", "") or ""),
        )
        nx, ny = normals.get(pin, (0.0, 0.0))
        nx, ny = float(nx), float(ny)
        if abs(nx) >= abs(ny) and abs(nx) > 0.25:
            return "right" if nx > 0 else "left"
        if abs(ny) > 0.25:
            return "below" if ny > 0 else "above"

        x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
        y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
        pins = _relation_pin_positions(symbol, rotation)
        if pin not in pins:
            raise ValueError(f"Pin {symbol.get('reference', '?')}.{pin} not found")
        px, py = pins[pin]
        dx, dy = px - x, py - y
        if abs(dx) >= abs(dy):
            return "right" if dx >= 0 else "left"
        return "below" if dy >= 0 else "above"

    def _place_outside_anchor_bounds(
        subject: dict[str, Any],
        subject_pin: str,
        rotation: int,
        anchor: dict[str, Any],
        anchor_pin_position: tuple[float, float],
        side: str,
        distance_steps: int,
        avoid_bounds: list[tuple[float, float, float, float]],
    ) -> tuple[float, float, tuple[float, float]] | None:
        sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
        sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
        pins = _relation_pin_positions(subject, rotation)
        if subject_pin not in pins:
            raise ValueError(
                f"Pin {subject.get('reference', '?')}.{subject_pin} was not found"
            )
        px, py = pins[subject_pin]
        pin_rel_x, pin_rel_y = px - sx, py - sy
        subject_bounds = _virtual_symbol_bounds(subject, rotation, sx, sy)
        rel_left = subject_bounds[0] - sx
        rel_top = subject_bounds[1] - sy
        rel_right = subject_bounds[2] - sx
        rel_bottom = subject_bounds[3] - sy
        anchor_bounds = _symbol_bbox_bounds(anchor)
        gap = SCHEMATIC_GRID_MM * max(1, distance_steps)

        ax, ay = anchor_pin_position
        if side == "left":
            target_x = anchor_bounds[0] - gap - rel_right
            target_y = ay - pin_rel_y
        elif side == "right":
            target_x = anchor_bounds[2] + gap - rel_left
            target_y = ay - pin_rel_y
        elif side == "above":
            target_x = ax - pin_rel_x
            target_y = anchor_bounds[1] - gap - rel_bottom
        elif side == "below":
            target_x = ax - pin_rel_x
            target_y = anchor_bounds[3] + gap - rel_top
        else:
            raise ValueError("side must resolve before placement")

        step_x, step_y = _relation_direction(side)
        step = SCHEMATIC_GRID_MM * 2.0
        for attempt in range(10):
            candidate_x = target_x + step_x * step * attempt
            candidate_y = target_y + step_y * step * attempt
            candidate_x = round(candidate_x / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
            candidate_y = round(candidate_y / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
            candidate_bounds = _virtual_symbol_bounds(
                subject,
                rotation,
                candidate_x,
                candidate_y,
            )
            if not any(
                _bounds_conflict(candidate_bounds, other)
                for other in (*avoid_bounds, anchor_bounds)
            ):
                target_pin = (
                    candidate_x + pin_rel_x,
                    candidate_y + pin_rel_y,
                )
                return candidate_x, candidate_y, target_pin

        # No collision-free candidate exists. Report infeasibility rather than
        # silently returning a position that overlaps a neighbour or anchor.
        return None

    @server.tool()
    async def sch_plan_relation_lanes(
        groups: list[dict[str, Any]],
        lane_pitch_steps: int = 4,
    ) -> str:
        """Assign outward lanes to relation groups from real anchor geometry."""
        if lane_pitch_steps < 1 or lane_pitch_steps > 12:
            raise ValueError("lane_pitch_steps must be between 1 and 12")
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        planned: dict[str, dict[str, Any]] = {}
        buckets: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}

        for group in groups:
            name = str(group["name"])
            side = str(group["side"])
            _relation_direction(side)
            raw_anchors = list(group.get("anchors", []))
            if not raw_anchors:
                continue
            points: list[tuple[float, float]] = []
            anchor_refs: list[str] = []
            for raw in raw_anchors:
                reference = str(raw["reference"])
                pin = str(raw["pin"])
                symbol = _symbol_by_reference(data, reference)
                rotation = int(
                    symbol.get("rotation", symbol.get("rot", 0)) or 0
                ) % 360
                pins = _relation_pin_positions(symbol, rotation)
                if pin not in pins:
                    raise ValueError(f"Pin {reference}.{pin} was not found")
                points.append(pins[pin])
                anchor_refs.append(reference)

            transverse = (
                sum(point[1] for point in points) / len(points)
                if side in {"left", "right"}
                else sum(point[0] for point in points) / len(points)
            )
            bucket_key = (side, tuple(sorted(set(anchor_refs))))
            buckets.setdefault(bucket_key, []).append(
                {
                    "name": name,
                    "transverse": transverse,
                    "references": list(group.get("references", [])),
                    "anchors": raw_anchors,
                }
            )

        for (side, anchor_refs), items in buckets.items():
            ordered = sorted(
                items,
                key=lambda item: (
                    float(item["transverse"]),
                    str(item["name"]),
                ),
            )
            for lane_rank, item in enumerate(ordered):
                planned[str(item["name"])] = {
                    "side": side,
                    "anchor_refs": list(anchor_refs),
                    "lane_rank": lane_rank,
                    "extra_steps": lane_rank * lane_pitch_steps,
                    "transverse": round(float(item["transverse"]), 4),
                    "references": item["references"],
                }

        return json.dumps(
            {
                "lane_pitch_steps": lane_pitch_steps,
                "groups": planned,
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_place_node_cluster(
        anchor_ref: str,
        anchor_pin: str,
        side: str,
        members: list[dict[str, Any]],
        distance_steps: int = 2,
        anchor_rotation: int | None = None,
        avoid_refs: list[str] | None = None,
    ) -> str:
        """Solve a shared-node relation cluster with CP-SAT."""
        from backend.kicad.cpsat_relation_solver import (
            PlacementCandidate,
            solve_relation_group,
        )
        from kicad_mcp.tools.schematic import _placed_symbol_bbox
        from backend.schematic.kicad_geometry import measured_scene_from_kicad
        from backend.schematic.geometry_constraints import (
            reservations_for_owners,
            relocated_reservations,
            overlap_pairs,
            segment_intersects_reservation,
        )

        if not members:
            raise ValueError("node cluster members must be non-empty")
        if len(members) > 6:
            raise ValueError("CP-SAT node cluster supports up to six members")
        if distance_steps < 1 or distance_steps > 40:
            raise ValueError("distance_steps must be between 1 and 40")
        _relation_direction(side)

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        presentation_scene, presentation_coverage = measured_scene_from_kicad(schematic)
        from backend.schematic.field_placement import routing_text_obstacles
        presentation_scene = routing_text_obstacles(presentation_scene, bool(layout_clearances["tight_terminals"]))

        def body_bounds(
            symbol: dict[str, Any],
            rotation: int | None = None,
            x: float | None = None,
            y: float | None = None,
        ) -> tuple[float, float, float, float]:
            virtual = dict(symbol)
            if rotation is not None:
                virtual["rotation"] = int(rotation) % 360
            if x is not None:
                virtual["x"] = float(x)
                virtual["x_mm"] = float(x)
            if y is not None:
                virtual["y"] = float(y)
                virtual["y_mm"] = float(y)
            box = _placed_symbol_bbox(virtual)
            return (
                float(box.x_min),
                float(box.y_min),
                float(box.x_max),
                float(box.y_max),
            )

        anchor = _symbol_by_reference(data, anchor_ref)
        resolved_anchor_rotation = (
            int(anchor_rotation) % 360
            if anchor_rotation is not None
            else int(anchor.get("rotation", anchor.get("rot", 0)) or 0) % 360
        )
        anchor_pins = _relation_pin_positions(anchor, resolved_anchor_rotation)
        if anchor_pin not in anchor_pins:
            raise ValueError(f"Pin {anchor_ref}.{anchor_pin} was not found")
        anchor_point = anchor_pins[anchor_pin]

        raw_members: list[dict[str, Any]] = []
        member_refs: set[str] = set()
        for raw in members:
            reference = str(raw["reference"])
            if reference in member_refs:
                raise ValueError("node cluster member references must be unique")
            pin = str(raw["pin"])
            far_pin_raw = raw.get("far_pin")
            far_pin = str(far_pin_raw) if far_pin_raw else None
            extend_raw = raw.get("extend_toward")
            extend = str(extend_raw) if extend_raw else None
            subject = _symbol_by_reference(data, reference)
            current_rotation = int(
                subject.get("rotation", subject.get("rot", 0)) or 0
            ) % 360
            raw_members.append(
                {
                    "reference": reference,
                    "pin": pin,
                    "far_pin": far_pin,
                    "extend_toward": extend,
                    "subject": subject,
                    "current_rotation": current_rotation,
                }
            )
            member_refs.add(reference)

        avoid = {
            str(item).strip()
            for item in (avoid_refs or [])
            if str(item).strip()
            and str(item).strip() not in member_refs
            and str(item).strip() != anchor_ref
        }
        avoid_bounds = [
            body_bounds(_symbol_by_reference(data, reference))
            for reference in sorted(avoid)
        ]
        fixed_bounds = [
            *avoid_bounds,
            body_bounds(anchor),
        ]
        # The same typed presentation reservations used by libavoid are also
        # used by candidate selection. Fixed symbol graphics *and annotations*
        # block prospective placed symbols; no reference-name special cases.
        fixed_presentation = reservations_for_owners(
            presentation_scene, [*sorted(avoid), anchor_ref],
        )
        owner_presentation = {
            item["reference"]: reservations_for_owners(
                presentation_scene, [item["reference"]],
            )
            for item in raw_members
        }

        grid = SCHEMATIC_GRID_MM
        outward_dx, outward_dy = _relation_direction(side)
        transverse = (
            (0.0, 1.0)
            if side in {"left", "right"}
            else (1.0, 0.0)
        )
        transverse_offsets = [0, -2, 2, -4, 4, -6, 6, -8, 8]
        base_node = (
            anchor_point[0] + outward_dx * grid * max(1, distance_steps),
            anchor_point[1] + outward_dy * grid * max(1, distance_steps),
        )

        junction_costs: dict[str, int] = {}
        junction_meta: dict[str, dict[str, Any]] = {}
        for outward_steps in range(0, 9, 2):
            for transverse_steps in transverse_offsets:
                node = (
                    base_node[0]
                    + outward_dx * grid * outward_steps
                    + transverse[0] * grid * transverse_steps,
                    base_node[1]
                    + outward_dy * grid * outward_steps
                    + transverse[1] * grid * transverse_steps,
                )
                node = (
                    round(node[0] / grid) * grid,
                    round(node[1] / grid) * grid,
                )
                route = _orthogonal_route_candidate(
                    anchor_point,
                    node,
                    avoid_bounds,
                )
                if route is None:
                    continue
                route_points, route_score = route
                junction_id = (
                    f"j_{outward_steps}_{transverse_steps:+d}"
                )
                soft_cost = (
                    route_score
                    + abs(transverse_steps) * grid * 0.35
                    + outward_steps * grid * 0.15
                )
                junction_costs[junction_id] = int(round(soft_cost * 100.0))
                junction_meta[junction_id] = {
                    "node": node,
                    "route_points": route_points,
                    "outward_steps": outward_steps,
                    "transverse_steps": transverse_steps,
                }

        placement_candidates: list[PlacementCandidate] = []
        candidate_meta: dict[str, dict[str, Any]] = {}
        candidate_presentation: dict[str, tuple[Any, ...]] = {}
        discarded_for_presentation = 0
        by_member_junction: dict[tuple[str, str], list[str]] = {}

        for item in raw_members:
            subject = item["subject"]
            sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
            sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
            rotations = (
                (0, 90, 180, 270)
                if item["far_pin"]
                else (item["current_rotation"],)
            )
            for junction_id, jmeta in junction_meta.items():
                node = jmeta["node"]
                for rotation in rotations:
                    pins = _relation_pin_positions(subject, int(rotation))
                    if item["pin"] not in pins:
                        continue
                    px, py = pins[item["pin"]]
                    pin_rel_x, pin_rel_y = px - sx, py - sy
                    pin_side = _resolved_side_from_pin(
                        subject,
                        item["pin"],
                        int(rotation),
                    )
                    pin_dx, pin_dy = _relation_direction(pin_side)

                    # Separate-symbol pins never occupy the junction coordinate.
                    # The pin faces the junction across a short visible wire.
                    for stub_steps in (2, 3, 4):
                        target_pin = (
                            node[0] - pin_dx * grid * stub_steps,
                            node[1] - pin_dy * grid * stub_steps,
                        )
                        x = round(
                            (target_pin[0] - pin_rel_x) / grid
                        ) * grid
                        y = round(
                            (target_pin[1] - pin_rel_y) / grid
                        ) * grid
                        target_pin = (
                            x + pin_rel_x,
                            y + pin_rel_y,
                        )
                        bounds = body_bounds(
                            subject,
                            int(rotation),
                            x,
                            y,
                        )
                        candidate_boxes = relocated_reservations(
                            owner_presentation[item["reference"]],
                            old_origin=(sx, sy),
                            new_origin=(x, y),
                            rotation_delta_degrees=(
                                int(rotation) - item["current_rotation"]
                            ),
                        )
                        if overlap_pairs(
                            candidate_boxes,
                            fixed_presentation,
                            clearance_mm=0.254,
                        ):
                            discarded_for_presentation += 1
                            continue

                        if any(
                            _bounds_conflict(
                                bounds,
                                other,
                                padding=0.254,
                            )
                            for other in fixed_bounds
                        ):
                            continue
                        if any(
                            _segment_hits_bounds(
                                target_pin,
                                node,
                                other,
                                padding=0.0,
                            )
                            for other in avoid_bounds
                        ):
                            continue

                        cost = _extension_penalty(
                            subject,
                            item["pin"],
                            item["far_pin"],
                            int(rotation),
                            item["extend_toward"],
                        )
                        if (
                            int(rotation) % 180
                            != item["current_rotation"] % 180
                        ):
                            cost += grid * 3.0

                        flow = _pin_flow_direction(
                            subject,
                            item["pin"],
                            item["far_pin"],
                            int(rotation),
                        )
                        if flow is not None:
                            flow_dx, flow_dy = _relation_direction(flow)
                            toward_anchor = -(
                                flow_dx * outward_dx
                                + flow_dy * outward_dy
                            )
                            if toward_anchor > 0:
                                cost += toward_anchor * grid * 8.0

                        # Prefer a clearly visible but compact two-grid stub.
                        cost += (stub_steps - 2) * grid * 0.5
                        candidate_id = (
                            f"{item['reference']}::{junction_id}"
                            f"::r{int(rotation) % 360}::s{stub_steps}"
                        )
                        placement_candidates.append(
                            PlacementCandidate(
                                candidate_id=candidate_id,
                                member=item["reference"],
                                junction_id=junction_id,
                                cost=int(round(cost * 100.0)),
                            )
                        )
                        candidate_presentation[candidate_id] = candidate_boxes
                        candidate_meta[candidate_id] = {
                            **item,
                            "rotation": int(rotation) % 360,
                            "x": x,
                            "y": y,
                            "bounds": bounds,
                            "flow_direction": flow,
                            "pin_position": target_pin,
                            "pin_side": pin_side,
                            "stub_steps": stub_steps,
                        }
                        by_member_junction.setdefault(
                            (item["reference"], junction_id),
                            [],
                        ).append(candidate_id)

        incompatible_pairs: list[tuple[str, str]] = []
        member_names = [item["reference"] for item in raw_members]
        for junction_id in junction_meta:
            for first_index, first_member in enumerate(member_names):
                for second_member in member_names[first_index + 1 :]:
                    first_ids = by_member_junction.get(
                        (first_member, junction_id),
                        [],
                    )
                    second_ids = by_member_junction.get(
                        (second_member, junction_id),
                        [],
                    )
                    for first_id in first_ids:
                        for second_id in second_ids:
                            first_bounds = candidate_meta[first_id]["bounds"]
                            second_bounds = candidate_meta[second_id]["bounds"]
                            first_meta = candidate_meta[first_id]
                            second_meta = candidate_meta[second_id]
                            node = junction_meta[junction_id]["node"]
                            body_conflict = _bounds_conflict(
                                first_bounds,
                                second_bounds,
                                padding=0.254,
                            )
                            first_stub_hits_second = _segment_hits_bounds(
                                first_meta["pin_position"],
                                node,
                                second_bounds,
                                padding=0.0,
                            )
                            second_stub_hits_first = _segment_hits_bounds(
                                second_meta["pin_position"],
                                node,
                                first_bounds,
                                padding=0.0,
                            )
                            presentation_conflict = bool(overlap_pairs(
                                candidate_presentation[first_id],
                                candidate_presentation[second_id],
                                clearance_mm=0.254,
                            ))
                            # Attachment stubs may not cross another component's
                            # visible annotation even if neither symbol body
                            # intersects the wire.
                            first_stub_hits_second_text = any(
                                segment_intersects_reservation(
                                    first_meta["pin_position"], node, reservation,
                                )
                                for reservation in candidate_presentation[second_id]
                            )
                            second_stub_hits_first_text = any(
                                segment_intersects_reservation(
                                    second_meta["pin_position"], node, reservation,
                                )
                                for reservation in candidate_presentation[first_id]
                            )
                            if (
                                body_conflict
                                or first_stub_hits_second
                                or second_stub_hits_first
                                or presentation_conflict
                                or first_stub_hits_second_text
                                or second_stub_hits_first_text
                            ):
                                incompatible_pairs.append(
                                    (first_id, second_id)
                                )

        if not junction_costs or not placement_candidates:
            raise ValueError("Could not generate CP-SAT node-cluster candidates")

        solution = solve_relation_group(
            required_members=member_refs,
            junction_costs=junction_costs,
            candidates=placement_candidates,
            incompatible_pairs=incompatible_pairs,
            max_time_seconds=2.0,
        )
        jmeta = junction_meta[solution.junction_id]
        node = jmeta["node"]
        chosen = [
            candidate_meta[candidate_id]
            for candidate_id in solution.candidate_ids
        ]

        rotations: dict[str, int] = {}
        placed: list[dict[str, Any]] = []
        for item in chosen:
            await call_checked(
                "sch_move_symbol",
                {
                    "reference": item["reference"],
                    "x_mm": item["x"],
                    "y_mm": item["y"],
                    "snap_to_grid": True,
                },
            )
            rotations[item["reference"]] = int(item["rotation"]) % 360
            placed.append(
                {
                    "reference": item["reference"],
                    "rotation": int(item["rotation"]) % 360,
                    "position": [
                        round(float(item["x"]), 4),
                        round(float(item["y"]), 4),
                    ],
                    "body_bounds": [
                        round(float(value), 4)
                        for value in item["bounds"]
                    ],
                    "pin_position": [
                        round(float(value), 4)
                        for value in item["pin_position"]
                    ],
                    "pin_side": item["pin_side"],
                    "stub_steps": item["stub_steps"],
                    "extend_toward": item["extend_toward"],
                }
            )

        return json.dumps(
            {
                "anchor": f"{anchor_ref}.{anchor_pin}",
                "side": side,
                "distance_steps": distance_steps,
                "node_position": [round(node[0], 4), round(node[1], 4)],
                "anchor_route_points": [
                    [round(x, 4), round(y, 4)]
                    for x, y in jmeta["route_points"]
                ],
                "solver": "cp_sat",
                "solver_status": solution.status,
                "objective": solution.objective,
                "candidate_count": len(placement_candidates),
                "conflict_count": len(incompatible_pairs),
                "presentation_constraints": {
                    "model": "typed_scene_v1",
                    "fixed_reservations": len(fixed_presentation),
                    "discarded_candidates": discarded_for_presentation,
                    "candidate_reservation_shapes": sum(
                        len(value) for value in candidate_presentation.values()
                    ),
                    "coverage": presentation_coverage,
                },
                "rotations": rotations,
                "placed": placed,
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_place_relation_band(
        entries: list[dict[str, Any]],
        side: str,
        distance_steps: int = 2,
        avoid_refs: list[str] | None = None,
    ) -> str:
        """Place sibling pin relations together in one packed side band."""
        if len(entries) < 2:
            raise ValueError("relation band requires at least two entries")
        if distance_steps < 1 or distance_steps > 40:
            raise ValueError("distance_steps must be between 1 and 40")
        _relation_direction(side)

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        prepared: list[dict[str, Any]] = []
        anchor_bounds: list[tuple[float, float, float, float]] = []

        for raw in entries:
            reference = str(raw["reference"])
            pin = str(raw["pin"])
            anchor_ref = str(raw["anchor_ref"])
            anchor_pin = str(raw["anchor_pin"])
            far_pin_raw = raw.get("far_pin")
            far_pin = str(far_pin_raw) if far_pin_raw else None
            extend_raw = raw.get("extend_toward")
            extend = str(extend_raw) if extend_raw else None
            anchor_rotation_raw = raw.get("anchor_rotation")

            subject = _symbol_by_reference(data, reference)
            anchor = _symbol_by_reference(data, anchor_ref)
            anchor_rotation = (
                int(anchor_rotation_raw) % 360
                if anchor_rotation_raw is not None
                else int(anchor.get("rotation", anchor.get("rot", 0)) or 0) % 360
            )
            anchor_pins = _relation_pin_positions(anchor, anchor_rotation)
            if anchor_pin not in anchor_pins:
                raise ValueError(f"Pin {anchor_ref}.{anchor_pin} was not found")

            rotation = _choose_extension_rotation(
                subject,
                pin,
                far_pin,
                extend,
            )
            sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
            sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
            pins = _relation_pin_positions(subject, rotation)
            if pin not in pins:
                raise ValueError(f"Pin {reference}.{pin} was not found")
            px, py = pins[pin]
            bbox = _virtual_symbol_bounds(subject, rotation, sx, sy)
            prepared.append(
                {
                    "reference": reference,
                    "pin": pin,
                    "anchor_ref": anchor_ref,
                    "anchor_pin": anchor_pin,
                    "subject": subject,
                    "rotation": rotation,
                    "pin_rel": (px - sx, py - sy),
                    "bbox_rel": (
                        bbox[0] - sx,
                        bbox[1] - sy,
                        bbox[2] - sx,
                        bbox[3] - sy,
                    ),
                    "anchor_point": anchor_pins[anchor_pin],
                    "extend_toward": extend,
                }
            )
            anchor_bounds.append(_symbol_bbox_bounds(anchor))

        gap = SCHEMATIC_GRID_MM * max(1, distance_steps)
        if side in {"above", "below"}:
            if side == "above":
                edge = min(bounds[1] for bounds in anchor_bounds)
                required = max(
                    item["bbox_rel"][3] - item["pin_rel"][1]
                    for item in prepared
                )
                band_axis = edge - gap - required
            else:
                edge = max(bounds[3] for bounds in anchor_bounds)
                required = max(
                    item["pin_rel"][1] - item["bbox_rel"][1]
                    for item in prepared
                )
                band_axis = edge + gap + required

            ordered = sorted(
                prepared,
                key=lambda item: float(item["anchor_point"][0]),
            )
            targets: list[float] = []
            for index, item in enumerate(ordered):
                desired = float(item["anchor_point"][0])
                if not targets:
                    targets.append(desired)
                    continue
                prev = ordered[index - 1]
                prev_right = (
                    targets[-1]
                    + prev["bbox_rel"][2]
                    - prev["pin_rel"][0]
                )
                min_target = (
                    prev_right
                    + SCHEMATIC_GRID_MM * 2.0
                    - item["bbox_rel"][0]
                    + item["pin_rel"][0]
                )
                targets.append(max(desired, min_target))
            shift = (
                sum(float(item["anchor_point"][0]) for item in ordered)
                - sum(targets)
            ) / len(targets)
            targets = [value + shift for value in targets]
            target_pins = [
                (targets[index], band_axis)
                for index in range(len(ordered))
            ]
        else:
            if side == "left":
                edge = min(bounds[0] for bounds in anchor_bounds)
                required = max(
                    item["bbox_rel"][2] - item["pin_rel"][0]
                    for item in prepared
                )
                band_axis = edge - gap - required
            else:
                edge = max(bounds[2] for bounds in anchor_bounds)
                required = max(
                    item["pin_rel"][0] - item["bbox_rel"][0]
                    for item in prepared
                )
                band_axis = edge + gap + required

            ordered = sorted(
                prepared,
                key=lambda item: float(item["anchor_point"][1]),
            )
            targets = []
            for index, item in enumerate(ordered):
                desired = float(item["anchor_point"][1])
                if not targets:
                    targets.append(desired)
                    continue
                prev = ordered[index - 1]
                prev_bottom = (
                    targets[-1]
                    + prev["bbox_rel"][3]
                    - prev["pin_rel"][1]
                )
                min_target = (
                    prev_bottom
                    + SCHEMATIC_GRID_MM * 2.0
                    - item["bbox_rel"][1]
                    + item["pin_rel"][1]
                )
                targets.append(max(desired, min_target))
            shift = (
                sum(float(item["anchor_point"][1]) for item in ordered)
                - sum(targets)
            ) / len(targets)
            targets = [value + shift for value in targets]
            target_pins = [
                (band_axis, targets[index])
                for index in range(len(ordered))
            ]

        subject_refs = {item["reference"] for item in prepared}
        anchor_refs = {item["anchor_ref"] for item in prepared}
        static_avoid = [
            _symbol_bbox_bounds(_symbol_by_reference(data, reference))
            for reference in sorted(
                {
                    str(item).strip()
                    for item in (avoid_refs or [])
                    if str(item).strip()
                }
            )
            if reference not in subject_refs
            and reference not in anchor_refs
        ]
        static_avoid.extend(anchor_bounds)
        step_dx, step_dy = _relation_direction(side)
        step = SCHEMATIC_GRID_MM * 2.0
        chosen_positions: list[tuple[float, float]] = []

        for outward_attempt in range(12):
            trial_positions: list[tuple[float, float]] = []
            trial_bounds: list[tuple[float, float, float, float]] = []
            conflict = False
            for item, target_pin in zip(ordered, target_pins, strict=True):
                tx = target_pin[0] + step_dx * step * outward_attempt
                ty = target_pin[1] + step_dy * step * outward_attempt
                pin_rel_x, pin_rel_y = item["pin_rel"]
                x = round((tx - pin_rel_x) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
                y = round((ty - pin_rel_y) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
                bounds = _virtual_symbol_bounds(
                    item["subject"],
                    item["rotation"],
                    x,
                    y,
                )
                if any(
                    _bounds_conflict(bounds, other)
                    for other in static_avoid
                ):
                    conflict = True
                    break
                if any(
                    _bounds_conflict(bounds, other)
                    for other in trial_bounds
                ):
                    conflict = True
                    break
                trial_positions.append((x, y))
                trial_bounds.append(bounds)
            if not conflict:
                chosen_positions = trial_positions
                break

        if not chosen_positions:
            raise ValueError("Could not place relation band without overlap")

        rotations: dict[str, int] = {}
        placed: list[dict[str, Any]] = []
        for item, (x, y) in zip(ordered, chosen_positions, strict=True):
            await call_checked(
                "sch_move_symbol",
                {
                    "reference": item["reference"],
                    "x_mm": x,
                    "y_mm": y,
                    "snap_to_grid": True,
                },
            )
            rotations[item["reference"]] = int(item["rotation"]) % 360
            placed.append(
                {
                    "reference": item["reference"],
                    "anchor": f'{item["anchor_ref"]}.{item["anchor_pin"]}',
                    "rotation": int(item["rotation"]) % 360,
                    "position": [round(x, 4), round(y, 4)],
                    "extend_toward": item["extend_toward"],
                }
            )

        return json.dumps(
            {
                "side": side,
                "distance_steps": distance_steps,
                "band_axis": round(float(band_axis), 4),
                "rotations": rotations,
                "placed": placed,
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_place_pin_relation(
        reference: str,
        pin: str,
        anchor_ref: str,
        anchor_pin: str,
        side: str,
        distance_steps: int = 2,
        far_pin: str | None = None,
        extend_toward: str | None = None,
        anchor_rotation: int | None = None,
        avoid_refs: list[str] | None = None,
        optimize_route: bool = True,
        extension_weight: float = 2.0,
    ) -> str:
        """Solve one pin relation; optionally optimize its projected route.

        ``extension_weight`` is what a far pin pointing off ``extend_toward`` costs per grid step; above 1.5 a part
        that its library draws on its side (a diode) is stood up to extend along the page.
        """
        if distance_steps < 1 or distance_steps > 40:
            raise ValueError("distance_steps must be between 1 and 40")
        if extend_toward is not None:
            _relation_direction(extend_toward)

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        subject = _symbol_by_reference(data, reference)
        anchor = _symbol_by_reference(data, anchor_ref)
        current_rotation = int(
            subject.get("rotation", subject.get("rot", 0)) or 0
        ) % 360
        resolved_anchor_rotation = (
            int(anchor_rotation) % 360
            if anchor_rotation is not None
            else int(anchor.get("rotation", anchor.get("rot", 0)) or 0) % 360
        )
        anchor_pins = _relation_pin_positions(anchor, resolved_anchor_rotation)
        if anchor_pin not in anchor_pins:
            raise ValueError(f"Pin {anchor_ref}.{anchor_pin} was not found")

        resolved_side = (
            _resolved_side_from_pin(
                anchor,
                anchor_pin,
                resolved_anchor_rotation,
            )
            if side == "auto"
            else side
        )
        _relation_direction(resolved_side)
        anchor_point = anchor_pins[anchor_pin]

        avoid = {
            str(item).strip()
            for item in (avoid_refs or [])
            if str(item).strip()
            and str(item).strip() not in {reference, anchor_ref}
        }
        avoid_bounds = [
            _symbol_bbox_bounds(_symbol_by_reference(data, other_ref))
            for other_ref in sorted(avoid)
        ]

        if not optimize_route:
            chosen_rotation = _choose_extension_rotation(
                subject,
                pin,
                far_pin,
                extend_toward,
            )
            placement = _place_outside_anchor_bounds(
                subject,
                pin,
                chosen_rotation,
                anchor,
                anchor_point,
                resolved_side,
                distance_steps,
                avoid_bounds,
            )
            if placement is None:
                raise ValueError(
                    f"Could not place {reference} on the {resolved_side} of "
                    f"{anchor_ref}.{anchor_pin} without overlap"
                )
            target_x, target_y, target_pin = placement
            await call_checked(
                "sch_move_symbol",
                {
                    "reference": reference,
                    "x_mm": target_x,
                    "y_mm": target_y,
                    "snap_to_grid": True,
                },
            )
            return json.dumps(
                {
                    "reference": reference,
                    "pin": pin,
                    "anchor": f"{anchor_ref}.{anchor_pin}",
                    "requested_side": side,
                    "side": resolved_side,
                    "distance_steps": distance_steps,
                    "chosen_rotation": chosen_rotation,
                    "extend_toward": extend_toward,
                    "avoid_refs": sorted(avoid),
                    "optimize_route": False,
                    "symbol_position": [
                        round(target_x, 4),
                        round(target_y, 4),
                    ],
                    "target_pin_position": [
                        round(target_pin[0], 4),
                        round(target_pin[1], 4),
                    ],
                    "projected_route_points": [],
                },
                sort_keys=True,
            )

        rotations = (
            (0, 90, 180, 270)
            if far_pin
            else (current_rotation,)
        )
        outward_dx, outward_dy = _relation_direction(resolved_side)
        transverse = (
            (0.0, 1.0)
            if resolved_side in {"left", "right"}
            else (1.0, 0.0)
        )
        grid = SCHEMATIC_GRID_MM
        transverse_offsets = [0, -2, 2, -4, 4, -6, 6, -8, 8, -10, 10]
        best: tuple[
            float,
            int,
            float,
            float,
            tuple[float, float],
            list[tuple[float, float]],
        ] | None = None

        sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
        sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
        anchor_bounds = _symbol_bbox_bounds(anchor)
        for rotation in rotations:
            pins = _relation_pin_positions(subject, rotation)
            if pin not in pins:
                continue
            px, py = pins[pin]
            pin_rel_x, pin_rel_y = px - sx, py - sy
            base_x, base_y = _relation_base_position(
                subject,
                pin,
                rotation,
                anchor,
                anchor_point,
                resolved_side,
                distance_steps,
            )

            for outward_steps in range(0, 13, 2):
                for transverse_steps in transverse_offsets:
                    x = (
                        base_x
                        + outward_dx * grid * outward_steps
                        + transverse[0] * grid * transverse_steps
                    )
                    y = (
                        base_y
                        + outward_dy * grid * outward_steps
                        + transverse[1] * grid * transverse_steps
                    )
                    x = round(x / grid) * grid
                    y = round(y / grid) * grid
                    bounds = _virtual_symbol_bounds(
                        subject,
                        rotation,
                        x,
                        y,
                    )
                    if any(
                        _bounds_conflict(bounds, other, padding=0.635)
                        for other in (*avoid_bounds, anchor_bounds)
                    ):
                        continue

                    target_pin = (x + pin_rel_x, y + pin_rel_y)
                    route = _orthogonal_route_candidate(
                        anchor_point,
                        target_pin,
                        avoid_bounds,
                    )
                    if route is None:
                        continue
                    route_points, route_score = route
                    score = route_score
                    score += _extension_penalty(
                        subject,
                        pin,
                        far_pin,
                        rotation,
                        extend_toward,
                        extension_weight,
                    )
                    if rotation % 180 != current_rotation % 180:
                        score += grid * QUARTER_TURN_GRIDS
                    score += abs(transverse_steps) * grid * 1.0
                    score += outward_steps * grid * 1.0

                    candidate = (
                        score,
                        int(rotation) % 360,
                        x,
                        y,
                        target_pin,
                        route_points,
                    )
                    if best is None or candidate[0] < best[0]:
                        best = candidate

        if best is None:
            raise ValueError(
                f"Could not solve routing-aware relation for {reference}"
            )

        (
            score,
            chosen_rotation,
            target_x,
            target_y,
            target_pin,
            route_points,
        ) = best
        await call_checked(
            "sch_move_symbol",
            {
                "reference": reference,
                "x_mm": target_x,
                "y_mm": target_y,
                "snap_to_grid": True,
            },
        )
        return json.dumps(
            {
                "reference": reference,
                "pin": pin,
                "anchor": f"{anchor_ref}.{anchor_pin}",
                "requested_side": side,
                "side": resolved_side,
                "distance_steps": distance_steps,
                "chosen_rotation": chosen_rotation,
                "extend_toward": extend_toward,
                "avoid_refs": sorted(avoid),
                "optimize_route": True,
                "score": round(float(score), 4),
                "symbol_position": [
                    round(target_x, 4),
                    round(target_y, 4),
                ],
                "target_pin_position": [
                    round(target_pin[0], 4),
                    round(target_pin[1], 4),
                ],
                "projected_route_points": [
                    [round(x, 4), round(y, 4)]
                    for x, y in route_points
                ],
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_route_relation_connection(
        ref1: str | None = None,
        pin1: str | None = None,
        ref2: str | None = None,
        pin2: str | None = None,
        avoid_refs: list[str] | None = None,
        connections: list[dict[str, Any]] | None = None,
        protect_wires: bool = False,
        obstacle_boxes: bool = False,
        ignore_references: list[str] | None = None,
    ) -> str:
        """Route one or more local relation edges in one libavoid transaction.

        ``ignore_references`` names symbols that are not obstacles (power flags
        that will be moved onto a wire after routing).

        ``protect_wires`` turns on the net-aware ownership checks: a route may
        not touch or overlap an existing conductor or another net's pin, and
        may only cross another net's wire in the interior of both. With
        ``obstacle_boxes`` the existing conductors are also libavoid obstacles,
        so the router avoids them instead of only being rejected for touching.
        """
        import subprocess

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)

        # MCP Pro distinguishes the tight primitive-derived symbol body used by
        # its router from _symbol_bbox_bounds(), which deliberately widens the
        # box to include pin tips. libavoid needs the former.
        from kicad_mcp.tools.schematic import _placed_symbol_bbox
        from backend.schematic.kicad_geometry import measured_scene_from_kicad
        from backend.schematic.presentation_geometry import Bounds, GeometryKind
        from backend.schematic.geometry_constraints import (
            external_text_reservations,
            segment_intersects_reservation,
        )
        from backend.schematic.route_simplification import (
            connects_or_overlaps, leaves_pin_outward, separate_from_foreign, simplify_route, segments_intersect,
            segments_of, repair_diagonals, DiagonalRepairError,
        )
        presentation_scene, presentation_coverage = measured_scene_from_kicad(
            schematic
        )
        from backend.schematic.field_placement import routing_text_obstacles
        presentation_scene = routing_text_obstacles(presentation_scene, bool(layout_clearances["tight_terminals"]))
        # KiCad connects nothing where two wires merely cross, but a touch, a T
        # and a collinear overlap join them. The net-aware mode rejects only
        # those; the historical grouped call keeps rejecting every contact.
        wires_conflict = (
            connects_or_overlaps if protect_wires else segments_intersect
        )

        def body_bounds(
            symbol: dict[str, Any],
        ) -> tuple[float, float, float, float]:
            box = _placed_symbol_bbox(symbol)
            return mirror_box(
                (float(box.x_min), float(box.y_min), float(box.x_max), float(box.y_max)),
                (
                    float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0),
                    float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0),
                ),
                str(symbol.get("mirror", "") or ""),
            )

        requested = list(connections or [])
        if not requested:
            if not all((ref1, pin1, ref2, pin2)):
                raise ValueError(
                    "Provide either connections or ref1/pin1/ref2/pin2"
                )
            requested = [
                {
                    "id": "relation_0",
                    "ref1": str(ref1),
                    "pin1": str(pin1),
                    "ref2": str(ref2),
                    "pin2": str(pin2),
                    "net": "",
                }
            ]

        normalized: list[dict[str, Any]] = []
        endpoint_info: dict[str, dict[str, Any]] = {}
        side_map = {
            "left": "WEST",
            "right": "EAST",
            "above": "NORTH",
            "below": "SOUTH",
        }

        def endpoint(reference: str, pin: str) -> dict[str, Any]:
            token = f"{reference}.{pin}"
            cached = endpoint_info.get(token)
            if cached is not None:
                return cached

            symbol = _symbol_by_reference(data, reference)
            rotation = int(
                symbol.get("rotation", symbol.get("rot", 0)) or 0
            ) % 360
            pins = _relation_pin_positions(symbol, rotation)
            if pin not in pins:
                raise ValueError(f"Pin {token} was not found")
            point = pins[pin]

            side = side_map[
                _resolved_side_from_pin(symbol, pin, rotation)
            ]
            cached = {
                "token": token,
                "reference": reference,
                "pin": pin,
                "point": (float(point[0]), float(point[1])),
                "side": side,
                "bounds": body_bounds(symbol),
            }
            endpoint_info[token] = cached
            return cached

        def point_endpoint(raw_point: Any) -> dict[str, Any]:
            if (
                not isinstance(raw_point, (list, tuple))
                or len(raw_point) != 2
            ):
                raise ValueError(
                    "Point endpoints must be [x_mm, y_mm]"
                )
            point = (
                float(raw_point[0]),
                float(raw_point[1]),
            )
            token = f"@{point[0]:.4f},{point[1]:.4f}"
            cached = endpoint_info.get(token)
            if cached is not None:
                return cached
            cached = {
                "token": token,
                "reference": None,
                "pin": None,
                "point": point,
                "portal": point,
                "side": None,
                "bounds": None,
                "synthetic": True,
            }
            endpoint_info[token] = cached
            return cached

        def resolve_endpoint(
            raw: dict[str, Any],
            suffix: str,
        ) -> dict[str, Any]:
            reference = str(raw.get(f"ref{suffix}", "")).strip()
            pin = str(raw.get(f"pin{suffix}", "")).strip()
            point = raw.get(f"point{suffix}")
            if reference or pin:
                if not reference or not pin:
                    raise ValueError(
                        "Symbol endpoints need both reference and pin"
                    )
                return endpoint(reference, pin)
            if point is not None:
                return point_endpoint(point)
            raise ValueError(
                "Each grouped relation endpoint needs either "
                "ref/pin or point"
            )

        for index, raw in enumerate(requested):
            edge_id = str(raw.get("id", "")).strip() or f"relation_{index}"
            first = resolve_endpoint(raw, "1")
            second = resolve_endpoint(raw, "2")
            normalized.append(
                {
                    "id": edge_id,
                    "source_token": first["token"],
                    "target_token": second["token"],
                    "ref1": first["reference"],
                    "pin1": first["pin"],
                    "ref2": second["reference"],
                    "pin2": second["pin"],
                    "net": str(raw.get("net", "")).strip(),
                }
            )

        avoid = {
            str(item).strip()
            for item in (avoid_refs or [])
            if str(item).strip()
        }

        participants = {
            str(info["reference"])
            for info in endpoint_info.values()
            if info.get("reference")
        }
        obstacle_refs = routing_obstacle_refs(avoid, participants, ignore_references)
        symbol_bounds: dict[
            str,
            tuple[float, float, float, float],
        ] = {}
        children: list[dict[str, Any]] = []

        # Inflate obstacles for routing, but retain the exact KiCad envelope for
        # post-route validation. This gives libavoid a real visual keepout and
        # avoids conflating "near the body" with "through the body".
        obstacle_clearance = SCHEMATIC_GRID_MM * 0.5
        for reference in obstacle_refs:
            symbol = _symbol_by_reference(data, reference)
            bounds = body_bounds(symbol)
            symbol_bounds[reference] = bounds
            left, top, right, bottom = bounds
            children.append(
                {
                    "id": f"obstacle::{reference}",
                    "x": float(left - obstacle_clearance),
                    "y": float(top - obstacle_clearance),
                    "width": max(
                        0.01,
                        float(right - left + 2.0 * obstacle_clearance),
                    ),
                    "height": max(
                        0.01,
                        float(bottom - top + 2.0 * obstacle_clearance),
                    ),
                }
            )

        # Additional geometry comes from the exact same typed presentation
        # scene that CP-SAT sees. The original body rectangles remain for
        # electrical pin portals; only text extending outside those bodies
        # creates supplementary libavoid obstacles.
        presentation_text = external_text_reservations(
            presentation_scene,
            obstacle_refs,
            {owner: Bounds(*box) for owner, box in symbol_bounds.items()},
        )
        children.extend(
            reservation.as_router_node(obstacle_clearance)
            for reservation in presentation_text
        )
        # In the net-aware mode the power symbols already on the sheet are
        # obstacles too, graphic and Value text. The router was never told
        # where the terminals are, so a route could run through a ground
        # symbol's text or its graphic (FINDINGS section 3).
        if protect_wires:
            # A PWR_FLAG is a symbol of the circuit and is already an obstacle
            # with its text above; only the power symbols that terminals added
            # are new.
            power_owners = {
                part.owner for part in presentation_scene.items
                if part.kind in (GeometryKind.POWER_GRAPHIC, GeometryKind.POWER_TEXT)
            } - set(obstacle_refs) - {str(r) for r in ignore_references or ()}
            terminal_text = external_text_reservations(
                presentation_scene, power_owners, {}, include_net_labels=False,
            )
            presentation_text = (*presentation_text, *terminal_text)
            children.extend(
                reservation.as_router_node(obstacle_clearance)
                for reservation in terminal_text
            )
            for part in presentation_scene.items:
                if part.kind != GeometryKind.POWER_GRAPHIC or part.owner not in power_owners:
                    continue
                children.append({
                    "id": f"terminal::{part.id}",
                    "x": float(part.bounds.left - obstacle_clearance),
                    "y": float(part.bounds.top - obstacle_clearance),
                    "width": max(0.01, float(part.bounds.right - part.bounds.left + 2.0 * obstacle_clearance)),
                    "height": max(0.01, float(part.bounds.bottom - part.bounds.top + 2.0 * obstacle_clearance)),
                })

        # A grouped net belongs to a single electrical domain. Existing
        # authored conductors have not yet been attributed to their nets, so
        # regard them all as foreign when the net-aware API requests safety.
        preexisting_conductors = tuple(
            part.segment for part in presentation_scene.items
            if part.kind == GeometryKind.WIRE and part.segment is not None
        )
        if protect_wires and obstacle_boxes:
            wire_clearance = SCHEMATIC_GRID_MM * 0.4
            for index, ((ax, ay), (bx, by)) in enumerate(preexisting_conductors):
                children.append({
                    "id": f"foreign_wire::{index}",
                    "x": min(ax, bx) - wire_clearance,
                    "y": min(ay, by) - wire_clearance,
                    "width": max(0.01, abs(ax-bx) + 2*wire_clearance),
                    "height": max(0.01, abs(ay-by) + 2*wire_clearance),
                })

        # Diagnostic only: allow a full KiCad fixture run to test alternate
        # libavoid penalties without changing validated production defaults.
        # A fresh MCP server process is required for each profile.
        import os
        profile = os.getenv("KICAD_AI_ROUTER_EXPERIMENT", "").strip()
        experiments = {
            "": {},
            "production": {},
            "reduced_crossing": {"crossingPenalty": SCHEMATIC_GRID_MM * 10.0},
            "single_buffer": {"shapeBufferDistance": SCHEMATIC_GRID_MM * 0.5},
            "combined": {
                "crossingPenalty": SCHEMATIC_GRID_MM * 10.0,
                "shapeBufferDistance": SCHEMATIC_GRID_MM * 0.5,
            },
            # Routes pass 1.27 mm from a symbol instead of 1.905, and may turn 1.905 mm after a pin instead of
            # 2.54: a pin tip needs 3.175 mm of free space in front of it rather than 4.445.
            "compact_router": {"shapeBufferDistance": SCHEMATIC_GRID_MM * 0.5},
        }
        if profile not in experiments:
            raise ValueError(f"Unknown KICAD_AI_ROUTER_EXPERIMENT: {profile}")
        portal_clearance = SCHEMATIC_GRID_MM * (1.5 if profile == "compact_router" else 2.0)
        endpoint_size = 0.02
        endpoint_half = endpoint_size / 2.0
        endpoint_nodes: dict[str, tuple[str, str | None]] = {}

        def portal_for(info: dict[str, Any]) -> tuple[float, float]:
            px, py = info["point"]
            left, top, right, bottom = info["bounds"]
            side = info["side"]
            if side == "WEST":
                return (
                    min(px - portal_clearance, left - portal_clearance),
                    py,
                )
            if side == "EAST":
                return (
                    max(px + portal_clearance, right + portal_clearance),
                    py,
                )
            if side == "NORTH":
                return (
                    px,
                    min(py - portal_clearance, top - portal_clearance),
                )
            return (
                px,
                max(py + portal_clearance, bottom + portal_clearance),
            )

        for token, info in sorted(endpoint_info.items()):
            if info.get("synthetic"):
                portal = info["point"]
            else:
                portal = portal_for(info)
                info["portal"] = portal
            x, y = portal
            node_id = f"portal::{token}"
            children.append(
                {
                    "id": node_id,
                    "x": float(x - endpoint_half),
                    "y": float(y - endpoint_half),
                    "width": endpoint_size,
                    "height": endpoint_size,
                }
            )
            endpoint_nodes[token] = (node_id, None)

        graph_edges: list[dict[str, Any]] = []
        for item in normalized:
            source_token = item["source_token"]
            target_token = item["target_token"]
            source_node, source_port = endpoint_nodes[source_token]
            target_node, target_port = endpoint_nodes[target_token]
            edge: dict[str, Any] = {
                "id": item["id"],
                "source": source_node,
                "target": target_node,
            }
            if source_port is not None:
                edge["sourcePort"] = source_port
            if target_port is not None:
                edge["targetPort"] = target_port
            graph_edges.append(edge)

        graph = {
            "id": "root",
            "children": children,
            "edges": graph_edges,
        }
        payload = {
            "graph": graph,
            "options": {
                "routingType": "orthogonal",
                "shapeBufferDistance": SCHEMATIC_GRID_MM,
                "idealNudgingDistance": SCHEMATIC_GRID_MM,
                "segmentPenalty": SCHEMATIC_GRID_MM * 10.0,
                "crossingPenalty": SCHEMATIC_GRID_MM * 80.0,
                "portDirectionPenalty": SCHEMATIC_GRID_MM * 40.0,
                "reverseDirectionPenalty": SCHEMATIC_GRID_MM * 12.0,
                "fixedSharedPathPenalty": SCHEMATIC_GRID_MM * 25.0,
                "nudgeOrthogonalSegmentsConnectedToShapes": True,
                "nudgeOrthogonalTouchingColinearSegments": True,
                "performUnifyingNudgingPreprocessingStep": True,
                # Explicit schematic junctions are intentionally allowed
                # to have multiple orthogonal segments meet at one point. Do
                # not nudge those common-endpoint paths apart; that creates
                # cosmetic doglegs in otherwise straight host-to-junction
                # connections.
                "nudgeSharedPathsWithCommonEndPoint": False,
            },
        }

        payload["options"].update(experiments[profile])

        # The problem is handed to libavoid with its nearest grid corner as the origin: the same layout is then routed alike
        # wherever it stands (the same opamp, moved 178 mm, came out with another jog, `auto-tr-b`); see router_origin.
        from backend.schematic.route_simplification import router_origin

        origin_x, origin_y = router_origin(children, SCHEMATIC_GRID_MM)
        for child in children:
            child["x"] = round(child["x"] - origin_x, 6)
            child["y"] = round(child["y"] - origin_y, 6)

        completed = subprocess.run(
            ["node", "scripts/libavoid_route.mjs"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(
                "grouped libavoid routing failed: "
                + completed.stderr.strip()
            )
        raw_routes = json.loads(completed.stdout)

        grid = SCHEMATIC_GRID_MM

        def snap_point(raw: dict[str, Any]) -> tuple[float, float]:
            return (
                round(float(raw["x"]) / grid) * grid + origin_x,
                round(float(raw["y"]) / grid) * grid + origin_y,
            )

        def compact_points(
            points: list[tuple[float, float]],
        ) -> list[tuple[float, float]]:
            compact: list[tuple[float, float]] = []
            for point in points:
                if compact and (
                    abs(compact[-1][0] - point[0]) < 1e-9
                    and abs(compact[-1][1] - point[1]) < 1e-9
                ):
                    continue
                compact.append(point)

            changed = True
            while changed and len(compact) >= 3:
                changed = False
                reduced = [compact[0]]
                for index in range(1, len(compact) - 1):
                    prior = reduced[-1]
                    current = compact[index]
                    following = compact[index + 1]
                    if (
                        abs(prior[0] - current[0]) < 1e-9
                        and abs(current[0] - following[0]) < 1e-9
                    ) or (
                        abs(prior[1] - current[1]) < 1e-9
                        and abs(current[1] - following[1]) < 1e-9
                    ):
                        changed = True
                        continue
                    reduced.append(current)
                reduced.append(compact[-1])
                compact = reduced
            return compact

        repair_counts = {"diagonals_repaired": 0, "branches_rerouted": 0}

        def reroute_branch(branch: dict[str, Any]) -> dict[str, Any] | None:
            """Route one branch alone: symbol bodies get one more grid of margin and the other nets' branches
            accepted so far are obstacles."""
            import copy
            retry = copy.deepcopy(payload)
            widen = SCHEMATIC_GRID_MM
            nodes = retry["graph"]["children"]
            widen_obstacle_nodes(nodes, widen)
            clearance = SCHEMATIC_GRID_MM * 0.4
            for index, ((ax, ay), (bx, by)) in enumerate(added_segments):
                if added_segments[((ax, ay), (bx, by))] == branch["net"]:
                    continue
                nodes.append({
                    "id": f"accepted_wire::{index}",
                    "x": round(min(ax, bx) - clearance - origin_x, 6),
                    "y": round(min(ay, by) - clearance - origin_y, 6),
                    "width": max(0.01, abs(ax - bx) + 2 * clearance),
                    "height": max(0.01, abs(ay - by) + 2 * clearance),
                })
            retry["graph"]["edges"] = [
                edge for edge in retry["graph"]["edges"] if edge["id"] == branch["id"]
            ]
            again = subprocess.run(
                ["node", "scripts/libavoid_route.mjs"],
                input=json.dumps(retry), text=True, capture_output=True, check=False,
            )
            if again.returncode != 0:
                return None
            return json.loads(again.stdout).get(branch["id"])

        # Net ownership applies to all terminals in the batch, not just the
        # two endpoints of whichever branch is currently being inspected.
        net_owned_pins: dict[str, set[str]] = {}
        for candidate in normalized:
            owned = net_owned_pins.setdefault(candidate["net"], set())
            for side in ("1", "2"):
                ref, pin = candidate.get(f"ref{side}"), candidate.get(f"pin{side}")
                if ref and pin:
                    owned.add(f"{ref}.{pin}:terminal")

        routed: list[dict[str, Any]] = []
        presentation_warnings: list[dict[str, Any]] = []
        added_segments: dict[
            tuple[tuple[float, float], tuple[float, float]],
            str,
        ] = {}

        work = list(normalized)
        rerouted_ids: set[str] = set()
        original_failures: dict[str, ValueError] = {}
        while work:
            item = work.pop(0)
            raw_route = raw_routes.get(item["id"])
            if raw_route is None:
                raise ValueError(
                    f"libavoid returned no grouped route for {item['id']}"
                )

            source_token = item["source_token"]
            target_token = item["target_token"]
            source_info = endpoint_info[source_token]
            target_info = endpoint_info[target_token]
            pin_start = source_info["point"]
            pin_end = target_info["point"]
            start = source_info["portal"]
            end = target_info["portal"]
            # Explicit schematic junctions get a stronger convention than
            # generic connector routing: when a pin and junction are already
            # axis-aligned and the direct segment is clear, keep the visible
            # wire straight. This avoids router-created cosmetic doglegs while
            # still falling back to libavoid for obstructed/non-aligned legs.
            source_is_point = bool(source_info.get("synthetic"))
            target_is_point = bool(target_info.get("synthetic"))
            direct_junction_route = False
            direct_start = (
                float(pin_start[0]),
                float(pin_start[1]),
            )
            direct_end = (
                float(pin_end[0]),
                float(pin_end[1]),
            )
            if source_is_point != target_is_point:
                axis_aligned = (
                    abs(direct_start[0] - direct_end[0]) < 1e-6
                    or abs(direct_start[1] - direct_end[1]) < 1e-6
                )
                if axis_aligned:
                    blocked = False
                    for reference, bounds in symbol_bounds.items():
                        if reference in {
                            item.get("ref1"),
                            item.get("ref2"),
                        }:
                            continue
                        if _segment_hits_bounds(
                            direct_start,
                            direct_end,
                            bounds,
                            padding=0.0,
                        ):
                            blocked = True
                            break
                    if not blocked:
                        blocked = any(
                            segment_intersects_reservation(
                                direct_start,
                                direct_end,
                                reservation,
                            )
                            for reservation in presentation_text
                            if reservation.owner not in {
                                item.get("ref1"), item.get("ref2"),
                            }
                        )
                    direct_junction_route = not blocked

            if direct_junction_route:
                points = compact_points([
                    direct_start,
                    direct_end,
                ])
            else:
                raw_points = [
                    raw_route.get(
                        "sourcePoint",
                        {"x": start[0] - origin_x, "y": start[1] - origin_y},
                    ),
                    *raw_route.get("bendPoints", []),
                    raw_route.get(
                        "targetPoint",
                        {"x": end[0] - origin_x, "y": end[1] - origin_y},
                    ),
                ]
                points = [snap_point(raw) for raw in raw_points]
                if not points:
                    raise ValueError(
                        f"libavoid returned an empty grouped route for {item['id']}"
                    )
                points[0] = (float(start[0]), float(start[1]))
                points[-1] = (float(end[0]), float(end[1]))
                points = compact_points(points)

                # libavoid owns free-space routing between portals. Exact pin
                # attachment is represented by fixed, straight escape stubs.
                points = compact_points([
                    direct_start,
                    *points,
                    direct_end,
                ])

            warnings_before = len(presentation_warnings)
            try:
                original_points = tuple(points)
                # Only accept a simpler libavoid route if *every* proposed
                # segment clears other pins, symbol bodies, external fields and
                # already-authored conductors. No guesswork about unknown nets.
                original_wires = tuple(
                    part.segment
                    for part in presentation_scene.items
                    if part.kind == GeometryKind.WIRE and part.segment is not None
                )
                source_terminal = (
                    f"{item['ref1']}.{item['pin1']}:terminal"
                    if item.get("ref1") and item.get("pin1") else None
                )
                target_terminal = (
                    f"{item['ref2']}.{item['pin2']}:terminal"
                    if item.get("ref2") and item.get("pin2") else None
                )
                # Own-symbol *other pins* are just as electrically dangerous as
                # pins belonging to a foreign symbol. Exempt only actual endpoints.
                foreign_pins = tuple(
                    (part.bounds.left, part.bounds.top)
                    for part in presentation_scene.items
                    if part.kind == GeometryKind.PIN_ENDPOINT
                    and part.id not in {source_terminal, target_terminal}
                )

                side_vector = {"WEST": (-1.0, 0.0), "EAST": (1.0, 0.0), "NORTH": (0.0, -1.0), "SOUTH": (0.0, 1.0)}

                def escapes(token: str, tip: tuple[float, float], other: tuple[float, float]) -> bool:
                    """The segment at a pin may touch its own symbol only if it leaves the way the pin points. The net-aware
                    mode requires that (a shortcut straight back across a connector's body is not an escape); the
                    historical call keeps its historical rule."""
                    side = (endpoint_info.get(token) or {}).get("side")
                    if not protect_wires or side not in side_vector:
                        return True
                    return leaves_pin_outward(tip, other, side_vector[side])

                def shortcut_is_safe(candidate: tuple[tuple[float,float], ...]) -> bool:
                    candidate_segments = segments_of(candidate)
                    for segment_index, (a, b) in enumerate(candidate_segments):
                        for reference, bounds in symbol_bounds.items():
                            own_escape = (
                                (segment_index == 0 and reference == item.get("ref1")
                                 and escapes(item["source_token"], a, b))
                                or (segment_index == len(candidate_segments)-1
                                    and reference == item.get("ref2")
                                    and escapes(item["target_token"], b, a))
                            )
                            if not own_escape and _segment_hits_bounds(
                                a, b, bounds, padding=0.0,
                            ):
                                return False
                        if any(
                            segment_intersects_reservation(a, b, reservation)
                            for reservation in presentation_text
                            if not (
                                (segment_index == 0 and reservation.owner == item.get("ref1"))
                                or (segment_index == len(candidate_segments)-1
                                    and reservation.owner == item.get("ref2"))
                            )
                        ):
                            return False
                        if any(
                            (abs(a[0]-b[0])<1e-5 and abs(a[0]-x)<1e-5
                             and min(a[1],b[1])-1e-5<=y<=max(a[1],b[1])+1e-5)
                            or (abs(a[1]-b[1])<1e-5 and abs(a[1]-y)<1e-5
                             and min(a[0],b[0])-1e-5<=x<=max(a[0],b[0])+1e-5)
                            for x,y in foreign_pins
                        ):
                            return False
                        if any(
                            wires_conflict((a,b),wire) for wire in original_wires
                        ):
                            return False
                        if any(
                            other_net != item["net"] and wires_conflict((a,b),known)
                            for known, other_net in added_segments.items()
                        ):
                            return False
                    return True

                def _tidy(pts):
                    if not direct_junction_route and len(pts) > 3:
                        pts = list(simplify_route(
                            pts, route_is_safe=shortcut_is_safe,
                            grid_mm=SCHEMATIC_GRID_MM,
                        ))
                    # Grid snapping can put this route on the line of another net's conductor drawn before it; KiCad would merge them.
                    if len(pts) > 3:
                        pts = list(separate_from_foreign(
                            pts,
                            touches_foreign=lambda segment: any(
                                other_net != item["net"] and connects_or_overlaps(segment, known)
                                for known, other_net in added_segments.items()
                            ),
                            route_is_safe=shortcut_is_safe,
                            grid_mm=SCHEMATIC_GRID_MM,
                        ))
                    return pts

                points = _tidy(points)
                if not direct_junction_route and any(
                    abs(p0[0] - p1[0]) >= 1e-6 and abs(p0[1] - p1[1]) >= 1e-6
                    for p0, p1 in zip(points, points[1:], strict=False)
                ):
                    # Last resort: only a diagonal that simplification could not remove is repaired.
                    try:
                        repaired_points, repaired = repair_diagonals(
                            points, route_is_safe=shortcut_is_safe, grid_mm=SCHEMATIC_GRID_MM,
                        )
                    except DiagonalRepairError as unsafe:
                        raise ValueError(
                            "libavoid emitted a non-orthogonal grouped segment "
                            f"for {item['id']}: {unsafe.segment[0]}->{unsafe.segment[1]}"
                        ) from None
                    repair_counts["diagonals_repaired"] += repaired
                    points = _tidy(list(repaired_points))

                for first_point, second_point in zip(
                    points,
                    points[1:],
                    strict=False,
                ):
                    if not (
                        abs(first_point[0] - second_point[0]) < 1e-6
                        or abs(first_point[1] - second_point[1]) < 1e-6
                    ):
                        raise ValueError(
                            "libavoid emitted a non-orthogonal grouped segment "
                            f"for {item['id']}: {first_point}->{second_point}"
                        )

                # Hard postcondition: free-space portions may not cross any
                # component body. Only the first and last escape stubs may intersect
                # their own endpoint symbols.
                segments = list(zip(points, points[1:], strict=False))
                for segment_index, (first_point, second_point) in enumerate(segments):
                    for reference, bounds in symbol_bounds.items():
                        own_escape = (
                            segment_index == 0
                            and item.get("ref1") is not None
                            and reference == item["ref1"]
                            and escapes(item["source_token"], first_point, second_point)
                        ) or (
                            segment_index == len(segments) - 1
                            and item.get("ref2") is not None
                            and reference == item["ref2"]
                            and escapes(item["target_token"], second_point, first_point)
                        )
                        if own_escape:
                            continue
                        if _segment_hits_bounds(
                            first_point,
                            second_point,
                            bounds,
                            padding=0.0,
                        ):
                            raise ValueError(
                                "libavoid route intersects symbol body: "
                                f"{item['id']} crosses {reference} "
                                f"on {first_point}->{second_point}"
                            )

                if protect_wires:
                    for a, b in segments:
                        if any(
                            wires_conflict((a, b), wire)
                            for wire in preexisting_conductors
                        ):
                            raise ValueError(
                                "Net-aware route intersects pre-existing conductor "
                                f"of unknown net ownership: {item['id']} {a}->{b}"
                            )
                        # All branches in this routing transaction belong to
                        # the same named electrical net; allow touching another
                        # one of its terminals, while forbidding unrelated pins.
                        intended = net_owned_pins[item["net"]]
                        for pin_obj in presentation_scene.items:
                            if (
                                pin_obj.kind != GeometryKind.PIN_ENDPOINT
                                or pin_obj.id in intended
                            ):
                                continue
                            x, y = pin_obj.bounds.left, pin_obj.bounds.top
                            on_segment = (
                                (abs(a[0]-b[0])<1e-6
                                 and abs(a[0]-x)<1e-6
                                 and min(a[1],b[1])-1e-6<=y<=max(a[1],b[1])+1e-6)
                                or (abs(a[1]-b[1])<1e-6
                                 and abs(a[1]-y)<1e-6
                                 and min(a[0],b[0])-1e-6<=x<=max(a[0],b[0])+1e-6)
                            )
                            if on_segment:
                                raise ValueError(
                                    "Net-aware route intersects foreign pin "
                                    f"{pin_obj.id} on {item['id']}"
                                )

                # Text fields are first-class routing obstacles. A conductor may
                # touch the pin annotation at its OWN attachment, but not another
                # symbol's text or its own separate reference/value text.
                for segment_index, (first_point, second_point) in enumerate(segments):
                    for reservation in presentation_text:
                        own_endpoint = (
                            (segment_index == 0 and reservation.owner == item.get("ref1"))
                            or (
                                segment_index == len(segments) - 1
                                and reservation.owner == item.get("ref2")
                            )
                        )
                        if own_endpoint and reservation.kind in {
                            GeometryKind.PIN_NAME, GeometryKind.PIN_NUMBER,
                        }:
                            continue
                        if segment_intersects_reservation(
                            first_point, second_point, reservation,
                        ):
                            violation = {
                                "edge": item["id"],
                                "reservation": reservation.identity,
                                "kind": reservation.kind.value,
                                "certainty": reservation.certainty,
                                "owner": reservation.owner,
                                "own_endpoint_escape": bool(own_endpoint),
                                "segment": [list(first_point), list(second_point)],
                            }
                            if reservation.certainty == "estimated":
                                # Until glyph metrics are authoritative, prefer
                                # avoidance, but do not reject a valid netlist on
                                # an approximate text rectangle. Surface the
                                # conflict for the subsequent visual QA pass.
                                presentation_warnings.append(violation)
                                continue
                            raise ValueError(
                                "libavoid route intersects measured presentation "
                                f"reservation: {violation}"
                            )

            except ValueError as failure:
                message = str(failure)
                if item["id"] in rerouted_ids:
                    _log.warning("route recovery: %s failed again after reroute: %s", item["id"], message)
                    raise failure_with_repair_counts(
                        original_failures[item["id"]], repair_counts["branches_rerouted"],
                        repair_counts["diagonals_repaired"],
                    ) from None
                if not should_reroute_failure(message):
                    raise
                _log.warning("route recovery: rerouting %s after: %s", item["id"], message)
                del presentation_warnings[warnings_before:]
                rerouted_ids.add(item["id"])
                original_failures[item["id"]] = failure
                fresh = reroute_branch(item)
                if fresh is None:
                    _log.warning("route recovery: reroute of %s produced no route", item["id"])
                    raise failure_with_repair_counts(
                        failure, repair_counts["branches_rerouted"], repair_counts["diagonals_repaired"],
                    ) from None
                raw_routes[item["id"]] = fresh
                _log.info("route recovery: %s rerouted, revalidating", item["id"])
                repair_counts["branches_rerouted"] += 1
                work.insert(0, item)
                continue

            added: list[dict[str, Any]] = []
            for first_point, second_point in zip(
                points,
                points[1:],
                strict=False,
            ):
                canonical = tuple(
                    sorted(
                        (
                            (
                                round(first_point[0], 6),
                                round(first_point[1], 6),
                            ),
                            (
                                round(second_point[0], 6),
                                round(second_point[1], 6),
                            ),
                        )
                    )
                )
                prior_net = added_segments.get(canonical)
                if prior_net is not None:
                    if prior_net != item["net"]:
                        raise ValueError(
                            "libavoid overlapped routes from distinct nets: "
                            f"{prior_net!r} and {item['net']!r}"
                        )
                    continue

                if protect_wires and any(
                    different_net != item["net"]
                    and wires_conflict((first_point, second_point), prior)
                    for prior, different_net in added_segments.items()
                ):
                    raise ValueError(
                        "Net-aware grouped route intersects foreign net "
                        f"at {item['id']}: {first_point}->{second_point}"
                    )

                result = await call_checked(
                    "sch_add_wire",
                    {
                        "x1_mm": first_point[0],
                        "y1_mm": first_point[1],
                        "x2_mm": second_point[0],
                        "y2_mm": second_point[1],
                        "snap_to_grid": True,
                    },
                )
                added_segments[canonical] = item["net"]
                added.append(
                    {
                        "from": [
                            round(first_point[0], 4),
                            round(first_point[1], 4),
                        ],
                        "to": [
                            round(second_point[0], 4),
                            round(second_point[1], 4),
                        ],
                        "result": str(result),
                    }
                )

            length = sum(
                abs(second_point[0] - first_point[0])
                + abs(second_point[1] - first_point[1])
                for first_point, second_point in zip(
                    points,
                    points[1:],
                    strict=False,
                )
            )
            routed.append(
                {
                    "id": item["id"],
                    "net": item["net"],
                    "from": source_token,
                    "to": target_token,
                    "points": [
                        [round(x, 4), round(y, 4)]
                        for x, y in points
                    ],
                    "segments": max(0, len(points) - 1),
                    "bends": max(0, len(points) - 2),
                    "bends_before_shortcut": max(0, len(original_points) - 2),
                    "shortcuts_applied": len(points) < len(original_points),
                    "length_mm": round(float(length), 4),
                    "source_side": raw_route.get("sourceSide"),
                    "target_side": raw_route.get("targetSide"),
                    "strategy": (
                        "direct_junction"
                        if direct_junction_route
                        else "libavoid"
                    ),
                    "added": added,
                }
            )

        return json.dumps(
            {
                "router": "libavoid_group_portals",
                "wire_ownership_policy": (
                    "treat_existing_as_foreign" if protect_wires
                    else "legacy_unattributed"
                ),
                "existing_wires_are_obstacles": bool(
                    protect_wires and obstacle_boxes
                ),
                "connection_count": len(routed),
                "routes": routed,
                "route_repairs": dict(repair_counts),
                "total_segments": sum(item["segments"] for item in routed),
                "total_bends": sum(item["bends"] for item in routed),
                "total_length_mm": round(
                    sum(item["length_mm"] for item in routed),
                    4,
                ),
                "avoid_refs": sorted(avoid),
                "obstacle_clearance_mm": round(
                    float(obstacle_clearance),
                    4,
                ),
                "obstacle_model": "typed_scene_body_and_text",
                "presentation_constraints": {
                    "text_reservations": len(presentation_text),
                    "body_reservations": len(symbol_bounds),
                    "estimated_text_crossings": presentation_warnings,
                    "estimated_text_crossing_count": len(presentation_warnings),
                    "coverage": presentation_coverage,
                },
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_place_series_relation(
        reference: str,
        pin_a: str,
        pin_b: str,
        anchor_a_ref: str,
        anchor_a_pin: str,
        anchor_b_ref: str,
        anchor_b_pin: str,
        side: str,
        distance_steps: int = 2,
        anchor_a_rotation: int | None = None,
        anchor_b_rotation: int | None = None,
    ) -> str:
        """Place a series element in an envelope-aware lane beside its anchors."""
        if distance_steps < 1 or distance_steps > 40:
            raise ValueError("distance_steps must be between 1 and 40")
        _relation_direction(side)
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        subject = _symbol_by_reference(data, reference)
        anchor_a = _symbol_by_reference(data, anchor_a_ref)
        anchor_b = _symbol_by_reference(data, anchor_b_ref)

        def resolved_rotation(
            symbol: dict[str, Any],
            override: int | None,
        ) -> int:
            if override is not None:
                return int(override) % 360
            return int(
                symbol.get("rotation", symbol.get("rot", 0)) or 0
            ) % 360

        a_rotation = resolved_rotation(anchor_a, anchor_a_rotation)
        b_rotation = resolved_rotation(anchor_b, anchor_b_rotation)
        a_pins = _relation_pin_positions(anchor_a, a_rotation)
        b_pins = _relation_pin_positions(anchor_b, b_rotation)
        if anchor_a_pin not in a_pins:
            raise ValueError(f"Pin {anchor_a_ref}.{anchor_a_pin} was not found")
        if anchor_b_pin not in b_pins:
            raise ValueError(f"Pin {anchor_b_ref}.{anchor_b_pin} was not found")
        aa = a_pins[anchor_a_pin]
        bb = b_pins[anchor_b_pin]
        anchor_bounds_a = _symbol_bbox_bounds(anchor_a)
        anchor_bounds_b = _symbol_bbox_bounds(anchor_b)
        gap = SCHEMATIC_GRID_MM * distance_steps

        sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
        sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
        current_rotation = int(
            subject.get("rotation", subject.get("rot", 0)) or 0
        ) % 360
        best: tuple[float, float, int, float, float] | None = None
        for rotation in (0, 90, 180, 270):
            pins = _relation_pin_positions(subject, rotation)
            if pin_a not in pins or pin_b not in pins:
                continue
            pa = pins[pin_a]
            pb = pins[pin_b]
            pa_rel = (pa[0] - sx, pa[1] - sy)
            pb_rel = (pb[0] - sx, pb[1] - sy)
            bbox = _virtual_symbol_bounds(subject, rotation, sx, sy)
            bbox_rel = (
                bbox[0] - sx,
                bbox[1] - sy,
                bbox[2] - sx,
                bbox[3] - sy,
            )

            if side == "left":
                x = min(anchor_bounds_a[0], anchor_bounds_b[0]) - gap - bbox_rel[2]
                y = (
                    (aa[1] - pa_rel[1])
                    + (bb[1] - pb_rel[1])
                ) / 2.0
                error = (
                    (y + pa_rel[1] - aa[1]) ** 2
                    + (y + pb_rel[1] - bb[1]) ** 2
                )
            elif side == "right":
                x = max(anchor_bounds_a[2], anchor_bounds_b[2]) + gap - bbox_rel[0]
                y = (
                    (aa[1] - pa_rel[1])
                    + (bb[1] - pb_rel[1])
                ) / 2.0
                error = (
                    (y + pa_rel[1] - aa[1]) ** 2
                    + (y + pb_rel[1] - bb[1]) ** 2
                )
            elif side == "above":
                y = min(anchor_bounds_a[1], anchor_bounds_b[1]) - gap - bbox_rel[3]
                x = (
                    (aa[0] - pa_rel[0])
                    + (bb[0] - pb_rel[0])
                ) / 2.0
                error = (
                    (x + pa_rel[0] - aa[0]) ** 2
                    + (x + pb_rel[0] - bb[0]) ** 2
                )
            else:
                y = max(anchor_bounds_a[3], anchor_bounds_b[3]) + gap - bbox_rel[1]
                x = (
                    (aa[0] - pa_rel[0])
                    + (bb[0] - pb_rel[0])
                ) / 2.0
                error = (
                    (x + pa_rel[0] - aa[0]) ** 2
                    + (x + pb_rel[0] - bb[0]) ** 2
                )

            turn_distance = min(
                (rotation - current_rotation) % 360,
                (current_rotation - rotation) % 360,
            )
            candidate = (
                -error,
                -float(turn_distance) / 1000.0,
                rotation,
                x,
                y,
            )
            if best is None or candidate > best:
                best = candidate

        if best is None:
            raise ValueError(f"Could not orient series element {reference}")

        chosen_rotation = int(best[2]) % 360
        target_x = round(float(best[3]) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
        target_y = round(float(best[4]) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
        await call_checked(
            "sch_move_symbol",
            {
                "reference": reference,
                "x_mm": target_x,
                "y_mm": target_y,
                "snap_to_grid": True,
            },
        )

        final_pins = _relation_pin_positions(
            {
                **subject,
                "x": target_x,
                "y": target_y,
                "x_mm": target_x,
                "y_mm": target_y,
            },
            chosen_rotation,
        )
        return json.dumps(
            {
                "reference": reference,
                "pins": [pin_a, pin_b],
                "anchors": [
                    f"{anchor_a_ref}.{anchor_a_pin}",
                    f"{anchor_b_ref}.{anchor_b_pin}",
                ],
                "side": side,
                "distance_steps": distance_steps,
                "chosen_rotation": chosen_rotation,
                "symbol_position": [
                    round(target_x, 4),
                    round(target_y, 4),
                ],
                "pin_positions": [
                    [
                        round(float(final_pins[pin_a][0]), 4),
                        round(float(final_pins[pin_a][1]), 4),
                    ],
                    [
                        round(float(final_pins[pin_b][0]), 4),
                        round(float(final_pins[pin_b][1]), 4),
                    ],
                ],
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_place_parallel_bank(
        entries: list[dict[str, Any]],
        side: str,
        distance_steps: int = 2,
        far_direction: str = "below",
        avoid_refs: list[str] | None = None,
    ) -> str:
        """Place a bank of two-pin parts with one aligned near-pin row/column."""
        if not entries:
            raise ValueError("parallel bank entries must be non-empty")
        if distance_steps < 1 or distance_steps > 40:
            raise ValueError("distance_steps must be between 1 and 40")
        _relation_direction(side)
        _relation_direction(far_direction)

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        avoid = {
            str(item).strip()
            for item in (avoid_refs or [])
            if str(item).strip()
        }

        prepared: list[dict[str, Any]] = []
        anchor_bounds: list[tuple[float, float, float, float]] = []
        for entry in entries:
            reference = str(entry["reference"])
            near_pin = str(entry["near_pin"])
            far_pin = str(entry["far_pin"])
            anchor_ref = str(entry["anchor_ref"])
            anchor_pin = str(entry["anchor_pin"])
            subject = _symbol_by_reference(data, reference)
            anchor = _symbol_by_reference(data, anchor_ref)
            rotation = _choose_extension_rotation(
                subject,
                near_pin,
                far_pin,
                far_direction,
            )
            requested_anchor_rotation = entry.get("anchor_rotation")
            anchor_rotation = (
                int(requested_anchor_rotation) % 360
                if requested_anchor_rotation is not None
                else int(
                    anchor.get("rotation", anchor.get("rot", 0)) or 0
                ) % 360
            )
            anchor_pins = _relation_pin_positions(anchor, anchor_rotation)
            if anchor_pin not in anchor_pins:
                raise ValueError(f"Pin {anchor_ref}.{anchor_pin} was not found")
            sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
            sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
            subject_pins = _relation_pin_positions(subject, rotation)
            if near_pin not in subject_pins:
                raise ValueError(f"Pin {reference}.{near_pin} was not found")
            px, py = subject_pins[near_pin]
            bbox = _virtual_symbol_bounds(subject, rotation, sx, sy)
            prepared.append(
                {
                    "reference": reference,
                    "near_pin": near_pin,
                    "far_pin": far_pin,
                    "anchor_ref": anchor_ref,
                    "anchor_pin": anchor_pin,
                    "subject": subject,
                    "rotation": rotation,
                    "pin_rel": (px - sx, py - sy),
                    "bbox_rel": (
                        bbox[0] - sx,
                        bbox[1] - sy,
                        bbox[2] - sx,
                        bbox[3] - sy,
                    ),
                    "anchor_point": anchor_pins[anchor_pin],
                }
            )
            anchor_bounds.append(_symbol_bbox_bounds(anchor))

        gap = SCHEMATIC_GRID_MM * max(1, distance_steps)
        if side in {"above", "below"}:
            if side == "above":
                edge = min(bounds[1] for bounds in anchor_bounds)
                required = max(
                    item["bbox_rel"][3] - item["pin_rel"][1]
                    for item in prepared
                )
                bank_axis = edge - gap - required
            else:
                edge = max(bounds[3] for bounds in anchor_bounds)
                required = max(
                    item["pin_rel"][1] - item["bbox_rel"][1]
                    for item in prepared
                )
                bank_axis = edge + gap + required

            ordered = sorted(
                prepared,
                key=lambda item: float(item["anchor_point"][0]),
            )
            targets: list[float] = []
            for index, item in enumerate(ordered):
                desired = float(item["anchor_point"][0])
                if not targets:
                    targets.append(desired)
                    continue
                prev = ordered[index - 1]
                prev_right = (
                    targets[-1]
                    + prev["bbox_rel"][2]
                    - prev["pin_rel"][0]
                )
                min_target = (
                    prev_right
                    + SCHEMATIC_GRID_MM * 2.0
                    - item["bbox_rel"][0]
                    + item["pin_rel"][0]
                )
                targets.append(max(desired, min_target))
            shift = (
                sum(float(item["anchor_point"][0]) for item in ordered)
                - sum(targets)
            ) / len(targets)
            targets = [value + shift for value in targets]
            target_pins = [
                (targets[index], bank_axis)
                for index in range(len(ordered))
            ]
        else:
            if side == "left":
                edge = min(bounds[0] for bounds in anchor_bounds)
                required = max(
                    item["bbox_rel"][2] - item["pin_rel"][0]
                    for item in prepared
                )
                bank_axis = edge - gap - required
            else:
                edge = max(bounds[2] for bounds in anchor_bounds)
                required = max(
                    item["pin_rel"][0] - item["bbox_rel"][0]
                    for item in prepared
                )
                bank_axis = edge + gap + required

            ordered = sorted(
                prepared,
                key=lambda item: float(item["anchor_point"][1]),
            )
            targets = []
            for index, item in enumerate(ordered):
                desired = float(item["anchor_point"][1])
                if not targets:
                    targets.append(desired)
                    continue
                prev = ordered[index - 1]
                prev_bottom = (
                    targets[-1]
                    + prev["bbox_rel"][3]
                    - prev["pin_rel"][1]
                )
                min_target = (
                    prev_bottom
                    + SCHEMATIC_GRID_MM * 2.0
                    - item["bbox_rel"][1]
                    + item["pin_rel"][1]
                )
                targets.append(max(desired, min_target))
            shift = (
                sum(float(item["anchor_point"][1]) for item in ordered)
                - sum(targets)
            ) / len(targets)
            targets = [value + shift for value in targets]
            target_pins = [
                (bank_axis, targets[index])
                for index in range(len(ordered))
            ]

        static_avoid = [
            _symbol_bbox_bounds(_symbol_by_reference(data, ref))
            for ref in sorted(avoid)
            if ref not in {item["reference"] for item in prepared}
            and ref not in {item["anchor_ref"] for item in prepared}
        ]
        step_dx, step_dy = _relation_direction(side)
        step = SCHEMATIC_GRID_MM * 2.0
        chosen_positions: list[tuple[float, float]] = []
        for outward_attempt in range(10):
            trial_positions: list[tuple[float, float]] = []
            trial_bounds: list[tuple[float, float, float, float]] = []
            conflict = False
            for item, target_pin in zip(ordered, target_pins, strict=True):
                tx = target_pin[0] + step_dx * step * outward_attempt
                ty = target_pin[1] + step_dy * step * outward_attempt
                pin_rel_x, pin_rel_y = item["pin_rel"]
                x = round((tx - pin_rel_x) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
                y = round((ty - pin_rel_y) / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
                bounds = _virtual_symbol_bounds(
                    item["subject"],
                    item["rotation"],
                    x,
                    y,
                )
                if any(_bounds_conflict(bounds, other) for other in static_avoid):
                    conflict = True
                    break
                if any(_bounds_conflict(bounds, other) for other in trial_bounds):
                    conflict = True
                    break
                trial_positions.append((x, y))
                trial_bounds.append(bounds)
            if not conflict:
                chosen_positions = trial_positions
                break
        if not chosen_positions:
            raise ValueError("Could not place parallel bank without overlap")

        rotations: dict[str, int] = {}
        placed: list[dict[str, Any]] = []
        for item, (x, y) in zip(ordered, chosen_positions, strict=True):
            await call_checked(
                "sch_move_symbol",
                {
                    "reference": item["reference"],
                    "x_mm": x,
                    "y_mm": y,
                    "snap_to_grid": True,
                },
            )
            rotations[item["reference"]] = int(item["rotation"]) % 360
            placed.append(
                {
                    "reference": item["reference"],
                    "rotation": int(item["rotation"]) % 360,
                    "position": [round(x, 4), round(y, 4)],
                }
            )

        return json.dumps(
            {
                "side": side,
                "far_direction": far_direction,
                "distance_steps": distance_steps,
                "rotations": rotations,
                "placed": placed,
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_route_pin_rail(
        connections: list[dict[str, str]],
        net: str,
        direction: str,
        clearance_steps: int = 2,
    ) -> str:
        """Route a clean rail outside endpoint envelopes with one visible rail label."""
        if not connections:
            raise ValueError("rail connections must be non-empty")
        if clearance_steps < 1 or clearance_steps > 8:
            raise ValueError("clearance_steps must be between 1 and 8")
        _relation_direction(direction)
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)

        endpoints: list[
            tuple[
                str,
                str,
                float,
                float,
                tuple[float, float, float, float],
            ]
        ] = []
        for item in connections:
            reference = str(item["reference"])
            pin = str(item["pin"])
            symbol = _symbol_by_reference(data, reference)
            rotation = int(
                symbol.get("rotation", symbol.get("rot", 0)) or 0
            ) % 360
            pins = _relation_pin_positions(symbol, rotation)
            if pin not in pins:
                raise ValueError(f"Pin {reference}.{pin} was not found")
            px, py = pins[pin]
            endpoints.append(
                (
                    reference,
                    pin,
                    float(px),
                    float(py),
                    _symbol_bbox_bounds(symbol),
                )
            )

        gap = SCHEMATIC_GRID_MM * max(1, clearance_steps)

        async def add_power_symbol(x: float, y: float) -> None:
            await call_checked(
                "sch_add_power_symbol",
                {
                    "name": net,
                    "x_mm": x,
                    "y_mm": y,
                    "rotation": 0,
                    "snap_to_grid": True,
                },
            )

        async def independent_terminals(why: dict[str, int] | None = None) -> str:
            # A collapsed rail still needs an electrical attachment, not
            # coincident symbol bodies. Reuse the same obstacle-aware terminal
            # placement as every other independent power connection.
            attachments = [
                {"reference": reference, "pin": pin, "net": net}
                for reference, pin, _px, _py, _bbox in endpoints
            ]
            await call_checked(
                "sch_add_pin_power_terminals",
                {
                    "connections": attachments,
                    "clearance_mm": max(3.81, gap),
                },
            )
            return json.dumps(
                {
                    "net": net,
                    "direction": direction,
                    "mode": "independent_terminals",
                    "attachment_policy": "obstacle_aware_visible_stubs",
                    "connections": [
                        f"{item['reference']}.{item['pin']}"
                        for item in attachments
                    ],
                    **({"shared_rail_refused": why} if why else {}),
                },
                sort_keys=True,
            )

        # A one-pin rail, or a group with no meaningful transverse span, is
        # clearer as independent conventional rail symbols than as a zero-width
        # bus that doubles back through the parts.
        horizontal = direction in {"above", "below"}
        transverse = (
            [item[2] for item in endpoints]
            if horizontal
            else [item[3] for item in endpoints]
        )
        span = max(transverse) - min(transverse)
        if len(endpoints) == 1 or span < SCHEMATIC_GRID_MM * 2.0:
            return await independent_terminals()

        if layout_clearances["tight_terminals"]:
            # So is a rail that would cut across what is already on the sheet: a decoupling bank whose ground rail
            # runs across the supply wires to its own capacitors reads as a short between them. Each pin gets a
            # symbol of its own instead, where the terminal planner can keep clear of the wires.
            from backend.schematic.presentation_geometry import Bounds
            from backend.schematic.terminal_placement import rail_obstructions

            if horizontal:
                rail_at = (
                    min(item[4][1] for item in endpoints) - gap
                    if direction == "above"
                    else max(item[4][3] for item in endpoints) + gap
                )
            else:
                rail_at = (
                    min(item[4][0] for item in endpoints) - gap
                    if direction == "left"
                    else max(item[4][2] for item in endpoints) + gap
                )
            sheet = terminal_sheet(max(3.81, gap))
            members = {item[0] for item in endpoints}
            tokens = {f"{item[0]}.{item[1]}" for item in endpoints}
            obstructed = rail_obstructions(
                horizontal=horizontal,
                rail=round(rail_at / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM,
                pins=tuple((item[2], item[3]) for item in endpoints),
                foreign_bounds=tuple(
                    Bounds(*box) for ref, box in sheet.bounds_by_ref.items() if ref not in members
                ),
                foreign_pin_points=tuple(
                    point for token, point in sheet.pin_tips.items() if token not in tokens
                ),
                existing_wire_segments=tuple(sheet.wires),
                grid_mm=SCHEMATIC_GRID_MM,
            )
            if obstructed:
                return await independent_terminals(obstructed)

        written: list[list[float]] = []

        async def add_wire(
            x1: float,
            y1: float,
            x2: float,
            y2: float,
        ) -> None:
            if abs(x1 - x2) <= 1e-6 and abs(y1 - y2) <= 1e-6:
                return
            await call_checked(
                "sch_add_wire",
                {
                    "x1_mm": x1,
                    "y1_mm": y1,
                    "x2_mm": x2,
                    "y2_mm": y2,
                    "snap_to_grid": True,
                },
            )
            written.append(
                [
                    round(x1, 4),
                    round(y1, 4),
                    round(x2, 4),
                    round(y2, 4),
                ]
            )

        def place_rail_symbol(
            horizontal: bool, rail: float, low: float, high: float, middle: tuple[float, float],
        ) -> tuple[float, float]:
            """The symbol sits in the middle of the rail. A tight layout leaves little room around it, so there
            the same planner as for every other power symbol finds a free place along the rail, or just past
            its ends; MCP Pro's own boxes (the rollback baseline) keep the middle."""
            if not layout_clearances["tight_terminals"]:
                return middle
            from backend.schematic.presentation_geometry import Bounds
            from backend.schematic.terminal_placement import choose_rail_symbol

            sheet = terminal_sheet(max(3.81, gap))
            tokens = [f"{item[0]}.{item[1]}" for item in endpoints]
            turned_away: dict[str, int] = {}
            plan = choose_rail_symbol(
                horizontal=horizontal, rail=rail, low=low, high=high,
                end_symbol=power_end_symbol(net),
                foreign_bounds=tuple(Bounds(*box) for box in sheet.bounds_by_ref.values()),
                foreign_pin_points=tuple(
                    point for token, point in sheet.pin_tips.items() if token not in tokens
                ),
                existing_wire_segments=tuple(sheet.wires),
                text_boxes=tuple(sheet.text_boxes),
                grid_mm=SCHEMATIC_GRID_MM,
                rejected=turned_away,
            )
            if plan is None:
                why = ", ".join(
                    f"{reason} {count}" for reason, count in sorted(turned_away.items(), key=lambda kv: -kv[1])[:4]
                )
                raise ValueError(
                    f"No unobstructed power terminal exit from {tokens[0]} for net {net}; the shared rail of "
                    f"{', '.join(tokens)} has no free place for its symbol; reposition its source or nearby "
                    "geometry" + (f" (turned away: {why})" if why else "")
                )
            return plan.position

        if direction in {"above", "below"}:
            rail_y = (
                min(item[4][1] for item in endpoints) - gap
                if direction == "above"
                else max(item[4][3] for item in endpoints) + gap
            )
            rail_y = round(rail_y / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
            xs = [item[2] for item in endpoints]
            low, high = min(xs), max(xs)
            symbol_x = round(
                ((low + high) / 2.0) / SCHEMATIC_GRID_MM
            ) * SCHEMATIC_GRID_MM
            symbol_x, symbol_y = place_rail_symbol(True, rail_y, low, high, (symbol_x, rail_y))

            # Split the trunk at the rail symbol so the symbol pin is an
            # explicit wire endpoint rather than merely lying on a segment.
            # A symbol past the end of the rail extends it.
            breaks = sorted({low, symbol_x, high})
            for start, end in zip(breaks, breaks[1:]):
                await add_wire(start, rail_y, end, rail_y)
            for _ref, _pin, px, py, _bbox in endpoints:
                await add_wire(px, py, px, rail_y)
        else:
            rail_x = (
                min(item[4][0] for item in endpoints) - gap
                if direction == "left"
                else max(item[4][2] for item in endpoints) + gap
            )
            rail_x = round(rail_x / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
            ys = [item[3] for item in endpoints]
            low, high = min(ys), max(ys)
            symbol_y = round(
                ((low + high) / 2.0) / SCHEMATIC_GRID_MM
            ) * SCHEMATIC_GRID_MM
            symbol_x, symbol_y = place_rail_symbol(False, rail_x, low, high, (rail_x, symbol_y))

            breaks = sorted({low, symbol_y, high})
            for start, end in zip(breaks, breaks[1:]):
                await add_wire(rail_x, start, rail_x, end)
            for _ref, _pin, px, py, _bbox in endpoints:
                await add_wire(px, py, rail_x, py)

        await add_power_symbol(symbol_x, symbol_y)
        return json.dumps(
            {
                "net": net,
                "direction": direction,
                "mode": "shared",
                "rail_symbol_position": [
                    round(symbol_x, 4),
                    round(symbol_y, 4),
                ],
                "segments": written,
                "connections": [
                    f"{reference}.{pin}"
                    for reference, pin, _px, _py, _bbox in endpoints
                ],
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_place_pin_at_offset(
        reference: str,
        pin: str,
        anchor_ref: str,
        anchor_pin: str,
        offset_x_mm: float,
        offset_y_mm: float,
        rotation: int = 0,
    ) -> str:
        """Place a symbol so one pin lands at an offset from another real pin.

        Companion supplies semantic motif geometry only. MCP Pro resolves both
        library pin locations from the actual KiCad symbols, computes one
        grid-aligned symbol translation, and delegates mutation to
        sch_move_symbol. The rotation is the intended final orientation and is
        used for pin geometry; the workflow rebuild applies it later.
        """
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        subject = _symbol_by_reference(data, reference)
        anchor = _symbol_by_reference(data, anchor_ref)

        subject_lib = str(subject.get("lib_id", ""))
        anchor_lib = str(anchor.get("lib_id", ""))
        if ":" not in subject_lib or ":" not in anchor_lib:
            raise ValueError("Both symbols must have KiCad library IDs")
        subject_library, subject_name = subject_lib.split(":", 1)
        anchor_library, anchor_name = anchor_lib.split(":", 1)

        sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
        sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
        ax = float(anchor.get("x", anchor.get("x_mm", 0.0)) or 0.0)
        ay = float(anchor.get("y", anchor.get("y_mm", 0.0)) or 0.0)
        subject_unit = int(subject.get("unit", 1) or 1)
        anchor_unit = int(anchor.get("unit", 1) or 1)
        anchor_rotation = int(
            anchor.get("rotation", anchor.get("rot", 0)) or 0
        ) % 360

        subject_pins = get_pin_positions(
            subject_library,
            subject_name,
            sx,
            sy,
            int(rotation) % 360,
            subject_unit,
        )
        anchor_pins = get_pin_positions(
            anchor_library,
            anchor_name,
            ax,
            ay,
            anchor_rotation,
            anchor_unit,
        )
        if pin not in subject_pins:
            raise ValueError(f"Pin {reference}.{pin} was not found")
        if anchor_pin not in anchor_pins:
            raise ValueError(f"Pin {anchor_ref}.{anchor_pin} was not found")

        subject_px, subject_py = subject_pins[pin]
        anchor_px, anchor_py = anchor_pins[anchor_pin]
        target_px = float(anchor_px) + float(offset_x_mm)
        target_py = float(anchor_py) + float(offset_y_mm)

        dx = target_px - float(subject_px)
        dy = target_py - float(subject_py)
        target_x = sx + dx
        target_y = sy + dy
        target_x = round(target_x / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
        target_y = round(target_y / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM

        await call_checked(
            "sch_move_symbol",
            {
                "reference": reference,
                "x_mm": target_x,
                "y_mm": target_y,
                "snap_to_grid": True,
            },
        )

        updated = parse_schematic_file(schematic)
        placed = _symbol_by_reference(updated, reference)
        actual_x = float(placed.get("x", placed.get("x_mm", 0.0)) or 0.0)
        actual_y = float(placed.get("y", placed.get("y_mm", 0.0)) or 0.0)
        final_pin = get_pin_positions(
            subject_library,
            subject_name,
            actual_x,
            actual_y,
            int(rotation) % 360,
            subject_unit,
        )[pin]
        return json.dumps(
            {
                "reference": reference,
                "pin": pin,
                "anchor_ref": anchor_ref,
                "anchor_pin": anchor_pin,
                "rotation": int(rotation) % 360,
                "offset_mm": [
                    round(float(offset_x_mm), 4),
                    round(float(offset_y_mm), 4),
                ],
                "anchor_pin_position": [
                    round(float(anchor_px), 4),
                    round(float(anchor_py), 4),
                ],
                "target_pin_position": [
                    round(target_px, 4),
                    round(target_py, 4),
                ],
                "actual_symbol_position": [
                    round(actual_x, 4),
                    round(actual_y, 4),
                ],
                "actual_pin_position_at_final_rotation": [
                    round(float(final_pin[0]), 4),
                    round(float(final_pin[1]), 4),
                ],
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_add_pin_power_symbols(
        connections: list[dict[str, Any]],
    ) -> str:
        """Attach conventional power symbols directly to actual symbol pins."""
        schematic = _get_schematic_file()
        diagnostics: list[dict[str, Any]] = []

        for entry in connections:
            data = parse_schematic_file(schematic)
            reference = str(entry.get("reference", "")).strip()
            pin = str(entry.get("pin", "")).strip()
            net = str(entry.get("net", "")).strip()
            rotation = int(entry.get("rotation", 0) or 0) % 360
            if not reference or not pin or not net:
                raise ValueError("connections need reference, pin, and net")

            symbol = _symbol_by_reference(data, reference)
            lib_id = str(symbol.get("lib_id", ""))
            if ":" not in lib_id:
                raise ValueError(f"Symbol {reference} has no KiCad library ID")
            library, symbol_name = lib_id.split(":", 1)
            x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
            y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
            symbol_rotation = int(
                symbol.get("rotation", symbol.get("rot", 0)) or 0
            ) % 360
            unit = int(symbol.get("unit", 1) or 1)
            pins = get_pin_positions(
                library,
                symbol_name,
                x,
                y,
                symbol_rotation,
                unit,
            )
            if pin not in pins:
                raise ValueError(f"Pin {reference}.{pin} was not found")
            px, py = pins[pin]

            await call_checked(
                "sch_add_power_symbol",
                {
                    "name": net,
                    "x_mm": float(px),
                    "y_mm": float(py),
                    "rotation": rotation,
                    "snap_to_grid": True,
                },
            )
            diagnostics.append(
                {
                    "reference": reference,
                    "pin": pin,
                    "net": net,
                    "rotation": rotation,
                    "position": [
                        round(float(px), 4),
                        round(float(py), 4),
                    ],
                }
            )

        return json.dumps(
            {"connections": diagnostics},
            sort_keys=True,
        )

    @server.tool()
    async def sch_align_relative_pins(
        reference: str,
        pin: str,
        anchor_ref: str,
        anchor_pin: str,
        relation: str,
        rotation: int = 0,
        anchor_rotation: int = 0,
        steps: int = 1,
    ) -> str:
        """Refine relative placement so connected pins share the branch axis.

        For left/right relations, the two selected pins are aligned vertically.
        For above/below relations, they are aligned horizontally. MCP Pro's own
        get_pin_positions() resolves all symbol-library geometry.
        """
        if relation not in _RELATIONS:
            raise ValueError(
                "relation must be one of: " + ", ".join(sorted(_RELATIONS))
            )
        if steps < 1 or steps > 8:
            raise ValueError("steps must be between 1 and 8")
        if reference == anchor_ref:
            raise ValueError("reference and anchor_ref must differ")

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        subject = _symbol_by_reference(data, reference)
        anchor = _symbol_by_reference(data, anchor_ref)

        subject_lib = str(subject.get("lib_id", ""))
        anchor_lib = str(anchor.get("lib_id", ""))
        if ":" not in subject_lib or ":" not in anchor_lib:
            raise ValueError("Both symbols must have KiCad library IDs")
        subject_library, subject_name = subject_lib.split(":", 1)
        anchor_library, anchor_name = anchor_lib.split(":", 1)

        sx = float(subject.get("x", subject.get("x_mm", 0.0)) or 0.0)
        sy = float(subject.get("y", subject.get("y_mm", 0.0)) or 0.0)
        ax = float(anchor.get("x", anchor.get("x_mm", 0.0)) or 0.0)
        ay = float(anchor.get("y", anchor.get("y_mm", 0.0)) or 0.0)
        subject_unit = int(subject.get("unit", 1) or 1)
        anchor_unit = int(anchor.get("unit", 1) or 1)

        # Preserve MCP Pro's normal center-to-center relation spacing first.
        target_x = ax
        target_y = ay
        if relation == "left_of":
            target_x -= AUTO_LAYOUT_COLUMN_SPACING_MM * steps
        elif relation == "right_of":
            target_x += AUTO_LAYOUT_COLUMN_SPACING_MM * steps
        elif relation == "above":
            target_y -= AUTO_LAYOUT_ROW_SPACING_MM * steps
        elif relation == "below":
            target_y += AUTO_LAYOUT_ROW_SPACING_MM * steps

        anchor_pins = get_pin_positions(
            anchor_library,
            anchor_name,
            ax,
            ay,
            int(anchor_rotation) % 360,
            anchor_unit,
        )
        subject_pins = get_pin_positions(
            subject_library,
            subject_name,
            target_x,
            target_y,
            int(rotation) % 360,
            subject_unit,
        )
        if anchor_pin not in anchor_pins:
            raise ValueError(f"Pin {anchor_ref}.{anchor_pin} was not found")
        if pin not in subject_pins:
            raise ValueError(f"Pin {reference}.{pin} was not found")

        anchor_px, anchor_py = anchor_pins[anchor_pin]
        subject_px, subject_py = subject_pins[pin]
        anchor_normals = get_pin_outward_normals(
            anchor_library,
            anchor_name,
            int(anchor_rotation) % 360,
            anchor_unit,
        )
        anchor_normal = anchor_normals.get(anchor_pin, (0.0, 0.0))
        escape_x = float(anchor_px) + float(anchor_normal[0]) * SCHEMATIC_GRID_MM
        escape_y = float(anchor_py) + float(anchor_normal[1]) * SCHEMATIC_GRID_MM

        # Align to the router's first outward escape axis rather than the raw
        # pin center. This keeps a perpendicular shunt branch on the same axis
        # where MCP Pro naturally forms its first T-intersection, while
        # preserving the requested center-to-center spacing along the relation.
        if relation in {"above", "below"}:
            target_x += escape_x - float(subject_px)
        else:
            target_y += escape_y - float(subject_py)

        await call_checked(
            "sch_move_symbol",
            {
                "reference": reference,
                "x_mm": target_x,
                "y_mm": target_y,
                "snap_to_grid": True,
            },
        )

        updated = parse_schematic_file(schematic)
        placed = _symbol_by_reference(updated, reference)
        actual_x = float(placed.get("x", placed.get("x_mm", 0.0)) or 0.0)
        actual_y = float(placed.get("y", placed.get("y_mm", 0.0)) or 0.0)
        return json.dumps(
            {
                "reference": reference,
                "pin": pin,
                "anchor_ref": anchor_ref,
                "anchor_pin": anchor_pin,
                "relation": relation,
                "rotation": int(rotation) % 360,
                "anchor_rotation": int(anchor_rotation) % 360,
                "anchor_pin_position": [
                    round(float(anchor_px), 4),
                    round(float(anchor_py), 4),
                ],
                "anchor_outward_normal": [
                    float(anchor_normal[0]),
                    float(anchor_normal[1]),
                ],
                "anchor_escape_position": [
                    round(escape_x, 4),
                    round(escape_y, 4),
                ],
                "x_mm": actual_x,
                "y_mm": actual_y,
            },
            sort_keys=True,
        )

    @server.tool()
    def sch_choose_orientations(
        constraints: list[dict[str, Any]],
    ) -> str:
        """Choose symbol rotations from MCP Pro pin geometry.

        Each constraint identifies a symbol pin and a reference that pin should
        face. The tool evaluates 0/90/180/270 with get_pin_positions() and returns
        the lowest-distance rotation for each constrained symbol.
        """
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        grouped: dict[str, list[dict[str, Any]]] = {}

        for constraint in constraints:
            reference = str(constraint.get("reference", "")).strip()
            pin = str(constraint.get("pin", "")).strip()
            toward_ref = str(constraint.get("toward_ref", "")).strip()
            library = str(constraint.get("library", "")).strip()
            symbol_name = str(constraint.get("symbol_name", "")).strip()
            if not all((reference, pin, toward_ref, library, symbol_name)):
                raise ValueError(
                    "orientation constraints need reference, pin, toward_ref, "
                    "library, and symbol_name"
                )
            grouped.setdefault(reference, []).append(constraint)

        rotations: dict[str, int] = {}
        mirrors: dict[str, str] = {}
        diagnostics: dict[str, Any] = {}

        def target_pin_geometry(
            toward_ref: str,
            toward_pin: str,
        ) -> tuple[tuple[float, float], tuple[float, float]] | None:
            if not toward_pin:
                return None
            target = _symbol_by_reference(data, toward_ref)
            lib_id = str(target.get("lib_id", ""))
            if ":" not in lib_id:
                return None
            target_library, target_name = lib_id.split(":", 1)
            target_unit = int(target.get("unit", 1) or 1)
            target_rotation = int(
                target.get("rotation", target.get("rot", 0)) or 0
            ) % 360
            tx = float(target.get("x", target.get("x_mm", 0.0)) or 0.0)
            ty = float(target.get("y", target.get("y_mm", 0.0)) or 0.0)
            positions = get_pin_positions(
                target_library, target_name, tx, ty, target_rotation, target_unit
            )
            if toward_pin not in positions:
                return None
            normals = get_pin_outward_normals(
                target_library, target_name, target_rotation, target_unit
            )
            point = positions[toward_pin]
            normal = normals.get(toward_pin, (0.0, 0.0))
            return (
                (float(point[0]), float(point[1])),
                (float(normal[0]), float(normal[1])),
            )

        for reference, items in grouped.items():
            placed = _symbol_by_reference(data, reference)
            x = float(placed.get("x", placed.get("x_mm", 0.0)) or 0.0)
            y = float(placed.get("y", placed.get("y_mm", 0.0)) or 0.0)
            unit = int(placed.get("unit", 1) or 1)
            library = str(items[0]["library"])
            symbol_name = str(items[0]["symbol_name"])

            # A symbol whose pins read in order (a connector) is also tried mirrored: turning it half a turn puts its
            # pins on the other side but reverses them, mirroring it keeps them in order.
            ordered = any(bool(item.get("ordered")) for item in items)
            targets: list[PinTarget] = []
            for item in items:
                pin = str(item["pin"])
                toward_ref = str(item["toward_ref"])
                geometry = target_pin_geometry(toward_ref, str(item.get("toward_pin", "")))
                if geometry is None:
                    target = _symbol_by_reference(data, toward_ref)
                    targets.append(PinTarget(pin, fallback=(
                        float(target.get("x", target.get("x_mm", 0.0)) or 0.0),
                        float(target.get("y", target.get("y_mm", 0.0)) or 0.0),
                    )))
                else:
                    targets.append(PinTarget(pin, geometry[0], geometry[1]))
            candidates = {
                Orientation(rotation, mirror): (
                    {
                        str(pin): (float(point[0]), float(point[1]))
                        for pin, point in get_pin_positions(
                            library, symbol_name, x, y, rotation, unit, mirror=mirror,
                        ).items()
                    },
                    {
                        str(pin): (float(vector[0]), float(vector[1]))
                        for pin, vector in get_pin_outward_normals(
                            library, symbol_name, rotation, unit, mirror=mirror,
                        ).items()
                    },
                )
                for rotation in ROTATIONS for mirror in (MIRRORS if ordered else ("",))
            }
            try:
                best, scores = choose_orientation(
                    candidates, targets, ordered=ordered, mirrors=ordered, grid_mm=SCHEMATIC_GRID_MM,
                )
            except ValueError as error:
                raise ValueError(f"No valid orientation candidate for {reference}; check symbol pin numbers.") from error

            rotations[reference] = best.rotation
            if best.mirror:
                mirrors[reference] = best.mirror
            diagnostics[reference] = {
                "rotation": best.rotation,
                "mirror": best.mirror,
                "scores": {
                    f"{orientation.rotation}{orientation.mirror}": round(score, 6)
                    for orientation, score in scores.items()
                },
                "constraints": items,
            }

        return json.dumps(
            {"rotations": rotations, "mirrors": mirrors, "diagnostics": diagnostics},
            sort_keys=True,
        )

    @server.tool()
    def sch_symbol_geometry(
        references: list[str],
        orientations: dict[str, list[dict[str, Any]]] | None = None,
    ) -> str:
        """Each symbol's pins, the way they point and the box placement keeps clear, at the orientations asked for.

        Everything is relative to the symbol's anchor, so a position and an orientation can be chosen without moving
        the symbol. The box is what the layout policy gives (``sch_set_layout_clearances``): the box the placement tools
        keep clear, margin included. ``orientations`` maps a reference to the ``{rotation, mirror}`` pairs wanted; a
        symbol not in it is described at the orientation it has now.
        """
        from backend.schematic.placement_plan import describe_symbol

        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        described: dict[str, Any] = {}
        for reference in references:
            symbol = _symbol_by_reference(data, reference)
            lib_id = str(symbol.get("lib_id", ""))
            if ":" not in lib_id:
                raise ValueError(f"Symbol {reference} has no KiCad library ID")
            library, symbol_name = lib_id.split(":", 1)
            unit = int(symbol.get("unit", 1) or 1)
            x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
            y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
            wanted = [
                Orientation(int(item.get("rotation", 0)) % 360, str(item.get("mirror", "") or ""))
                for item in (orientations or {}).get(reference, ())
            ] or [Orientation(
                int(symbol.get("rotation", symbol.get("rot", 0)) or 0) % 360, str(symbol.get("mirror", "") or ""),
            )]

            def box_at(orientation: Orientation) -> tuple[float, float, float, float]:
                virtual = {
                    **symbol, "x": 0.0, "y": 0.0, "x_mm": 0.0, "y_mm": 0.0,
                    "rotation": orientation.rotation, "mirror": orientation.mirror,
                }
                box = _symbol_bbox_bounds(virtual)
                # MCP Pro's own box is of the symbol unmirrored; the tight box above already follows the mirror
                if layout_clearances["mode"] != "tight" and orientation.mirror:
                    box = mirror_box(box, (0.0, 0.0), orientation.mirror)
                return box

            def body_at(orientation: Orientation) -> tuple[float, float, float, float]:
                # the body and pin tips, without the layout policy's margin or MCP Pro's 20 x 15 mm minimum
                virtual = {
                    **symbol, "x": 0.0, "y": 0.0, "x_mm": 0.0, "y_mm": 0.0,
                    "rotation": orientation.rotation, "mirror": orientation.mirror,
                }
                tight = _placed_symbol_bbox(virtual)
                return mirror_box(
                    (float(tight.x_min), float(tight.y_min), float(tight.x_max), float(tight.y_max)),
                    (0.0, 0.0), orientation.mirror,
                )

            described[reference] = describe_symbol(
                x, y, wanted,
                lambda o: get_pin_positions(library, symbol_name, 0.0, 0.0, o.rotation, unit, mirror=o.mirror),
                lambda o: get_pin_outward_normals(library, symbol_name, o.rotation, unit, mirror=o.mirror),
                box_at,
                body_at,
            )
        return json.dumps({"symbols": described}, sort_keys=True)

    @server.tool()
    def sch_set_symbol_mirror(mirrors: dict[str, str]) -> str:
        """Mirror placed symbols about their anchors: ``y`` flips a symbol left to right, ``x`` top to bottom, "" clears.

        The symbol, its pins and its fields keep their anchors; only KiCad's ``(mirror x|y)`` line changes
        (``backend.schematic.symbol_edit``). Every geometry helper in this server reads it back.
        """
        from kicad_mcp.tools import schematic as schematic_tools

        from backend.schematic.symbol_edit import set_mirror

        def mutator(content: str) -> str:
            for reference, axis in mirrors.items():
                content = set_mirror(content, str(reference), str(axis or ""))
            return content

        schematic_tools._transactional_write_to_schematic(mutator)
        return json.dumps({"mirrored": {str(ref): str(axis) for ref, axis in mirrors.items()}}, sort_keys=True)

    @server.tool()
    def sch_move_unit(reference: str, x_mm: float, y_mm: float, snap_to_grid: bool = True) -> str:
        """Move one placed instance by its placeable name: ``U5:2`` is unit 2 of ``U5`` (a plain reference is its first instance).

        MCP Pro's ``sch_move_symbol`` finds a symbol by its reference alone, so it cannot say which unit of a multi-unit part
        to move; this one can. The instance and its field texts move together (``backend.schematic.symbol_edit.move_symbol``).
        """
        from kicad_mcp.tools import schematic as schematic_tools

        from backend.schematic.symbol_edit import move_symbol

        x, y = float(x_mm), float(y_mm)
        if snap_to_grid:
            x = round(x / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
            y = round(y / SCHEMATIC_GRID_MM) * SCHEMATIC_GRID_MM
        schematic_tools._transactional_write_to_schematic(lambda content: move_symbol(content, str(reference), x, y))
        return json.dumps({"reference": str(reference), "x_mm": round(x, 4), "y_mm": round(y, 4)}, sort_keys=True)

    @server.tool()
    def sch_frame_functional_blocks(
        blocks: list[dict[str, Any]],
        padding_mm: float = 5.08,
    ) -> str:
        """Draw rough labeled rectangles around functional blocks.

        These are diagnostic, non-electrical graphics only. Bounds come from
        MCP Pro's current pin-aware symbol geometry. No overlap avoidance or
        layout mutation is attempted.
        """
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        padding = max(SCHEMATIC_GRID_MM, float(padding_mm))
        framed: list[dict[str, Any]] = []

        def mutator(content: str) -> str:
            updated = content
            for block in blocks:
                name = str(block.get("name", "")).strip()
                label = str(block.get("label", "")).strip() or name
                refs = [
                    str(ref).strip()
                    for ref in block.get("references", [])
                    if str(ref).strip()
                ]
                if not name or not refs:
                    continue

                bounds = [
                    _symbol_bbox_bounds(_symbol_by_reference(data, reference))
                    for reference in refs
                ]
                x1 = min(item[0] for item in bounds) - padding
                y1 = min(item[1] for item in bounds) - padding
                x2 = max(item[2] for item in bounds) + padding
                y2 = max(item[3] for item in bounds) + padding

                rectangle = (
                    "\t(rectangle\n"
                    f"\t\t(start {_fmt_mm(x1)} {_fmt_mm(y1)})\n"
                    f"\t\t(end {_fmt_mm(x2)} {_fmt_mm(y2)})\n"
                    "\t\t(stroke\n"
                    "\t\t\t(width 0.254)\n"
                    "\t\t\t(type default)\n"
                    "\t\t)\n"
                    "\t\t(fill\n"
                    "\t\t\t(type none)\n"
                    "\t\t)\n"
                    f'\t\t(uuid "{new_uuid()}")\n'
                    "\t)"
                )
                text_x = x1 + SCHEMATIC_GRID_MM
                text_y = y1 + SCHEMATIC_GRID_MM
                title = (
                    f"\t(text {_sexpr_string(label)}\n"
                    "\t\t(exclude_from_sim no)\n"
                    f"\t\t(at {_fmt_mm(text_x)} {_fmt_mm(text_y)} 0)\n"
                    "\t\t(effects\n"
                    "\t\t\t(font\n"
                    "\t\t\t\t(size 1.27 1.27)\n"
                    "\t\t\t\t(bold yes)\n"
                    "\t\t\t)\n"
                    "\t\t\t(justify left bottom)\n"
                    "\t\t)\n"
                    f'\t\t(uuid "{new_uuid()}")\n'
                    "\t)"
                )
                updated = _append_before_sheet_instances(updated, rectangle)
                updated = _append_before_sheet_instances(updated, title)
                framed.append(
                    {
                        "name": name,
                        "label": label,
                        "references": refs,
                        "bounds": [
                            round(x1, 4),
                            round(y1, 4),
                            round(x2, 4),
                            round(y2, 4),
                        ],
                    }
                )
            return updated

        _transactional_write_to_schematic(mutator)
        return json.dumps(
            {
                "framed": framed,
                "padding_mm": padding,
            },
            sort_keys=True,
        )

    @server.tool()
    async def sch_mark_no_connect_pins(
        entries: list[dict[str, str]],
    ) -> str:
        """Place KiCad no-connect markers from reference/pin semantics.

        Pin coordinates and symbol rotation are resolved entirely inside MCP Pro.
        """
        schematic = _get_schematic_file()
        data = parse_schematic_file(schematic)
        marked: list[dict[str, Any]] = []

        for entry in entries:
            reference = str(entry.get("reference", "")).strip()
            pin = str(entry.get("pin", "")).strip()
            if not reference or not pin:
                raise ValueError("no-connect entries need reference and pin")

            symbol = _symbol_by_reference(data, reference)
            lib_id = str(symbol.get("lib_id", ""))
            if ":" not in lib_id:
                raise ValueError(f"Symbol {reference} has no KiCad library ID")
            library, symbol_name = lib_id.split(":", 1)
            x = float(symbol.get("x", symbol.get("x_mm", 0.0)) or 0.0)
            y = float(symbol.get("y", symbol.get("y_mm", 0.0)) or 0.0)
            rotation = int(
                symbol.get("rotation", symbol.get("rot", 0)) or 0
            ) % 360
            unit = int(symbol.get("unit", 1) or 1)

            positions = get_pin_positions(
                library,
                symbol_name,
                x,
                y,
                rotation,
                unit,
            )
            if pin not in positions:
                raise ValueError(f"Pin {reference}.{pin} was not found")
            px, py = positions[pin]
            await call_checked(
                "sch_add_no_connect",
                {
                    "x_mm": float(px),
                    "y_mm": float(py),
                    "snap_to_grid": True,
                },
            )
            marked.append(
                {
                    "reference": reference,
                    "pin": pin,
                    "x_mm": round(float(px), 4),
                    "y_mm": round(float(py), 4),
                    "rotation": rotation,
                }
            )

        return json.dumps({"marked": marked}, sort_keys=True)

    @server.tool()
    def sch_modify_field_orientation(
        references: list[str],
        fields: list[str] | None = None,
        angle: int = 0,
    ) -> str:
        """Set visible symbol-field angles without moving their positions.

        This uses MCP Pro's transactional schematic writer and its existing
        property-position helper. It is intended for keeping Reference/Value
        text upright after a deliberate symbol rotation.
        """
        from kicad_mcp.tools import schematic as schematic_tools

        if angle not in {0, 90, 180, 270}:
            raise ValueError("angle must be one of 0, 90, 180, 270")
        selected_fields = fields or ["Reference", "Value"]
        changed: list[str] = []

        def property_position(block: str, field: str) -> tuple[float, float] | None:
            marker = f'(property "{field}"'
            index = block.find(marker)
            if index < 0:
                return None
            prop_block, _length = schematic_tools._extract_block(block, index)
            if not prop_block:
                return None
            match = re.search(
                r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+[-\d.]+\)",
                prop_block,
            )
            if match is None:
                return None
            return float(match.group(1)), float(match.group(2))

        def mutator(content: str) -> str:
            updated = content
            for reference in references:
                match = find_symbol_block(updated, reference)
                if match is None:
                    raise ValueError(f"Reference not found: {reference}")
                block, start, end, _parsed = match
                new_block = block
                for field in selected_fields:
                    position = property_position(new_block, field)
                    if position is None:
                        continue
                    x, y = position
                    changed_block = schematic_tools._set_property_position(
                        new_block,
                        field,
                        x,
                        y,
                        angle,
                    )
                    if changed_block != new_block:
                        changed.append(f"{reference}.{field}")
                    new_block = changed_block
                updated = updated[:start] + new_block + updated[end:]
            return updated

        schematic_tools._transactional_write_to_schematic(mutator)
        return json.dumps(
            {
                "updated_fields": changed,
                "angle": angle,
            },
            sort_keys=True,
        )

    @server.tool()
    def sch_place_fields(placements: list[dict[str, Any]]) -> str:
        """Move symbols' Reference and Value text to chosen places.

        Each placement names a ``reference``, a ``field`` (Reference or Value), the new anchor ``x_mm`` and ``y_mm``
        and the field's ``angle``. Only the field's own position is rewritten, in one transaction through MCP Pro's
        schematic writer; the symbol, its pins and every other field stay as they are.
        """
        from kicad_mcp.tools import schematic as schematic_tools

        moved: list[str] = []

        def mutator(content: str) -> str:
            updated = content
            for entry in placements:
                reference = str(entry.get("reference", "")).strip()
                field = str(entry.get("field", "")).strip()
                if not reference or field not in {"Reference", "Value"}:
                    raise ValueError("a field placement needs a reference and a field (Reference or Value)")
                match = find_symbol_block(updated, reference)
                if match is None:
                    raise ValueError(f"Reference not found: {reference}")
                block, start, end, _parsed = match
                new_block = schematic_tools._set_property_position(
                    block, field, float(entry["x_mm"]), float(entry["y_mm"]), float(entry.get("angle", 0)),
                )
                if new_block != block:
                    moved.append(f"{reference}.{field}")
                updated = updated[:start] + new_block + updated[end:]
            return updated

        schematic_tools._transactional_write_to_schematic(mutator)
        return json.dumps({"moved": moved, "requested": len(placements)}, sort_keys=True)

    @server.tool()
    def sch_attach_power_flags(flags: list[dict[str, Any]]) -> str:
        """Put each power flag on a wire of the net it marks, standing off the wire, instead of giving it symbols.

        Each entry names the flag's ``reference`` and the ``hosts`` (pins, "REF.PIN") of its net. The flag moves to a
        grid point on a wire that touches one of them, where its graphic and name keep clear of everything else
        (``backend.schematic.flag_attachment``); the wire is cut there, because KiCad joins a pin to the ends of
        wires only. A flag with no free place is reported in ``unplaced`` and left where it was, for the caller to
        give a terminal of its own.
        """
        from kicad_mcp.tools import schematic as schematic_tools

        from backend.schematic.flag_attachment import plan_flag_attachment
        from backend.schematic.flag_edit import attach_flag
        from backend.schematic.kicad_geometry import measured_scene_from_kicad, text_width_mm
        from backend.schematic.field_placement import routing_text_obstacles
        from backend.schematic.presentation_geometry import TEXT_KINDS, Bounds, GeometryKind
        from backend.schematic.wire_nets import wire_network

        attached: list[dict[str, Any]] = []
        unplaced: list[str] = []
        schematic = _get_schematic_file()
        for entry in flags:
            reference = str(entry.get("reference", "")).strip()
            hosts = [str(host) for host in entry.get("hosts", [])]
            if not reference or not hosts:
                raise ValueError("a power flag needs a reference and the pins of its net")
            scene, _coverage = measured_scene_from_kicad(schematic)
            network = wire_network(scene)
            segment_of = {
                item.id: item.segment for item in scene.items
                if item.kind == GeometryKind.WIRE and item.segment is not None
            }
            components = {network.by_pin[host] for host in hosts if host in network.by_pin}
            own_ids = {sid for index in components for sid in network.components[index].segments}
            own_pin = f"{reference}.1"
            own_items = [item for item in scene.items if item.owner == reference]
            graphic = next((item for item in own_items if item.kind == GeometryKind.POWER_GRAPHIC), None)
            if graphic is None:
                unplaced.append(reference)
                continue
            data = parse_schematic_file(schematic)
            symbol = _symbol_by_reference(data, reference)
            here = (float(symbol.get("x", 0.0)), float(symbol.get("y", 0.0)))
            plan = plan_flag_attachment(
                wires=tuple(segment_of[sid] for sid in sorted(own_ids)),
                prefer=here,
                text_width_mm=text_width_mm("PWR_FLAG", 1.27),
                foreign_bounds=tuple(
                    Bounds(*box) for ref, box in terminal_sheet(3.81).bounds_by_ref.items() if ref != reference
                ),
                foreign_pin_points=tuple(
                    (item.bounds.left, item.bounds.top) for item in scene.items
                    if item.kind == GeometryKind.PIN_ENDPOINT and item.id.endswith(":terminal")
                    and item.id[: -len(":terminal")] != own_pin
                ),
                other_wires=tuple(segment for sid, segment in segment_of.items() if sid not in own_ids),
                text_boxes=tuple(
                    item.bounds for item in routing_text_obstacles(scene, bool(layout_clearances["tight_terminals"])).items
                    if item.kind in TEXT_KINDS and item.owner != reference
                ),
                host_points=tuple(
                    (item.bounds.left, item.bounds.top) for item in scene.items
                    if item.kind == GeometryKind.PIN_ENDPOINT and item.id.endswith(":terminal")
                    and item.id[: -len(":terminal")] in hosts
                ),
            )
            if plan is None:
                unplaced.append(reference)
                continue

            schematic_tools._transactional_write_to_schematic(
                lambda content, reference=reference, plan=plan: attach_flag(
                    content, reference, plan.point, plan.rotation, plan.segment,
                )
            )
            attached.append({
                "reference": reference, "point": [round(plan.point[0], 4), round(plan.point[1], 4)],
                "rotation": plan.rotation,
            })
        return json.dumps({"attached": attached, "unplaced": unplaced}, sort_keys=True)

    allowed = getattr(server, "allowed_tool_names", None)
    if isinstance(allowed, set):
        allowed.add("sch_attach_power_flags")
        allowed.add("sch_symbol_geometry")
        allowed.add("sch_set_symbol_mirror")
        allowed.add("sch_place_fields")
        allowed.add("sch_move_relative")
        allowed.add("sch_move_block_relative")
        allowed.add("sch_move_block_to_layout_offset")
        allowed.add("sch_measure_functional_blocks")
        allowed.add("sch_translate_references_to_margin")
        allowed.add("sch_set_sheet_size")
        allowed.add("sch_add_pin_power_terminals")
        allowed.add("sch_route_pin_bus")
        allowed.add("sch_place_pin_at_offset")
        allowed.add("sch_add_pin_power_symbols")
        allowed.add("sch_place_pin_relation")
        allowed.add("sch_place_series_relation")
        allowed.add("sch_place_parallel_bank")
        allowed.add("sch_route_pin_rail")
        allowed.add("sch_place_node_cluster")
        allowed.add("sch_place_relation_band")
        allowed.add("sch_route_relation_connection")
        allowed.add("sch_set_layout_clearances")
        allowed.add("sch_add_pin_net_labels")
        allowed.add("sch_add_wire_labels")
        allowed.add("sch_add_text")
        allowed.add("sch_plan_relation_lanes")
        allowed.add("sch_align_relative_pins")
        allowed.add("sch_choose_orientations")
        allowed.add("sch_frame_functional_blocks")
        allowed.add("sch_mark_no_connect_pins")
        allowed.add("sch_modify_field_orientation")
        allowed.add("sch_move_unit")
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Pro with companion schematic extensions")
    parser.add_argument("--transport", default="streamable-http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3334)
    parser.add_argument("--profile", default="schematic_authoring")
    parser.add_argument("--mode", default="write")
    args = parser.parse_args()

    if args.transport != "streamable-http":
        raise SystemExit("Only streamable-http is supported by the companion wrapper.")

    os.environ["KICAD_MCP_OPERATING_MODE"] = args.mode
    server = build_extended_server(args.profile)

    import anyio

    async def serve() -> None:
        await server.run_streamable_http_async(host=args.host, port=args.port)

    anyio.run(serve)


if __name__ == "__main__":
    main()
