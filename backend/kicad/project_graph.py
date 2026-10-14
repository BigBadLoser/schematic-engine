from __future__ import annotations

import hashlib
import re
from typing import Any


def result_text(payload: dict[str, Any] | None) -> str:
    """Normalize textual MCP evidence while keeping provider details at the adapter edge."""
    if not payload:
        return ""
    result = payload.get("result")
    if isinstance(result, str):
        return result
    text = payload.get("text")
    if isinstance(text, str):
        return text
    content = payload.get("content")
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    return ""


def _structured_list(payload: dict[str, Any] | None, *keys: str) -> list[dict[str, Any]] | None:
    if not payload:
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    return None


def _diagnostic(source: str, candidate_count: int, parsed_count: int) -> dict[str, Any]:
    coverage = 1.0 if candidate_count == 0 else parsed_count / candidate_count
    return {
        "source": source,
        "candidate_items": candidate_count,
        "parsed_items": parsed_count,
        "coverage": round(coverage, 3),
        "warning": (
            None
            if coverage == 1.0
            else f"Parsed {parsed_count}/{candidate_count} {source} items; normalized graph may be incomplete."
        ),
    }


def _parse_symbols(payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    structured = _structured_list(payload, "symbols", "items")
    if structured is not None:
        nodes: list[dict[str, Any]] = []
        for item in structured:
            ref = item.get("ref") or item.get("reference")
            if not ref:
                continue
            value = str(item.get("value") or item.get("name") or "")
            library = item.get("library") or item.get("lib_id")
            x = item.get("x_mm", item.get("x"))
            y = item.get("y_mm", item.get("y"))
            region = (
                {"x_mm": float(x), "y_mm": float(y)}
                if isinstance(x, (int, float)) and isinstance(y, (int, float))
                else None
            )
            nodes.append(
                {
                    "id": f"component:{ref}",
                    "kind": "component",
                    "label": f"{ref} · {value}".rstrip(" ·"),
                    "ref": str(ref),
                    "document": "schematic",
                    "region": region,
                    "attributes": {
                        "value": value,
                        "library": library,
                        "rotation": item.get("rotation"),
                        "unit": item.get("unit"),
                        "footprint": item.get("footprint"),
                        "kiid": item.get("kiid") or item.get("uuid"),
                    },
                    "neighbors": [],
                }
            )
        return nodes, _diagnostic("structured symbols", len(structured), len(nodes))

    lines = [line for line in result_text(payload).splitlines() if line.startswith("- ")]
    nodes = []
    for line in lines:
        match = re.match(
            r"- (\S+) (.+) @ \(([^,]+), ([^)]+)\) rot=([^ ]+) unit=([^ ]+)(?: footprint=(.+))?",
            line,
        )
        if not match:
            continue
        ref, identity, x, y, rotation, unit, footprint = match.groups()
        value, _, library = identity.rpartition(" ")
        nodes.append(
            {
                "id": f"component:{ref}",
                "kind": "component",
                "label": f"{ref} · {value}",
                "ref": ref,
                "document": "schematic",
                "region": {"x_mm": float(x), "y_mm": float(y)},
                "attributes": {
                    "value": value,
                    "library": library,
                    "rotation": rotation,
                    "unit": unit,
                    "footprint": footprint or None,
                    "kiid": None,
                },
                "neighbors": [],
            }
        )
    return nodes, _diagnostic("text symbols", len(lines), len(nodes))


def _net_id(net_name: str, pins: str) -> str:
    normalized = net_name.strip()
    if normalized and normalized.casefold() not in {"(unnamed)", "unnamed", "<unnamed>"}:
        return f"net:{normalized}"
    digest = hashlib.sha1(pins.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"net:unnamed:{digest}"


def _parse_connectivity(
    payload: dict[str, Any] | None, component_refs: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    structured = _structured_list(payload, "nets", "groups")
    if structured is not None:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        parsed = 0
        for index, item in enumerate(structured):
            name = str(item.get("name") or item.get("net") or f"(unnamed {index + 1})")
            raw_pins = item.get("pins", [])
            if isinstance(raw_pins, list):
                pins = [str(pin) for pin in raw_pins]
            elif isinstance(raw_pins, str):
                pins = [part.strip() for part in raw_pins.split(",") if part.strip()]
            else:
                pins = []
            pins_text = ",".join(pins)
            node_id = _net_id(name, pins_text)
            nodes.append(
                {
                    "id": node_id,
                    "kind": "net",
                    "label": name,
                    "ref": name if not name.startswith("(unnamed") else None,
                    "document": "schematic",
                    "region": None,
                    "attributes": {
                        "pins": pins_text,
                        "point_count": item.get("point_count") or item.get("points"),
                    },
                    "neighbors": [],
                }
            )
            for pin in pins:
                ref = pin.split(":", 1)[0]
                if ref in component_refs:
                    edges.append({"kind": "connects", "from": node_id, "to": f"component:{ref}"})
            parsed += 1
        return nodes, edges, _diagnostic("structured nets", len(structured), parsed)

    lines = [line for line in result_text(payload).splitlines() if line.startswith("- Group ")]
    nodes = []
    edges = []
    for line in lines:
        match = re.match(r"- Group \d+: (.+) \| pins=(.+) \| points=(\d+)", line)
        if not match:
            continue
        net_name, pins, point_count = match.groups()
        node_id = _net_id(net_name, pins)
        nodes.append(
            {
                "id": node_id,
                "kind": "net",
                "label": net_name,
                "ref": net_name if "unnamed" not in net_name.casefold() else None,
                "document": "schematic",
                "region": None,
                "attributes": {"pins": pins, "point_count": int(point_count)},
                "neighbors": [],
            }
        )
        for pin in pins.split(","):
            ref = pin.strip().split(":", 1)[0]
            if ref in component_refs:
                edges.append({"kind": "connects", "from": node_id, "to": f"component:{ref}"})
    return nodes, edges, _diagnostic("text nets", len(lines), len(nodes))


def _parse_footprints(
    payload: dict[str, Any] | None, component_refs: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    structured = _structured_list(payload, "footprints", "items")
    if structured is not None:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        for item in structured:
            ref = item.get("ref") or item.get("reference")
            if not ref:
                continue
            value = str(item.get("value") or "")
            footprint_id = str(item.get("kiid") or item.get("uuid") or ref)
            x = item.get("x_mm", item.get("x"))
            y = item.get("y_mm", item.get("y"))
            region = (
                {"x_mm": float(x), "y_mm": float(y)}
                if isinstance(x, (int, float)) and isinstance(y, (int, float))
                else None
            )
            node_id = f"footprint:{footprint_id}"
            nodes.append(
                {
                    "id": node_id,
                    "kind": "footprint",
                    "label": f"{ref} · {value}".rstrip(" ·"),
                    "ref": str(ref),
                    "document": "board",
                    "region": region,
                    "attributes": {
                        "value": value,
                        "layer": item.get("layer"),
                        "kicad_id": footprint_id,
                    },
                    "neighbors": [],
                }
            )
            if ref in component_refs:
                edges.append({"kind": "implements", "from": f"component:{ref}", "to": node_id})
        return nodes, edges, _diagnostic("structured footprints", len(structured), len(nodes))

    lines = [line for line in result_text(payload).splitlines() if line.startswith("- ")]
    nodes = []
    edges = []
    for line in lines:
        match = re.match(r"- (.+) \((.+)\) @ \(([^,]+), ([^)]+)\) mm layer=(.+) id=(.+)", line)
        if not match:
            continue
        ref, value, x, y, layer, footprint_id = match.groups()
        stable_id = footprint_id.removesuffix("...")
        node_id = f"footprint:{stable_id}"
        nodes.append(
            {
                "id": node_id,
                "kind": "footprint",
                "label": f"{ref} · {value}",
                "ref": ref,
                "document": "board",
                "region": {"x_mm": float(x), "y_mm": float(y)},
                "attributes": {"value": value, "layer": layer, "kicad_id": footprint_id},
                "neighbors": [],
            }
        )
        if ref in component_refs:
            edges.append({"kind": "implements", "from": f"component:{ref}", "to": node_id})
    return nodes, edges, _diagnostic("text footprints", len(lines), len(nodes))


def build_project_graph(read: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded normalized graph and report evidence-parse coverage explicitly."""
    component_nodes, symbol_diag = _parse_symbols(read.get("symbols"))
    component_refs = {node["ref"] for node in component_nodes if node.get("ref")}
    net_nodes, connectivity_edges, net_diag = _parse_connectivity(
        read.get("connectivity"), component_refs
    )
    footprint_nodes, footprint_edges, footprint_diag = _parse_footprints(
        read.get("footprints"), component_refs
    )

    nodes = [*component_nodes, *net_nodes, *footprint_nodes]
    edges = [*connectivity_edges, *footprint_edges]
    neighbors: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        if edge["from"] in neighbors and edge["to"] in neighbors:
            neighbors[edge["from"]].append(edge["to"])
            neighbors[edge["to"]].append(edge["from"])
    for node in nodes:
        node["neighbors"] = neighbors[node["id"]]

    diagnostics = [symbol_diag, net_diag, footprint_diag]
    warnings = [diag["warning"] for diag in diagnostics if diag["warning"]]
    return {
        "schema_version": "1.0.0",
        "nodes": nodes,
        "edges": edges,
        "total_matches": len(nodes),
        "returned_items": len(nodes),
        "truncated": False,
        "parse_diagnostics": diagnostics,
        "warnings": warnings,
    }
