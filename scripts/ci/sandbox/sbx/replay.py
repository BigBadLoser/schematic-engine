"""Router-only replay of recorded libavoid calls under router variants (review experiments 2 and 3).

A dump (the e3_dump_libavoid overlay) holds the exact payload the engine sent to libavoid for one
grouped routing call, libavoid's raw answer, and the endpoint table. Replaying keeps the placement
and the earlier nets' wires frozen and changes only how the router is fed, then applies the
engine's own post-processing (grid snap, endpoint overwrite, pin stubs) and its acceptance checks
(orthogonality, symbol bodies, pre-existing conductors). The result answers, per call and variant:
would the engine accept this route, and how long and how bent is it?

Replicated from the engine: the grid snap, the endpoint overwrite and pin stubs, the direct straight
route for an axis-aligned pin-to-junction pair, simplify_route (with a safety predicate over symbol
bodies and pre-existing wires), and the orthogonality, body and conductor checks.

Not replayed: text-reservation and foreign-pin checks (they need data the dump does not hold), the
retry and fallback ladder, and the same-batch other-net check. The lab can therefore only be more
permissive than the engine, never stricter. Because each call is frozen, anything that depends on
how earlier nets were routed (order sensitivity) is outside this lab.
"""
from __future__ import annotations

import copy
import json
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import env
from .dumps import absolute

GRID = 1.27                 # SCHEMATIC_GRID_MM
BODY_INFLATION = 0.635      # obstacle clearance added to the tight bounds (mcp_extended_server.py:2505)
WIRE_CLEARANCE = 0.508      # half-width of a pre-existing wire's box (2549)
EPS = 1e-5
OUTWARD = {"WEST": (-1.0, 0.0), "EAST": (1.0, 0.0), "NORTH": (0.0, -1.0), "SOUTH": (0.0, 1.0)}
PROBES_DIR = env.SANDBOX_DIR / "probes"
PENALTIES = ("crossingPenalty", "fixedSharedPathPenalty", "portDirectionPenalty", "reverseDirectionPenalty")

Point = tuple[float, float]
Rect = tuple[float, float, float, float]


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Variant:
    name: str
    note: str
    options: dict[str, Any] = field(default_factory=dict)
    inflation: float | None = None       # replaces the engine's 0.635 obstacle inflation
    portal_offset: float | None = None   # replaces the 2.54 mm portal distance from the pin tip
    drop_text: bool = False              # remove field / pin-number text keep-outs
    ports: bool = False                  # symbol node (body + pins) with one real port per pin
    buffer_into_shapes: bool = False     # shapeBufferDistance -> 0, the same clearance baked into obstacle sizes
    legal_crossings: bool = False        # drop the foreign-wire boxes; accept proper crossings, reject touches/overlaps


_PLAIN: dict[str, Any] = {"nudgeOrthogonalSegmentsConnectedToShapes": False, **{key: 0 for key in PENALTIES}}

VARIANTS: dict[str, Variant] = {v.name: v for v in (
    Variant("asis", "exactly what the engine sent (control: must reproduce the recorded routes)"),
    Variant("nudge_off", "nudgeOrthogonalSegmentsConnectedToShapes=false",
            {"nudgeOrthogonalSegmentsConnectedToShapes": False}),
    Variant("penalties_off", "the four experimental penalties set to 0", {key: 0 for key in PENALTIES}),
    Variant("plain", "nudge off and penalties off", dict(_PLAIN)),
    Variant("no_text", "text keep-outs (fields, pin numbers) removed from the obstacles", drop_text=True),
    Variant("inflate0", "bodies not inflated by 0.635 (clearance = shapeBufferDistance only)", inflation=0.0),
    Variant("portal1.27", "portal 1.27 mm from the pin tip instead of 2.54", portal_offset=1.27),
    Variant("buf0", "libavoid's global buffer set to 0 and the same clearance baked into every real obstacle, "
                    "so the 0.02 mm portal/junction shapes stop blocking each other", buffer_into_shapes=True),
    Variant("buf0_plain", "buf0, nudge off, penalties off", dict(_PLAIN), buffer_into_shapes=True),
    Variant("cross_ok", "foreign wires are not obstacles and proper crossings are accepted (KiCad: a crossing "
                        "without a junction is no connection); touches and overlaps are still rejected",
            legal_crossings=True),
    Variant("cross_ok_buf0", "cross_ok plus buf0", buffer_into_shapes=True, legal_crossings=True),
    Variant("ports", "pins as real ports on a symbol node (body + pins), no portals", ports=True),
    Variant("ports_plain", "ports, nudge off, penalties off", dict(_PLAIN), ports=True),
    Variant("ports_plain_notext", "ports, plain options, no text keep-outs", dict(_PLAIN), drop_text=True, ports=True),
    Variant("ports_buf0", "ports with the buffer baked into the symbol nodes", ports=True, buffer_into_shapes=True),
    Variant("ports_buf0_plain", "ports_buf0, nudge off, penalties off", dict(_PLAIN), ports=True, buffer_into_shapes=True),
)}


