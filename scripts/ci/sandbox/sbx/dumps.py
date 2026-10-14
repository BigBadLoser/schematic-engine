"""Analyze libavoid dumps written by the e3_dump_libavoid overlay.

Questions answered (review findings B1-B3):
* do routes leave a portal back toward its pin (backtracking)?
* does libavoid's output start/end away from the portal (nudging displacement)?
* how many route vertices sit exactly on a half-grid tie, where independent
  per-vertex rounding is ambiguous?
* is any raw segment non-orthogonal before the engine snaps it?
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

OUTWARD = {"WEST": (-1, 0), "EAST": (1, 0), "NORTH": (0, -1), "SOUTH": (0, 1)}
TIE_TOLERANCE = 1e-6


def absolute(data: dict) -> dict:
    """A dump in the frame of the sheet.

    The engine poses a grouped routing problem to libavoid with a grid corner as the origin (``router_origin``) and records
    it so, while the endpoint table is in the sheet's frame: the offset is the difference between a portal in the table and
    the same portal's node in the payload. A dump from before that change has none and is returned as it is.
    """
    children = data["payload"]["graph"].get("children", [])
    nodes = {c["id"]: c for c in children}
    offsets = []
    for token, info in data.get("endpoints", {}).items():
        node = nodes.get(f"portal::{token}")
        where = info.get("portal") or info.get("point")
        if node is not None and where is not None:
            offsets.append((where[0] - (node["x"] + node["width"] / 2), where[1] - (node["y"] + node["height"] / 2)))
    if not offsets:
        return data
    dx = statistics.median(o[0] for o in offsets)
    dy = statistics.median(o[1] for o in offsets)
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return data
    moved = json.loads(json.dumps(data))
    for child in moved["payload"]["graph"].get("children", []):
        child["x"], child["y"] = round(child["x"] + dx, 6), round(child["y"] + dy, 6)
    for route in moved.get("raw_routes", {}).values():
        for key in ("sourcePoint", "targetPoint"):
            if route.get(key):
                route[key] = {"x": route[key]["x"] + dx, "y": route[key]["y"] + dy}
        route["bendPoints"] = [{"x": p["x"] + dx, "y": p["y"] + dy} for p in route.get("bendPoints", [])]
    return moved


def _points(route: dict) -> list[tuple[float, float]]:
    raw = [route["sourcePoint"], *route.get("bendPoints", []), route["targetPoint"]]
    return [(float(p["x"]), float(p["y"])) for p in raw]


def _sign(value: float) -> int:
    return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)


def _direction(a: tuple[float, float], b: tuple[float, float]) -> tuple[int, int]:
    return (_sign(b[0] - a[0]), _sign(b[1] - a[1]))


def _rect(child: dict) -> tuple[float, float, float, float]:
    return (child["x"], child["y"], child["x"] + child["width"], child["y"] + child["height"])


def _inside(point: tuple[float, float], rect: tuple[float, float, float, float], margin: float = 0.0) -> bool:
    """Strictly inside `rect` grown by `margin` (a point on the boundary is not inside)."""
    left, top, right, bottom = rect
    return (left - margin + 1e-6 < point[0] < right + margin - 1e-6
            and top - margin + 1e-6 < point[1] < bottom + margin - 1e-6)


def _crosses_interior(a: tuple[float, float], b: tuple[float, float], rect: tuple[float, float, float, float]) -> bool:
    """Does the segment a-b pass through the open interior of `rect` (Liang-Barsky, strict)?"""
    left, top, right, bottom = rect
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p_, q_ in ((-dx, a[0] - left), (dx, right - a[0]), (-dy, a[1] - top), (dy, bottom - a[1])):
        if abs(p_) < 1e-12:
            if q_ <= 1e-6:
                return False
            continue
        t = q_ / p_
        if p_ < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 >= t1 - 1e-9:
            return False
    return t1 - t0 > 1e-9


def analyze_dump(data: dict, grid: float) -> list[dict]:
    endpoints = data["endpoints"]
    payload = data["payload"]
    buffer_mm = float(payload.get("options", {}).get("shapeBufferDistance", 0.0))
    children = payload["graph"].get("children", [])
    obstacles = [_rect(c) for c in children if str(c.get("id", "")).startswith("obstacle::")]
    foreign_wires = [_rect(c) for c in children if str(c.get("id", "")).startswith("foreign_wire::")]
    portals = {
        c["id"].removeprefix("portal::"): ((c["x"] + c["width"] / 2), (c["y"] + c["height"] / 2))
        for c in children if str(c.get("id", "")).startswith("portal::")
    }
    rows = []
    for edge in payload["graph"]["edges"]:
        route = data["raw_routes"].get(edge["id"])
        if route is None:
            continue
        source = endpoints.get(edge["source"].removeprefix("portal::"))
        target = endpoints.get(edge["target"].removeprefix("portal::"))
        points = _points(route)
        row = {
            "edge": edge["id"],
            "bends": max(0, len(points) - 2),
            "nonorthogonal_segments": sum(
                abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6 for a, b in zip(points, points[1:])
            ),
            "half_grid_vertices": sum(
                abs(abs(c / grid - round(c / grid)) - 0.5) < TIE_TOLERANCE for point in points for c in point
            ),
            "vertex_coordinates": 2 * len(points),
            "obstacle_crossings": sum(
                _crosses_interior(a, b, rect) for a, b in zip(points, points[1:]) for rect in obstacles
            ),
            "foreign_wire_crossings": sum(
                _crosses_interior(a, b, rect) for a, b in zip(points, points[1:]) for rect in foreign_wires
            ),
        }
        for role, token in (("source", edge["source"].removeprefix("portal::")), ("target", edge["target"].removeprefix("portal::"))):
            portal = portals.get(token)
            if portal is not None:
                row[f"{role}_trapped"] = any(_inside(portal, rect, buffer_mm) for rect in obstacles)
        if source and source.get("portal"):
            row["source_displacement_mm"] = round(math.dist(points[0], source["portal"]), 4)
            outward = OUTWARD.get(source.get("side") or "")
            if outward and len(points) > 1:
                first = _direction(points[0], points[1])
                row["source_backtracks"] = first[0] * outward[0] + first[1] * outward[1] < 0
        if target and target.get("portal"):
            row["target_displacement_mm"] = round(math.dist(points[-1], target["portal"]), 4)
            outward = OUTWARD.get(target.get("side") or "")
            if outward and len(points) > 1:
                arriving = _direction(points[-2], points[-1])
                row["target_backtracks"] = arriving[0] * outward[0] + arriving[1] * outward[1] > 0
        rows.append(row)
    return rows


def analyze(dump_dir: Path, grid: float = 1.27) -> dict:
    rows: list[dict] = []
    files = sorted(dump_dir.glob("libavoid-*.json"))
    for path in files:
        rows.extend(analyze_dump(absolute(json.loads(path.read_text(encoding="utf-8"))), grid))
    displacement = [r[k] for r in rows for k in ("source_displacement_mm", "target_displacement_mm") if k in r]
    backtracks = sum(bool(r.get("source_backtracks")) + bool(r.get("target_backtracks")) for r in rows)
    ends = sum(("source_backtracks" in r) + ("target_backtracks" in r) for r in rows)
    ties = sum(r["half_grid_vertices"] for r in rows)
    coordinates = sum(r["vertex_coordinates"] for r in rows)
    summary = {
        "dump_files": len(files),
        "routes": len(rows),
        "route_ends_with_known_side": ends,
        "backtracking_ends": backtracks,
        "nonorthogonal_raw_segments": sum(r["nonorthogonal_segments"] for r in rows),
        "half_grid_tie_coordinates": ties,
        "route_coordinates": coordinates,
        "displacement_mm": {
            "median": round(statistics.median(displacement), 4) if displacement else None,
            "max": round(max(displacement), 4) if displacement else None,
            "over_0.05": sum(value > 0.05 for value in displacement),
        },
        "bends_total": sum(r["bends"] for r in rows),
        "trapped_portal_ends": sum(bool(r.get("source_trapped")) + bool(r.get("target_trapped")) for r in rows),
        "routes_through_obstacles": sum(1 for r in rows if r["obstacle_crossings"]),
        "routes_through_foreign_wires": sum(1 for r in rows if r["foreign_wire_crossings"]),
    }
    return {"summary": summary, "worst_displacement": sorted(
        (r for r in rows if "source_displacement_mm" in r or "target_displacement_mm" in r),
        key=lambda r: -max(r.get("source_displacement_mm", 0), r.get("target_displacement_mm", 0)),
    )[:5]}


def terminals(run_dir: Path) -> dict[str, list[dict]]:
    """Power-terminal decisions (e5_trace overlay), grouped by the fixture that produced them.

    All fixtures of a run share one MCP server, so dump files carry no fixture name; each file is
    assigned by time: a fixture's window ends when its log was last written.
    """
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    order = [name for name in manifest.get("fixtures", [])]
    ends: list[tuple[str, float]] = []
    for name in order:
        log = run_dir / "logs" / f"{name}.log"
        if log.is_file():
            ends.append((name, log.stat().st_mtime))
    grouped: dict[str, list[dict]] = {name: [] for name, _ in ends}
    for path in sorted((run_dir / "dumps").glob("power-terminals-*.json")):
        mtime = path.stat().st_mtime
        owner = next((name for name, end in ends if mtime <= end + 0.5), ends[-1][0] if ends else None)
        if owner is None:
            continue
        for item in json.loads(path.read_text(encoding="utf-8")).get("terminals", []):
            segments = item.get("segments") or []
            length = sum(abs(s[0] - s[2]) + abs(s[1] - s[3]) for s in segments)
            grouped[owner].append({
                "pin": f"{item.get('reference')}.{item.get('pin')}",
                "net": item.get("net"),
                "strategy": item.get("strategy"),
                "fallback": bool(item.get("fallback")),
                "length_mm": round(length, 3),
                "pin_position": item.get("pin_position"),
                "terminal_position": item.get("terminal_position"),
            })
    return grouped


def format_terminals(grouped: dict[str, list[dict]]) -> str:
    lines: list[str] = []
    for fixture, items in grouped.items():
        if not items:
            continue
        fallbacks = sum(1 for item in items if item["fallback"])
        mean = sum(item["length_mm"] for item in items) / len(items)
        lines.append(f"### {fixture}: {len(items)} power terminals, {fallbacks} on the unchecked fallback, "
                     f"mean stub {mean:.2f} mm, longest {max(item['length_mm'] for item in items):.2f} mm")
        lines.append("| pin | net | strategy | stub mm | unchecked |")
        lines.append("|---|---|---|---|---|")
        for item in items:
            lines.append(f"| {item['pin']} | {item['net']} | {item['strategy']} | {item['length_mm']} | "
                         f"{'YES' if item['fallback'] else ''} |")
        lines.append("")
    return "\n".join(lines) if lines else "no power-terminal dumps (run with --overlay e5_trace)"
