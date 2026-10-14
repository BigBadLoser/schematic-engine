"""Audit how power and ground symbols reach their first non-power pin.

Read-only probe over the final engine sheets (mcu_smoke.kicad_sch) of the newest
complete sandbox run. For every #PWR symbol it follows the shortest wire path from
the symbol's pin tip to the first non-power pin tip, classifies the stub by corner
count, and, for bent stubs, finds the nearest thing standing on the straight line
from the part pin along the first wire segment, within 7.62 mm.

Assumptions:
  * wires connect at shared endpoints (0.01 mm tolerance) and at junctions lying
    on a segment's interior; other touches are not connections;
  * power and flag pin tips are pass-through nodes, not targets;
  * a wire's net is the set of #PWR nets in its connected component (wires with
    no power symbol have unknown net and are never counted as another net).

Writes out/stubs.csv and out/summary.md next to this file.
"""
from __future__ import annotations

import csv
import heapq
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "scripts" / "ci" / "sandbox"
for _p in (SANDBOX, REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backend.schematic.kicad_geometry import measured_scene_from_kicad  # noqa: E402
from backend.schematic.presentation_geometry import GeometryKind as K  # noqa: E402

RUN = SANDBOX / ".work" / "runs" / "20261011T031335Z-auto-st3" / "artifacts"
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SHEET = "mcu_smoke.kicad_sch"
TOL = 0.01
RAY_MM = 7.62
HIT_EPS = 1e-6
TEXT_KINDS = {K.REFERENCE, K.VALUE, K.POWER_TEXT, K.NET_LABEL}
CSV_COLUMNS = [
    "fixture", "symbol", "net", "part_pin", "part_ref", "class", "corners",
    "length_mm", "obstacle_kind", "obstacle_owner", "n_obstacles", "via_power", "status",
]


def _sgn(v: float) -> int:
    if abs(v) <= TOL:
        return 0
    return 1 if v > 0 else -1


def _ray_hit(o, d, length, b):
    """Entry distance along the ray from o in direction d (unit), or None if it misses."""
    t0, t1 = 0.0, length
    for ov, dv, lo, hi in ((o[0], d[0], b.left, b.right), (o[1], d[1], b.top, b.bottom)):
        if abs(dv) < 1e-12:
            if ov < lo - HIT_EPS or ov > hi + HIT_EPS:
                return None
            continue
        ta = (lo - ov) / dv
        tb = (hi - ov) / dv
        if ta > tb:
            ta, tb = tb, ta
        t0 = max(t0, ta)
        t1 = min(t1, tb)
        if t0 > t1 + 1e-9:
            return None
    return t0


def _natural(s: str):
    digits = "".join(ch for ch in s if ch.isdigit())
    return (s.rstrip("0123456789"), int(digits) if digits else -1)


def audit_sheet(fixture: str, path: Path) -> list[dict]:
    scene, _coverage = measured_scene_from_kicad(path)
    items = list(scene.items)

    nodes: list[tuple[float, float]] = []

    def nid(x: float, y: float) -> int:
        for k, (nx, ny) in enumerate(nodes):
            if abs(nx - x) <= TOL and abs(ny - y) <= TOL:
                return k
        nodes.append((x, y))
        return len(nodes) - 1

    terms = [i for i in items if i.kind == K.PIN_ENDPOINT and i.id.endswith(":terminal")]
    term_node = {t.id: nid(t.bounds.left, t.bounds.top) for t in terms}

    junctions: set[int] = set()
    for i in items:
        if i.kind == K.JUNCTION:
            b = i.bounds
            junctions.add(nid((b.left + b.right) / 2, (b.top + b.bottom) / 2))

    seg_items = [i for i in items if i.kind == K.WIRE and i.segment]
    seg_nodes: dict[str, tuple[int, int]] = {}
    for i in seg_items:
        (x1, y1), (x2, y2) = i.segment
        seg_nodes[i.id] = (nid(x1, y1), nid(x2, y2))

    edges: list[tuple[int, int, float]] = []
    for i in seg_items:
        a, b = seg_nodes[i.id]
        if a == b:
            continue
        ax, ay = nodes[a]
        bx, by = nodes[b]
        vertical = abs(ax - bx) <= TOL
        on = [a, b]
        for j in junctions:
            if j in (a, b):
                continue
            jx, jy = nodes[j]
            if vertical:
                if abs(jx - ax) <= TOL and min(ay, by) + TOL < jy < max(ay, by) - TOL:
                    on.append(j)
            elif abs(jy - ay) <= TOL and min(ax, bx) + TOL < jx < max(ax, bx) - TOL:
                on.append(j)
        on.sort(key=lambda k: math.dist(nodes[k], (ax, ay)))
        for u, v in zip(on, on[1:]):
            edges.append((u, v, math.dist(nodes[u], nodes[v])))

    par = list(range(len(nodes)))

    def find(x: int) -> int:
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for u, v, w in edges:
        adj[u].append((v, w))
        adj[v].append((u, w))
        ru, rv = find(u), find(v)
        if ru != rv:
            par[ru] = rv
    wire_root = {wid: find(seg_nodes[wid][0]) for wid in seg_nodes}

    pwr_nodes: dict[int, list] = defaultdict(list)
    part_nodes: dict[int, list] = defaultdict(list)
    pass_nodes: set[int] = set()
    for t in terms:
        k = term_node[t.id]
        if t.owner.startswith("#PWR"):
            pwr_nodes[k].append(t)
            pass_nodes.add(k)
        elif t.owner.startswith("#FLG"):
            pass_nodes.add(k)
        else:
            part_nodes[k].append(t)

    comp_nets: dict[int, set[str]] = defaultdict(set)
    for k, ts in pwr_nodes.items():
        for t in ts:
            if t.net:
                comp_nets[find(k)].add(t.net)

    term_by_owner: dict[str, object] = {}
    for t in terms:
        if t.owner.startswith("#PWR"):
            term_by_owner.setdefault(t.owner, t)
    owners = sorted({i.owner for i in items if i.owner.startswith("#PWR")}, key=_natural)

    def shortest(src: int):
        dist = {src: 0.0}
        prev: dict[int, int] = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, math.inf):
                continue
            if u in part_nodes:
                return u, prev
            for v, w in adj.get(u, ()):
                nd = d + w
                if nd < dist.get(v, math.inf) - 1e-12:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return None, prev

    def obstacle(origin, direction, part_ref, power_owner, own_root, net):
        best = None
        count = 0
        for it in items:
            k, o = it.kind, it.owner
            if o == power_owner:
                continue
            if k == K.WIRE:
                root = wire_root.get(it.id)
                if root is None or root == own_root:
                    continue
                nets = comp_nets.get(root)
                if not nets or net in nets:
                    continue
                label = "wire"
            elif o.startswith("#FLG"):
                label = "flag"
            elif k == K.BODY:
                if o == part_ref:
                    continue
                label = "body"
            elif k in TEXT_KINDS or (k == K.POWER_GRAPHIC and o.startswith("#PWR")):
                label = k.value
            else:
                continue
            t = _ray_hit(origin, direction, RAY_MM, it.bounds)
            if t is None:
                continue
            count += 1
            cand = (round(t, 6), label, o)
            if best is None or cand < best:
                best = cand
        return best, count

    rows: list[dict] = []
    for owner in owners:
        t = term_by_owner.get(owner)
        net = ""
        if t is not None:
            net = t.net or ""
        if not net:
            net = next((i.net for i in items if i.owner == owner and i.net), "") or ""
        base = {"fixture": fixture, "symbol": owner, "net": net}
        if t is None:
            rows.append({**base, "part_pin": "", "part_ref": "", "class": "open", "corners": "",
                         "length_mm": "", "obstacle_kind": "", "obstacle_owner": "",
                         "n_obstacles": "", "via_power": "", "status": "no_terminal"})
            continue
        src = term_node[t.id]
        target, prev = shortest(src)
        if target is None:
            rows.append({**base, "part_pin": "", "part_ref": "", "class": "open", "corners": "",
                         "length_mm": "", "obstacle_kind": "", "obstacle_owner": "",
                         "n_obstacles": "", "via_power": "", "status": "unterminated"})
            continue
        path = [target]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        dirs = [
            (_sgn(nodes[v][0] - nodes[u][0]), _sgn(nodes[v][1] - nodes[u][1]))
            for u, v in zip(path, path[1:])
        ]
        corners = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
        length = sum(math.dist(nodes[u], nodes[v]) for u, v in zip(path, path[1:]))
        via_power = any(n in pass_nodes for n in path[1:-1])
        pin_t = part_nodes[target][0]
        part_pin = pin_t.id[: -len(":terminal")]
        cls = "straight" if corners == 0 else ("L" if corners == 1 else "dogleg")
        row = {**base, "part_pin": part_pin, "part_ref": pin_t.owner, "class": cls,
               "corners": corners, "length_mm": round(length, 3), "obstacle_kind": "",
               "obstacle_owner": "", "n_obstacles": "", "via_power": int(via_power),
               "status": "ok"}
        if corners > 0 and len(path) >= 2:
            origin = nodes[target]
            px, py = nodes[path[-2]]
            dx, dy = px - origin[0], py - origin[1]
            norm = math.hypot(dx, dy)
            direction = (dx / norm, dy / norm)
            best, count = obstacle(origin, direction, pin_t.owner, owner, find(src), net)
            row["n_obstacles"] = count
            if best is not None:
                row["obstacle_kind"] = best[1]
                row["obstacle_owner"] = best[2]
            else:
                row["obstacle_kind"] = "none"
        rows.append(row)
    return rows