# ----------------------------------------------------------------------------- geometry helpers

def _rect(node: dict) -> Rect:
    return (node["x"], node["y"], node["x"] + node["width"], node["y"] + node["height"])


def _set_rect(node: dict, rect: Rect) -> None:
    node["x"], node["y"] = rect[0], rect[1]
    node["width"], node["height"] = max(0.01, rect[2] - rect[0]), max(0.01, rect[3] - rect[1])


def _snap(value: float) -> float:
    return round(value / GRID) * GRID


def compact_points(points: list[Point]) -> list[Point]:
    """The engine's compact_points: drop repeats, then collinear middle points (server:2674)."""
    compact: list[Point] = []
    for point in points:
        if compact and abs(compact[-1][0] - point[0]) < 1e-9 and abs(compact[-1][1] - point[1]) < 1e-9:
            continue
        compact.append(point)
    changed = True
    while changed and len(compact) >= 3:
        changed = False
        reduced = [compact[0]]
        for index in range(1, len(compact) - 1):
            prior, current, following = reduced[-1], compact[index], compact[index + 1]
            if (abs(prior[0] - current[0]) < 1e-9 and abs(current[0] - following[0]) < 1e-9) or (
                abs(prior[1] - current[1]) < 1e-9 and abs(current[1] - following[1]) < 1e-9
            ):
                changed = True
                continue
            reduced.append(current)
        reduced.append(compact[-1])
        compact = reduced
    return compact


def segment_hits_bounds(first: Point, second: Point, bounds: Rect) -> bool:
    """The engine's _segment_hits_bounds with padding 0 (server:964); a diagonal always 'hits'."""
    (x1, y1), (x2, y2) = first, second
    left, top, right, bottom = bounds
    if abs(y1 - y2) < 1e-9:
        low, high = sorted((x1, x2))
        return top < y1 < bottom and high > left and low < right
    if abs(x1 - x2) < 1e-9:
        low, high = sorted((y1, y2))
        return left < x1 < right and high > top and low < bottom
    return True


def segments_intersect(a: tuple[Point, Point], b: tuple[Point, Point]) -> bool:
    """Closed axis-aligned segment intersection, T and collinear included (route_simplification.py:46)."""
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    if abs(x1 - x2) < EPS and abs(x3 - x4) < EPS:
        return abs(x1 - x3) < EPS and max(min(y1, y2), min(y3, y4)) <= min(max(y1, y2), max(y3, y4)) + EPS
    if abs(y1 - y2) < EPS and abs(y3 - y4) < EPS:
        return abs(y1 - y3) < EPS and max(min(x1, x2), min(x3, x4)) <= min(max(x1, x2), max(x3, x4)) + EPS
    if abs(x1 - x2) < EPS:
        return min(x3, x4) - EPS <= x1 <= max(x3, x4) + EPS and min(y1, y2) - EPS <= y3 <= max(y1, y2) + EPS
    return min(x1, x2) - EPS <= x3 <= max(x1, x2) + EPS and min(y3, y4) - EPS <= y1 <= max(y3, y4) + EPS


