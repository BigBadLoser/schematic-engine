"""Prototype: one whole-circuit ELK layered layout with convention-chosen orientations. Offline, no engine use.
Run: PYTHONPATH=. venv-python scripts/ci/sandbox/probes/elk_layered/elk_layered.py [demo_buck demo_lm317]"""
from __future__ import annotations
import itertools, json, math, os, subprocess, sys
from pathlib import Path

from kicad_mcp.tools.schematic import get_pin_positions, get_pin_outward_normals, get_symbol_primitive_bounds
from scripts import schematic_demo_circuits as demos

HERE = Path(__file__).resolve().parent
BUNDLE = HERE.parents[1] / ".work" / "elk.bundled.js"
OUT = HERE / "out"
G = 1.27
STUB = 5.08
SIDE = {(1, 0): "EAST", (-1, 0): "WEST", (0, 1): "SOUTH", (0, -1): "NORTH"}


def snap(v, g=G):
    return round(v / g) * g


class Part:
    def __init__(s, sym):
        s.ref, s.lib, s.name = sym.reference, sym.library, sym.symbol_name
        s.pins = list(get_pin_positions(s.lib, s.name, 0, 0, 0, 1))
        s.rail = {}
        s.sig = set()
        s.rank = 0

    def geom(s, rot):
        pos = get_pin_positions(s.lib, s.name, 0, 0, rot, 1)
        nrm = get_pin_outward_normals(s.lib, s.name, rot, 1)
        b = get_symbol_primitive_bounds(s.lib, s.name, 0, 0, rot, 1)
        return pos, {k: (round(v[0]), round(v[1])) for k, v in nrm.items()}, b


def load(name):
    intent = getattr(demos, name)()
    parts = {sy.reference: Part(sy) for sy in intent.symbols if sy.library != "power"}
    for i, blk in enumerate(intent.blocks):
        for r in blk.members:
            if r in parts:
                parts[r].rank = i
    nets = []
    for n in intent.nets:
        pins = [(p.reference, p.pin) for p in n.endpoints if p.reference in parts]
        kind = n.kind or "signal"
        nets.append((n.name, kind, pins))
        for r, p in pins:
            if kind in ("ground", "power"):
                parts[r].rail[p] = kind
            else:
                parts[r].sig.add(p)
    outs = {o for blk in intent.blocks for o in blk.outputs}
    return parts, nets, outs


def rot_with(part, pin, normal):
    for rot in (0, 90, 180, 270):
        if part.geom(rot)[1][pin] == normal:
            return rot
    return 0


def candidates(part, nets):
    if len(part.pins) == 2:
        a, b = part.pins
        railed = [p for p in part.pins if p in part.rail]
        if len(railed) == 1:
            p = railed[0]
            return [rot_with(part, p, (0, 1) if part.rail[p] == "ground" else (0, -1))]

        def nb(pin):
            rs = [PARTS[r].rank for nn, k, pins in nets if (part.ref, pin) in pins for r, _ in pins if r != part.ref]
            return sum(rs) / len(rs) if rs else part.rank
        left = a if nb(a) <= nb(b) else b
        return [rot_with(part, left, (-1, 0))]
    if part.lib.startswith("Transistor"):
        return [0, 180]
    if part.name.startswith("D_Bridge"):
        return [0, 90, 180, 270]
    if part.name.startswith("Transformer"):
        return [0, 180]
    return [0]


VARIANTS = [
    {"elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX", "elk.layered.nodePlacement.favorStraightEdges": "true"},
    {"elk.layered.nodePlacement.strategy": "BRANDES_KOEPF", "elk.layered.nodePlacement.bk.edgeStraightening": "IMPROVE_STRAIGHTNESS"},
    {"elk.layered.nodePlacement.strategy": "BRANDES_KOEPF", "elk.layered.layering.strategy": "LONGEST_PATH"},
]