def _summary(rows: list[dict], skipped: list[str], fixtures: list[str]) -> str:
    lines = ["# Power/ground stub audit", ""]
    lines.append(f"Fixtures audited: {len(fixtures)}. Skipped (no {SHEET}): "
                 f"{', '.join(skipped) if skipped else 'none'}.")
    lines.append("")
    lines.append("## Per fixture")
    lines.append("")
    lines.append("| fixture | symbols | straight | L | dogleg | open |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for fx in fixtures:
        fr = [r for r in rows if r["fixture"] == fx]
        c = Counter(r["class"] for r in fr)
        lines.append(f"| {fx} | {len(fr)} | {c['straight']} | {c['L']} | {c['dogleg']} | {c['open']} |")
    total = Counter(r["class"] for r in rows)
    n = len(rows)
    lines.append(f"| **all** | {n} | {total['straight']} | {total['L']} | {total['dogleg']} | {total['open']} |")
    if n:
        lines.append("")
        lines.append(f"Shares of all symbols: straight {total['straight'] / n:.1%}, "
                     f"L {total['L'] / n:.1%}, dogleg {total['dogleg'] / n:.1%}, "
                     f"open {total['open'] / n:.1%}.")
    bent = [r for r in rows if r["class"] in ("L", "dogleg")]
    lines.append("")
    lines.append("## Obstacle kinds for bent stubs")
    lines.append("")
    lines.append(f"{len(bent)} bent stubs.")
    lines.append("")
    lines.append("| obstacle kind | count |")
    lines.append("|---|---:|")
    for kind, cnt in Counter(r["obstacle_kind"] or "none" for r in bent).most_common():
        lines.append(f"| {kind} | {cnt} |")
    lines.append("")
    lines.append("## 10 worst stubs (corners, then length)")
    lines.append("")
    lines.append("| fixture | symbol | net | part pin | corners | length mm | obstacle | owner |")
    lines.append("|---|---|---|---|---:|---:|---|---|")
    worst = sorted(bent, key=lambda r: (-r["corners"], -r["length_mm"]))[:10]
    for r in worst:
        lines.append(f"| {r['fixture']} | {r['symbol']} | {r['net']} | {r['part_pin']} | {r['corners']} | "
                     f"{r['length_mm']} | {r['obstacle_kind']} | {r['obstacle_owner']} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    fixtures: list[str] = []
    skipped: list[str] = []
    rows: list[dict] = []
    for folder in sorted(RUN.glob("schematic-engine-*")):
        name = folder.name.removeprefix("schematic-engine-")
        sheet = folder / SHEET
        if not sheet.is_file():
            skipped.append(name)
            continue
        fixtures.append(name)
        rows.extend(audit_sheet(name, sheet))
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "stubs.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
    (OUT / "summary.md").write_text(_summary(rows, skipped, fixtures), encoding="utf-8")
    total = Counter(r["class"] for r in rows)
    print(f"symbols={len(rows)} fixtures={len(fixtures)} skipped={skipped}")
    print(dict(total))
    print(Counter(r["obstacle_kind"] or "none" for r in rows if r["class"] in ("L", "dogleg")).most_common())
    print(Counter(r["status"] for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