def connects_or_overlaps(a: tuple[Point, Point], b: tuple[Point, Point]) -> bool:
    """Would KiCad join or merge these two orthogonal segments? A proper crossing (both interiors) does not."""
    if not segments_intersect(a, b):
        return False
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    a_vertical, b_vertical = abs(x1 - x2) < EPS, abs(x3 - x4) < EPS
    if a_vertical == b_vertical:
        return True                       # collinear overlap or end-to-end contact
    horizontal, vertical = (b, a) if a_vertical else (a, b)
    (hx1, hy), (hx2, _) = horizontal
    (vx, vy1), (_, vy2) = vertical
    at_horizontal_end = min(abs(vx - hx1), abs(vx - hx2)) < EPS
    at_vertical_end = min(abs(hy - vy1), abs(hy - vy2)) < EPS
    return at_horizontal_end or at_vertical_end


def bodies_of(data: dict) -> dict[str, Rect]:
    """The engine's symbol_bounds: each obstacle rectangle shrunk back by the 0.635 inflation."""
    bodies: dict[str, Rect] = {}
    for node in data["payload"]["graph"]["children"]:
        if str(node["id"]).startswith("obstacle::"):
            left, top, right, bottom = _rect(node)
            bodies[node["id"].removeprefix("obstacle::")] = (
                left + BODY_INFLATION, top + BODY_INFLATION, right - BODY_INFLATION, bottom - BODY_INFLATION,
            )
    return bodies


def wires_of(data: dict) -> list[tuple[Point, Point]]:
    """The pre-existing conductors, recovered from their router boxes (server:2549-2557)."""
    wires = []
    for node in data["payload"]["graph"]["children"]:
        if str(node["id"]).startswith("foreign_wire::"):
            left, top, right, bottom = _rect(node)
            wires.append(((left + WIRE_CLEARANCE, top + WIRE_CLEARANCE), (right - WIRE_CLEARANCE, bottom - WIRE_CLEARANCE)))
    return wires


# ----------------------------------------------------------------------------- building jobs

@dataclass
class Prepared:
    key: str
    graph: dict
    options: dict
    attach: dict[str, Point]     # endpoint token -> where the engine pins the route's first/last point
    portal_mode: bool            # True: the engine adds a stub from the pin tip to the portal
    legal_crossings: bool = False


def _tokens(edge: dict) -> tuple[str, str]:
    return edge["source"].removeprefix("portal::"), edge["target"].removeprefix("portal::")


def _bake_buffer(variant: Variant, options: dict, children: list[dict], *, symbol_ports: bool) -> None:
    """Replace libavoid's global shape buffer by the same clearance written into every real obstacle.

    libavoid applies shapeBufferDistance to *every* shape, including the 0.02 mm portal and junction
    nodes, so two portals closer than twice the buffer block each other's straight connection.
    """
    if not variant.buffer_into_shapes:
        return
    buffer_mm = float(options.get("shapeBufferDistance", 0.0))
    options["shapeBufferDistance"] = 0.0
    for node in children:
        node_id = str(node["id"])
        if node_id.startswith("portal::"):
            continue
        if symbol_ports and node_id.startswith("symbol::"):
            # keep the pin tips on the node boundary: ports need the node edge at the tip
            continue
        left, top, right, bottom = _rect(node)
        _set_rect(node, (left - buffer_mm, top - buffer_mm, right + buffer_mm, bottom + buffer_mm))