def build_graph(parts, nets, outs, rots, variant=0):
    nodes, geo = [], {}
    for ref, p in parts.items():
        pos, nrm, b = p.geom(rots[ref])
        x0, y0, x1, y1 = b
        for q in pos.values():
            x0, x1, y0, y1 = min(x0, q[0]), max(x1, q[0]), min(y0, q[1]), max(y1, q[1])
        ext = {"EAST": 0, "WEST": 0, "SOUTH": 0, "NORTH": 0}
        for pin in p.rail:
            ext[SIDE[nrm[pin]]] = STUB
        x0, x1, y0, y1 = x0 - ext["WEST"], x1 + ext["EAST"], y0 - ext["NORTH"], y1 + ext["SOUTH"]
        x0, y0 = math.floor(x0 / G) * G, math.floor(y0 / G) * G
        x1, y1 = math.ceil(x1 / G) * G, math.ceil(y1 / G) * G
        ports = []
        for pin in p.sig:
            side = SIDE[nrm[pin]]
            px, py = pos[pin]
            if os.environ.get('SHUNT_WEST') and p.rail and len(p.pins) == 2:   # what-if: ignore real geometry
                side, px, py = 'WEST', x0, (y0 + y1) / 2
            px = {"EAST": x1, "WEST": x0}.get(side, px)
            py = {"SOUTH": y1, "NORTH": y0}.get(side, py)
            ports.append({"id": f"{ref}:{pin}", "x": px - x0, "y": py - y0, "width": 0, "height": 0,
                          "layoutOptions": {"elk.port.side": side}})
        geo[ref] = dict(origin=(x0, y0), size=(x1 - x0, y1 - y0), body=b, pos=pos, nrm=nrm)
        nodes.append({"id": ref, "width": x1 - x0, "height": y1 - y0, "ports": ports,
                      "layoutOptions": {"elk.portConstraints": "FIXED_POS"}})
    edges, conn = [], []
    for name, kind, pins in nets:
        if kind in ("ground", "power"):
            continue
        pins = [pp for pp in pins if pp[1] in parts[pp[0]].sig]
        if len(pins) < 2:
            continue
        mr = min(parts[q[0]].rank for q in pins)
        root = next((pp for pp in pins if name in outs and parts[pp[0]].rank == mr), pins[0])
        for pp in pins:
            if pp == root or pp[0] == root[0]:
                continue
            eid = f"{name}#{len(edges)}"
            edges.append({"id": eid, "sources": [f"{root[0]}:{root[1]}"], "targets": [f"{pp[0]}:{pp[1]}"]})
            conn.append((eid, name))
    graph = {"id": "root", "children": nodes, "edges": edges, "layoutOptions": {
        "elk.algorithm": "layered", "elk.direction": "RIGHT", "elk.edgeRouting": "ORTHOGONAL",
        "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
        "elk.spacing.nodeNode": str(G * 4), "elk.layered.spacing.nodeNodeBetweenLayers": str(G * 10),
        "elk.spacing.edgeNode": str(G * 2), "elk.spacing.edgeEdge": str(G * 2),
        "elk.layered.spacing.edgeEdgeBetweenLayers": str(G * 2), "elk.layered.spacing.edgeNodeBetweenLayers": str(G * 2),
        "elk.padding": "[top=0,left=0,bottom=0,right=0]", "elk.separateConnectedComponents": "false", **VARIANTS[variant]}}
    return graph, geo, conn


def route(e):
    s = e["sections"][0]
    pts = [(s["startPoint"]["x"], s["startPoint"]["y"])] + [(b["x"], b["y"]) for b in s.get("bendPoints", [])] \
        + [(s["endPoint"]["x"], s["endPoint"]["y"])]
    pts = [(snap(x), snap(y)) for x, y in pts]
    out = []
    for p in pts:
        if out and p == out[-1]:
            continue
        out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
            out.pop(i)
        else:
            i += 1
    return out


def metrics(layout, geo, conn):
    routes = {e["id"]: route(e) for e in layout["edges"]}
    net_of = dict(conn)
    corners = sum(max(len(r) - 2, 0) for r in routes.values())
    jogs = sum(1 for r in routes.values() for a, b in zip(r[1:-2], r[2:-1]) if math.dist(a, b) < 2.54 - 1e-6)
    segs = [(net_of[k], a, b) for k, r in routes.items() for a, b in zip(r, r[1:])]
    cross = 0
    for (n1, a, b), (n2, c, d) in itertools.combinations(segs, 2):
        if n1 == n2 or (a[0] == b[0]) == (c[0] == d[0]):
            continue
        v, h = ((a, b), (c, d)) if a[0] == b[0] else ((c, d), (a, b))
        if min(h[0][0], h[1][0]) < v[0][0] < max(h[0][0], h[1][0]) and \
           min(v[0][1], v[1][1]) < h[0][1] < max(v[0][1], v[1][1]):
            cross += 1
    ch = layout["children"]
    area = (max(c["x"] + c["width"] for c in ch) - min(c["x"] for c in ch)) * \
           (max(c["y"] + c["height"] for c in ch) - min(c["y"] for c in ch))
    n = max(len(routes), 1)
    return dict(connections=len(routes), corners_per_conn=round(corners / n, 2), crossings=cross, jogs=jogs,
                area_mm2=round(area)), routes


def score(m):
    return m["crossings"] * 10 + m["corners_per_conn"] * 10 + m["jogs"] + m["area_mm2"] / 2000