def prepare(data: dict, variant: Variant, key: str) -> Prepared:
    payload = copy.deepcopy(data["payload"])
    graph, options = payload["graph"], payload.setdefault("options", {})
    options.update(variant.options)
    endpoints = data["endpoints"]
    children: list[dict] = graph["children"]
    if variant.drop_text:
        children[:] = [c for c in children if not str(c["id"]).startswith("keepout::")]
    if variant.legal_crossings:
        children[:] = [c for c in children if not str(c["id"]).startswith("foreign_wire::")]

    attach: dict[str, Point] = {}
    for token, info in endpoints.items():
        tip = tuple(info["point"])
        if info.get("synthetic") or not info.get("portal"):
            attach[token] = tip
            continue
        attach[token] = tuple(info["portal"])

    if variant.ports:
        by_ref: dict[str, list[str]] = {}
        for token, info in endpoints.items():
            if not info.get("synthetic") and info.get("reference") and info.get("portal"):
                by_ref.setdefault(info["reference"], []).append(token)
        bodies = bodies_of(data)
        port_tokens = {token for tokens in by_ref.values() for token in tokens}
        rebuilt: list[dict] = []
        for node in children:
            node_id = str(node["id"])
            if node_id.startswith("portal::") and node_id.removeprefix("portal::") in port_tokens:
                continue
            ref = node_id.removeprefix("obstacle::") if node_id.startswith("obstacle::") else None
            if ref in by_ref and ref in bodies:
                left, top, right, bottom = bodies[ref]
                symbol = {"id": f"symbol::{ref}", "x": left, "y": top,
                          "width": max(0.01, right - left), "height": max(0.01, bottom - top), "ports": []}
                for token in by_ref[ref]:
                    info = endpoints[token]
                    tip = info["point"]
                    symbol["ports"].append({
                        "id": f"port::{token}", "x": tip[0] - left, "y": tip[1] - top, "width": 0.0, "height": 0.0,
                        "properties": {"elk.port.side": info["side"]},
                    })
                    attach[token] = (float(tip[0]), float(tip[1]))
                rebuilt.append(symbol)
            else:
                rebuilt.append(node)
        children[:] = rebuilt
        for edge in graph["edges"]:
            for role, token in zip(("source", "target"), _tokens(edge)):
                info = endpoints[token]
                if token in port_tokens:
                    edge[role] = f"symbol::{info['reference']}"
                    edge[f"{role}Port"] = f"port::{token}"
        _bake_buffer(variant, options, children, symbol_ports=True)
        return Prepared(key, graph, options, attach, portal_mode=False, legal_crossings=variant.legal_crossings)

    if variant.inflation is not None:
        for node in children:
            if str(node["id"]).startswith("obstacle::"):
                left, top, right, bottom = _rect(node)
                grow = variant.inflation - BODY_INFLATION
                _set_rect(node, (left - grow, top - grow, right + grow, bottom + grow))
    if variant.portal_offset is not None:
        for token, info in endpoints.items():
            direction = OUTWARD.get(info.get("side") or "")
            if info.get("synthetic") or not info.get("portal") or direction is None:
                continue
            portal = (info["point"][0] + direction[0] * variant.portal_offset,
                      info["point"][1] + direction[1] * variant.portal_offset)
            attach[token] = portal
            for node in children:
                if node["id"] == f"portal::{token}":
                    node["x"], node["y"] = portal[0] - node["width"] / 2, portal[1] - node["height"] / 2
    _bake_buffer(variant, options, children, symbol_ports=False)
    return Prepared(key, graph, options, attach, portal_mode=True, legal_crossings=variant.legal_crossings)


# ----------------------------------------------------------------------------- running the router

def run_router(jobs: list[Prepared], *, batch: int = 120, timeout: float = 900.0) -> dict[str, dict]:
    if not (env.NODE_MODULES / "@mr_mint" / "elkjs-libavoid").is_dir():
        raise ReplayError("the router is not installed; run 'sandbox.py bootstrap --yes' first")
    lab = env.WORK_DIR / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROBES_DIR / "batch_route.mjs", lab / "batch_route.mjs")
    results: dict[str, dict] = {}
    for start in range(0, len(jobs), batch):
        chunk = jobs[start:start + batch]
        stdin = json.dumps([{"key": j.key, "graph": j.graph, "options": j.options} for j in chunk])
        done = subprocess.run(["node", str(lab / "batch_route.mjs")], input=stdin, capture_output=True,
                              text=True, cwd=lab, timeout=timeout, check=False)
        if done.returncode != 0:
            raise ReplayError(f"node failed: {(done.stderr or done.stdout).strip()[-800:]}")
        results.update(json.loads(done.stdout))
    return results


# ----------------------------------------------------------------------------- the engine's verdict

def _segments_of(points: list[Point]) -> list[tuple[Point, Point]]:
    return list(zip(points, points[1:]))


def _clear_of_bodies(points: list[Point], refs: tuple[str | None, str | None], bodies: dict[str, Rect]) -> bool:
    segments = _segments_of(points)
    for index, (a, b) in enumerate(segments):
        for ref, bounds in bodies.items():
            own = (index == 0 and refs[0] is not None and ref == refs[0]) or (
                index == len(segments) - 1 and refs[1] is not None and ref == refs[1])
            if not own and segment_hits_bounds(a, b, bounds):
                return False
    return True


def _simplify(points: list[Point], refs: tuple[str | None, str | None], bodies: dict[str, Rect],
              wires: list[tuple[Point, Point]], conflicts) -> list[Point]:
    """The engine's simplify_route with the part of its safety predicate the dump can support."""
    if str(env.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(env.REPO_ROOT))
    from backend.schematic.route_simplification import simplify_route

    def route_is_safe(candidate: tuple[Point, ...]) -> bool:
        path = list(candidate)
        return _clear_of_bodies(path, refs, bodies) and not any(
            conflicts(segment, wire) for segment in _segments_of(path) for wire in wires)

    return list(simplify_route(points, route_is_safe=route_is_safe, grid_mm=GRID))


def engine_verdict(
    raw_points: list[Point], start: Point, end: Point, tips: tuple[Point, Point], refs: tuple[str | None, str | None],
    bodies: dict[str, Rect], wires: list[tuple[Point, Point]], *, portal_mode: bool, protect_wires: bool,
    legal_crossings: bool = False, synthetic: tuple[bool, bool] = (False, False), simplify: bool = True,
) -> tuple[str, list[Point]]:
    """Re-apply the engine's post-processing and acceptance checks to one raw route (see the module doc)."""
    conflicts = connects_or_overlaps if legal_crossings else segments_intersect
    direct = False
    if synthetic[0] != synthetic[1]:
        # A pin and a junction that are axis-aligned and clear are joined by a straight wire, not libavoid's route.
        axis_aligned = abs(tips[0][0] - tips[1][0]) < 1e-6 or abs(tips[0][1] - tips[1][1]) < 1e-6
        others = {ref: rect for ref, rect in bodies.items() if ref not in refs}
        direct = axis_aligned and not any(segment_hits_bounds(tips[0], tips[1], rect) for rect in others.values())
    if direct:
        points = compact_points([tips[0], tips[1]])
    else:
        points = [(_snap(x), _snap(y)) for x, y in raw_points]
        points[0], points[-1] = start, end
        points = compact_points(points)
        if portal_mode:
            points = compact_points([tips[0], *points, tips[1]])
        if simplify and len(points) > 3:
            points = _simplify(points, refs, bodies, wires, conflicts)
    for a, b in _segments_of(points):
        if not (abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6):
            return "nonorthogonal", points
    if not _clear_of_bodies(points, refs, bodies):
        return "body", points
    if protect_wires:
        for a, b in _segments_of(points):
            if any(conflicts((a, b), wire) for wire in wires):
                return "foreign_wire", points
    return "ok", points


def _length(points: list[Point]) -> float:
    return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(points, points[1:]))


def _ties(raw_points: list[Point]) -> int:
    return sum(abs(abs(c / GRID - round(c / GRID)) - 0.5) < 1e-6 for point in raw_points for c in point)