def render(path, parts, nets, layout, geo, routes, title, m):
    sc = 6.0
    ox = {c["id"]: (snap(c["x"]), snap(c["y"])) for c in layout["children"]}
    W = max(c["x"] + c["width"] for c in layout["children"])
    H = max(c["y"] + c["height"] for c in layout["children"])
    pad = 8
    W = max(W, 120)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{(W + 2 * pad) * sc:.0f}" height="{(H + 2 * pad + 10) * sc:.0f}" font-family="sans-serif">',
         '<rect width="100%" height="100%" fill="white"/>',
         f'<g transform="translate({pad * sc},{(pad + 8) * sc}) scale({sc})">',
         f'<text x="0" y="-5" font-size="2.2">{title}  corners/conn {m["corners_per_conn"]}  crossings {m["crossings"]}  jogs {m["jogs"]}  area {m["area_mm2"]} mm2</text>']
    colors = ["#c0392b", "#2471a3", "#1e8449", "#b7950b", "#7d3c98", "#ca6f1e", "#117a65", "#6c3483"]
    for ref, p in parts.items():
        g = geo[ref]
        nx, ny = ox[ref]
        x0, y0 = g["origin"]
        ax, ay = nx - x0, ny - y0
        bx0, by0, bx1, by1 = g["body"]
        cx, cy = ax + (bx0 + bx1) / 2, ay + (by0 + by1) / 2
        o.append(f'<rect x="{nx}" y="{ny}" width="{g["size"][0]}" height="{g["size"][1]}" fill="none" stroke="#bbb" stroke-width=".2" stroke-dasharray="1,1"/>')
        o.append(f'<rect x="{ax + bx0}" y="{ay + by0}" width="{bx1 - bx0}" height="{by1 - by0}" fill="#fffbe6" stroke="#333" stroke-width=".3"/>')
        o.append(f'<text x="{cx}" y="{cy}" font-size="2" text-anchor="middle">{ref}</text>')
        o.append(f'<text x="{cx}" y="{cy + 2.4}" font-size="1.4" text-anchor="middle" fill="#666">{p.name[:14]}</text>')
        for pin, (px, py) in g["pos"].items():
            X, Y = ax + px, ay + py
            nx_, ny_ = g["nrm"][pin]
            o.append(f'<circle cx="{X}" cy="{Y}" r=".45" fill="#333"/>')
            o.append(f'<text x="{X - nx_ * 1.3 - .4}" y="{Y - ny_ * 1.3 + .5}" font-size="1.1" fill="#888">{pin}</text>')
            if pin in p.rail:
                X2, Y2 = X + nx_ * STUB, Y + ny_ * STUB
                net = next(n for n, k, pins in nets if (ref, pin) in pins)
                o.append(f'<line x1="{X}" y1="{Y}" x2="{X2}" y2="{Y2}" stroke="#555" stroke-width=".25"/>')
                if p.rail[pin] == "ground":
                    o.append(f'<path d="M{X2 - 1},{Y2} h2 M{X2 - .6},{Y2 + .5} h1.2 M{X2 - .25},{Y2 + 1} h.5" stroke="#555" stroke-width=".25"/>')
                else:
                    o.append(f'<path d="M{X2 - .8},{Y2} h1.6 L{X2},{Y2 - 1.2} z" fill="#555"/>')
                o.append(f'<text x="{X2 + 1.2}" y="{Y2 + .5}" font-size="1.3" fill="#555">{net}</text>')
    ci = {}
    for eid, r in routes.items():
        net = eid.split("#")[0]
        col = colors[ci.setdefault(net, len(ci)) % len(colors)]
        o.append(f'<polyline points="{" ".join(f"{x},{y}" for x, y in r)}" fill="none" stroke="{col}" stroke-width=".35"/>')
    seen = set()
    for eid, r in routes.items():
        net = eid.split("#")[0]
        if net in seen:
            continue
        seen.add(net)
        o.append(f'<text x="{r[0][0] + .8}" y="{r[0][1] - .8}" font-size="1.3" fill="{colors[ci[net] % len(colors)]}">{net}</text>')
    o.append("</g></svg>")
    Path(path).write_text("\n".join(o), encoding="utf-8")
    return int((W + 2 * pad) * sc), int((H + 2 * pad + 10) * sc)


PARTS: dict = {}


def run(name):
    global PARTS
    parts, nets, outs = load(name)
    PARTS = parts
    cands = {r: candidates(p, nets) for r, p in parts.items()}
    refs = list(parts)
    combos = [(c, v) for c in itertools.product(*[cands[r] for r in refs]) for v in range(len(VARIANTS))]
    built = [build_graph(parts, nets, outs, dict(zip(refs, c)), v) for c, v in combos]
    OUT.mkdir(exist_ok=True)
    gp, rp = OUT / f"{name}_graphs.json", OUT / f"{name}_layouts.json"
    gp.write_text(json.dumps([b[0] for b in built]))
    subprocess.run(["node", str(HERE / "run_elk.mjs"), str(BUNDLE), str(gp), str(rp)], check=True, timeout=300)
    res = json.loads(rp.read_text())
    best = None
    for c, (g, geo, conn), lay in zip(combos, built, res):
        if "error" in lay:
            continue
        m, routes = metrics(lay, geo, conn)
        if best is None or score(m) < score(best[0]):
            best = (m, routes, lay, geo, dict(zip(refs, c[0])), c[1])
    m, routes, lay, geo, rots, var = best
    png, svg = OUT / f"{name}{os.environ.get('SHUNT_WEST', '') and '_whatif'}.png", OUT / f"{name}{os.environ.get('SHUNT_WEST', '') and '_whatif'}.svg"
    w, h = render(svg, parts, nets, lay, geo, routes, name, m)
    subprocess.run([r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--headless=new", f"--screenshot={png}",
                    f"--window-size={w},{h}", str(svg)], capture_output=True, timeout=60)
    print(name, len(combos), "combos, variant", var, m, rots)


if __name__ == "__main__":
    for n in sys.argv[1:] or ["demo_buck", "demo_lm317"]:
        run(n)