def evaluate(data: dict, prepared: Prepared, result: dict) -> list[dict]:
    rows: list[dict] = []
    bodies, wires = bodies_of(data), wires_of(data)
    endpoints = data["endpoints"]
    for edge in data["payload"]["graph"]["edges"]:
        source_token, target_token = _tokens(edge)
        row: dict[str, Any] = {"edge": edge["id"]}
        route = result.get("routes", {}).get(edge["id"]) if result.get("ok") else None
        if route is None:
            row.update(verdict="no_route", reason=result.get("error", "no route returned") if not result.get("ok") else "no route returned")
            rows.append(row)
            continue
        raw = [route["sourcePoint"], *route.get("bendPoints", []), route["targetPoint"]]
        raw_points = [(float(p["x"]), float(p["y"])) for p in raw]
        source, target = endpoints[source_token], endpoints[target_token]
        if not prepared.portal_mode:
            # With real ports a route that does not start on its pin means libavoid found no path and
            # fell back to a straight line between the node centres.
            missed = [
                token for token, got in ((source_token, raw_points[0]), (target_token, raw_points[-1]))
                if not endpoints[token].get("synthetic") and endpoints[token].get("portal")
                and abs(got[0] - prepared.attach[token][0]) + abs(got[1] - prepared.attach[token][1]) > 0.05
            ]
            if missed:
                row.update(verdict="no_route", reason=f"route does not reach pin(s) {', '.join(missed)}")
                rows.append(row)
                continue
        verdict, points = engine_verdict(
            raw_points, prepared.attach[source_token], prepared.attach[target_token],
            (tuple(source["point"]), tuple(target["point"])),
            (source.get("reference") if not source.get("synthetic") else None,
             target.get("reference") if not target.get("synthetic") else None),
            bodies, wires, portal_mode=prepared.portal_mode, protect_wires=bool(data.get("protect_wires")),
            legal_crossings=prepared.legal_crossings,
            synthetic=(bool(source.get("synthetic")), bool(target.get("synthetic"))),
        )
        row.update(verdict=verdict, length_mm=round(_length(points), 3), bends=max(0, len(points) - 2),
                   tie_coordinates=_ties(raw_points))
        backtracks = 0
        for info, first, second in ((source, raw_points[0], raw_points[1]), (target, raw_points[-1], raw_points[-2])):
            direction = OUTWARD.get(info.get("side") or "")
            if direction and not info.get("synthetic"):
                step = (second[0] - first[0], second[1] - first[1])
                backtracks += (step[0] * direction[0] + step[1] * direction[1]) < -1e-9
        row["backtracking_ends"] = backtracks
        recorded = data["raw_routes"].get(edge["id"])
        if recorded is not None:
            old = [recorded["sourcePoint"], *recorded.get("bendPoints", []), recorded["targetPoint"]]
            row["same_as_recorded"] = len(old) == len(raw) and all(
                abs(float(a["x"]) - float(b["x"])) < 1e-6 and abs(float(a["y"]) - float(b["y"])) < 1e-6
                for a, b in zip(old, raw))
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- running a whole replay

def load_dumps(directory: Path) -> list[tuple[str, dict]]:
    files = sorted(directory.glob("libavoid-*.json"))
    if not files:
        raise ReplayError(f"no libavoid-*.json dumps in {directory}; run with the e3_dump_libavoid overlay")
    return [(path.stem, absolute(json.loads(path.read_text(encoding="utf-8")))) for path in files]


def replay(directory: Path, variant_names: list[str]) -> dict[str, Any]:
    unknown = [name for name in variant_names if name not in VARIANTS]
    if unknown:
        raise ReplayError(f"unknown variants {unknown}; choose from {', '.join(VARIANTS)}")
    dumps = load_dumps(directory)
    jobs: list[Prepared] = []
    index: dict[str, tuple[str, str]] = {}
    for stem, data in dumps:
        for name in variant_names:
            key = f"{stem}|{name}"
            jobs.append(prepare(data, VARIANTS[name], key))
            index[key] = (stem, name)
    results = run_router(jobs)
    by_variant: dict[str, dict[str, list[dict]]] = {name: {} for name in variant_names}
    lookup = dict(dumps)
    for job in jobs:
        stem, name = index[job.key]
        by_variant[name][stem] = evaluate(lookup[stem], job, results[job.key])
    return {"calls": len(dumps), "variants": {name: summarize(rows) for name, rows in by_variant.items()},
            "rows": by_variant}


def load_accepted(directory: Path, stem: str) -> dict[str, list[Point]] | None:
    """The routes the engine settled on for one recorded call (e3_dump_libavoid's <call>.accepted.jsonl)."""
    path = directory / f"{stem}.accepted.jsonl"
    if not path.is_file():
        return None
    accepted: dict[str, list[Point]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            accepted[record["edge"]] = [(float(x), float(y)) for x, y in record["points"]]
    return accepted


def calibrate(directory: Path) -> dict[str, Any]:
    """Score the lab's 'asis' verdict against what the engine actually did, edge by edge.

    The engine processes a call's edges in order and stops at the first one it rejects, so an edge
    with a recorded route was accepted, the first edge without one is where the call failed (if it
    did), and later edges were never reached. The router answer is the recorded raw route, so no
    router run is needed.
    """
    counts = {"agree_accepted": 0, "agree_rejected": 0, "lab_stricter": 0, "lab_more_permissive": 0,
              "same_points": 0, "different_points": 0, "calls": 0, "calls_without_record": 0}
    disagreements: list[dict[str, Any]] = []
    for stem, data in load_dumps(directory):
        accepted = load_accepted(directory, stem)
        if accepted is None:
            counts["calls_without_record"] += 1
            continue
        counts["calls"] += 1
        prepared = prepare(data, VARIANTS["asis"], stem)
        rows = evaluate(data, prepared, {"ok": True, "routes": data["raw_routes"]})
        failed_seen = False
        for edge, row in zip(data["payload"]["graph"]["edges"], rows):
            lab_ok = row["verdict"] == "ok"
            if edge["id"] in accepted:
                if lab_ok:
                    counts["agree_accepted"] += 1
                    engine_points = accepted[edge["id"]]
                    lab_points = _lab_points(data, prepared, edge)
                    counts["same_points" if lab_points == engine_points else "different_points"] += 1
                else:
                    counts["lab_stricter"] += 1
                    disagreements.append({"call": stem, "edge": edge["id"], "kind": "lab_stricter",
                                          "lab": row["verdict"]})
            elif not failed_seen:
                failed_seen = True            # the edge the engine rejected (or the call's last)
                if lab_ok:
                    counts["lab_more_permissive"] += 1
                    disagreements.append({"call": stem, "edge": edge["id"], "kind": "lab_more_permissive"})
                else:
                    counts["agree_rejected"] += 1
    return {"counts": counts, "disagreements": disagreements}


def _lab_points(data: dict, prepared: Prepared, edge: dict) -> list[Point]:
    """The final polyline the lab computes for an edge from the recorded raw route (verdict ignored)."""
    source_token, target_token = _tokens(edge)
    endpoints = data["endpoints"]
    source, target = endpoints[source_token], endpoints[target_token]
    route = data["raw_routes"][edge["id"]]
    raw = [(float(p["x"]), float(p["y"])) for p in [route["sourcePoint"], *route.get("bendPoints", []), route["targetPoint"]]]
    _verdict, points = engine_verdict(
        raw, prepared.attach[source_token], prepared.attach[target_token],
        (tuple(source["point"]), tuple(target["point"])),
        (source.get("reference") if not source.get("synthetic") else None,
         target.get("reference") if not target.get("synthetic") else None),
        bodies_of(data), wires_of(data), portal_mode=prepared.portal_mode,
        protect_wires=bool(data.get("protect_wires")), legal_crossings=prepared.legal_crossings,
        synthetic=(bool(source.get("synthetic")), bool(target.get("synthetic"))),
    )
    return [(round(x, 4), round(y, 4)) for x, y in points]


def format_calibration(result: dict[str, Any]) -> str:
    c = result["counts"]
    judged = c["agree_accepted"] + c["agree_rejected"] + c["lab_stricter"] + c["lab_more_permissive"]
    lines = [f"{c['calls']} recorded calls with engine records ({c['calls_without_record']} without), {judged} edges judged",
             f"  engine accepted, lab accepted : {c['agree_accepted']}   (same final points: {c['same_points']}, "
             f"different: {c['different_points']})",
             f"  engine rejected, lab rejected : {c['agree_rejected']}",
             f"  lab stricter than the engine  : {c['lab_stricter']}   (should be 0)",
             f"  lab more permissive           : {c['lab_more_permissive']}   (expected: text and foreign-pin checks are not replayed)"]
    for item in result["disagreements"][:8]:
        lines.append(f"    {item['kind']}: {item['call']} {item['edge']} {item.get('lab', '')}")
    return "\n".join(lines)


def summarize(per_call: dict[str, list[dict]]) -> dict[str, Any]:
    rows = [row for call in per_call.values() for row in call]
    accepted = [row for row in rows if row["verdict"] == "ok"]
    verdicts: dict[str, int] = {}
    for row in rows:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
    routed = [row for row in rows if "length_mm" in row]
    recorded = [row for row in rows if "same_as_recorded" in row]
    return {
        "edges": len(rows),
        "accepted": len(accepted),
        "verdicts": verdicts,
        "length_accepted_mm": round(sum(row["length_mm"] for row in accepted), 2),
        "median_length_accepted_mm": round(statistics.median(row["length_mm"] for row in accepted), 2) if accepted else None,
        "bends_accepted": sum(row["bends"] for row in accepted),
        "tie_coordinates": sum(row.get("tie_coordinates", 0) for row in routed),
        "backtracking_ends": sum(row.get("backtracking_ends", 0) for row in routed),
        "reproduced_recorded": f"{sum(row['same_as_recorded'] for row in recorded)}/{len(recorded)}" if recorded else None,
    }


def common_comparison(rows: dict[str, dict[str, list[dict]]], baseline: str) -> dict[str, dict[str, Any]]:
    """Length and bends over the edges that both the baseline variant and each other variant accept."""
    def accepted(name: str) -> dict[tuple[str, str], dict]:
        return {(call, row["edge"]): row for call, call_rows in rows[name].items() for row in call_rows
                if row["verdict"] == "ok"}

    base = accepted(baseline)
    comparison: dict[str, dict[str, Any]] = {}
    for name in rows:
        if name == baseline:
            continue
        other = accepted(name)
        shared = sorted(set(base) & set(other))
        comparison[name] = {
            "common_edges": len(shared),
            f"length_{baseline}_mm": round(sum(base[k]["length_mm"] for k in shared), 2),
            "length_variant_mm": round(sum(other[k]["length_mm"] for k in shared), 2),
            f"bends_{baseline}": sum(base[k]["bends"] for k in shared),
            "bends_variant": sum(other[k]["bends"] for k in shared),
        }
    return comparison


def format_report(report: dict[str, Any], baseline: str = "asis") -> str:
    lines = [f"{report['calls']} recorded libavoid calls", ""]
    header = (f"{'variant':<20} {'edges':>5} {'accepted':>8} {'rejected as':<34} {'len mm':>8} {'bends':>5} "
              f"{'ties':>4} {'backtrack':>9} {'reproduced':>10}")
    lines.append(header)
    lines.append("-" * len(header))
    for name, summary in report["variants"].items():
        rejected = ", ".join(f"{k} {v}" for k, v in sorted(summary["verdicts"].items()) if k != "ok") or "-"
        lines.append(
            f"{name:<20} {summary['edges']:>5} {summary['accepted']:>8} {rejected:<34} "
            f"{summary['length_accepted_mm']:>8} {summary['bends_accepted']:>5} {summary['tie_coordinates']:>4} "
            f"{summary['backtracking_ends']:>9} {str(summary['reproduced_recorded'] or '-'):>10}")
    if baseline in report["rows"] and len(report["rows"]) > 1:
        lines += ["", f"On the edges both '{baseline}' and the variant accept:"]
        for name, values in common_comparison(report["rows"], baseline).items():
            lines.append(
                f"  {name:<20} {values['common_edges']:>4} edges   length {values[f'length_{baseline}_mm']} -> "
                f"{values['length_variant_mm']} mm   bends {values[f'bends_{baseline}']} -> {values['bends_variant']}")
    return "\n".join(lines)
