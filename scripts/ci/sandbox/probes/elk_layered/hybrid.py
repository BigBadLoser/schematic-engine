"""Hybrid prototype: ELK layered on the signal-path spine only, shunt parts attached afterwards (vertical, T-join).
Run: PYTHONPATH=. venv-python scripts/ci/sandbox/probes/elk_layered/hybrid.py [demo_buck demo_lm317]
Reuses elk_layered.py (graph building, rendering); outputs go to out/<name>_hybrid.{svg,png}."""
from __future__ import annotations
import itertools, json, math, subprocess, sys
from pathlib import Path

import elk_layered as E
from elk_layered import G, STUB, snap, OUT, HERE, BUNDLE

GAP = 2.54


def seg_cross(s1, s2):
    (a, b), (c, d) = s1, s2
    if (a[0] == b[0]) == (c[0] == d[0]):
        return False
    v, h = ((a, b), (c, d)) if a[0] == b[0] else ((c, d), (a, b))
    return min(h[0][0], h[1][0]) < v[0][0] < max(h[0][0], h[1][0]) and \
        min(v[0][1], v[1][1]) < h[0][1] < max(v[0][1], v[1][1])


def seg_in_box(s, box, m=0.0):
    (a, b) = s
    x0, y0, x1, y1 = box[0] - m, box[1] - m, box[2] + m, box[3] + m
    lx, hx, ly, hy = min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])
    return hx > x0 and lx < x1 and hy > y0 and ly < y1


def simplify(pts):
    out = []
    for p in pts:
        if not out or p != out[-1]:
            out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        if (a[0] == b[0] == c[0] and (b[1] - a[1]) * (c[1] - b[1]) < 0) or \
           (a[1] == b[1] == c[1] and (b[0] - a[0]) * (c[0] - b[0]) < 0):
            return None   # back-tracking
        if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
            out.pop(i)
        else:
            i += 1
    return out


def attach(ref, part, sigpin, net, geo, rot, net_pts, net_runs, boxes, segs):
    """Pick the best placement for a shunt part. Returns (route, node_origin_abs, score)."""
    g = geo
    s = 1 if part.rail[next(iter(part.rail))] == "ground" else -1   # shunt below (ground) or above (power) the net
    sp = g["pos"][sigpin]
    ox0, oy0 = g["origin"]; w, h = g["size"]
    cands = []   # (route, tip)
    for (y, xa, xb, ) in net_runs:   # T-joins on horizontal runs
        x = math.ceil((xa + GAP) / GAP) * GAP
        while x <= xb - GAP:
            cands.append(([(x, y), (x, y + s * GAP)], (x, y + s * GAP)))
            x += GAP
    for P, n in net_pts:   # straight / L from a pin of the net
        for d in (GAP, 2 * GAP):
            J = (P[0] + n[0] * d, P[1] + n[1] * d)
            for dx in (0, 3 * 2.54, -3 * 2.54, 6 * 2.54, -6 * 2.54):
                xs = J[0] + dx
                tip = (xs, J[1] + s * GAP)
                r = simplify([P, J, (xs, J[1]), tip])
                if r:
                    cands.append((r, tip))
    best = None
    for r, tip in cands:
        A = (tip[0] - sp[0], tip[1] - sp[1])
        box = (A[0] + ox0, A[1] + oy0, A[0] + ox0 + w, A[1] + oy0 + h)
        rsegs = list(zip(r, r[1:]))
        bad = False
        for b in boxes.values():
            if box[0] < b[2] + GAP and box[2] > b[0] - GAP and box[1] < b[3] + GAP and box[3] > b[1] - GAP:
                bad = True; break
            if any(seg_in_box(sg, b) for sg in rsegs):
                bad = True; break
        if bad:
            continue
        cross = 0
        for nm, sg in segs:
            if nm != net and seg_in_box(sg, box, 0.5):
                bad = True; break
            if nm != net:
                cross += sum(seg_cross(sg, q) for q in rsegs)
        if bad:
            continue
        corners = len(r) - 2
        length = sum(math.dist(a, b) for a, b in rsegs)
        cx = sum(x for x, _ in r) / len(r)
        sc = corners * 10 + length / 20 + cross * 100
        if best is None or sc < best[2]:
            best = (r, A, sc, box)
    return best


def build(name):
    parts, nets, outs = E.load(name)
    E.PARTS = parts
    shunts = {r: p for r, p in parts.items() if len(p.sig) == 1 and p.rail}
    spine = {r: p for r, p in parts.items() if r not in shunts}
    cands = {r: E.candidates(p, nets) for r, p in parts.items()}
    sref = list(spine)
    cands_s = [cands[r] for r in sref]
    snets = [(n, k, [pp for pp in pins if pp[0] in spine]) for n, k, pins in nets]
    results = []
    combos = [(c, v) for c in itertools.product(*cands_s) for v in range(len(E.VARIANTS))]
    built = []
    for c, v in combos:
        rots = dict(zip(sref, c)); rots.update({r: cands[r][0] for r in shunts})
        gr, geo, conn = E.build_graph(spine, snets, outs, rots, v)
        built.append((gr, geo, conn, rots))
    gp, rp = OUT / f"{name}_hyb_graphs.json", OUT / f"{name}_hyb_layouts.json"
    gp.write_text(json.dumps([b[0] for b in built]))
    subprocess.run(["node", str(HERE / "run_elk.mjs"), str(BUNDLE), str(gp), str(rp)], check=True, timeout=300)
    layouts = json.loads(rp.read_text())
    best = None
    for (c, v), (gr, geo, conn, rots), lay in zip(combos, built, layouts):
        if "error" in lay:
            continue
        r = finish(parts, nets, shunts, lay, geo, conn, rots)
        if r and (best is None or E.score(r[0]) < E.score(best[0])):
            best = r + (v,)
    return parts, nets, best, cands


def finish(parts, nets, shunts, lay, geo, conn, rots):
    pos = {c["id"]: (snap(c["x"]), snap(c["y"])) for c in lay["children"]}
    size = {c["id"]: (c["width"], c["height"]) for c in lay["children"]}
    boxes = {r: (pos[r][0], pos[r][1], pos[r][0] + size[r][0], pos[r][1] + size[r][1]) for r in pos}
    routes = {e["id"]: E.route(e) for e in lay["edges"]}
    net_of = dict(conn)
    segs = [(net_of[k], a, b) for k, r in routes.items() for a, b in zip(r, r[1:])]
    segs = [(n, (a, b)) for n, a, b in segs]
    tips = {}
    for r in pos:
        o = geo[r]["origin"]
        for pin, q in geo[r]["pos"].items():
            tips[(r, pin)] = (pos[r][0] - o[0] + q[0], pos[r][1] - o[1] + q[1])
    placed = {}
    for ref in sorted(shunts, key=lambda r: [n for n, k, p in nets if any(x[0] == r for x in p)][0]):
        p = shunts[ref]
        sigpin = next(iter(p.sig))
        net = next(n for n, k, pins in nets if (ref, sigpin) in pins and k not in ("ground", "power"))
        g = shunt_geo(p, rots[ref])
        npts = []
        for n, k, pins in nets:
            if n == net:
                for rr, pn in pins:
                    if (rr, pn) in tips:
                        npts.append((tips[(rr, pn)], geo[rr]["nrm"][pn]))
        runs = []
        for k, r in routes.items():
            if net_of[k] == net:
                for a, b in zip(r, r[1:]):
                    if a[1] == b[1]:
                        runs.append((a[1], min(a[0], b[0]), max(a[0], b[0])))
        for k, (rr, a, b) in placed_runs.items() if False else []:
            pass
        res = attach(ref, p, sigpin, net, g, rots[ref], npts, runs, boxes, segs)
        if res is None:
            return None
        r, A, sc, box = res
        boxes[ref] = box
        routes[f"{net}#s{ref}"] = r
        net_of[f"{net}#s{ref}"] = net
        segs += [(net, sg) for sg in zip(r, r[1:])]
        pos[ref] = (A[0] + g["origin"][0], A[1] + g["origin"][1]); size[ref] = g["size"]
        geo[ref] = g
    m = measure(routes, net_of, boxes)
    children = [{"id": r, "x": pos[r][0], "y": pos[r][1], "width": size[r][0], "height": size[r][1]} for r in pos]
    return m, routes, {"children": children}, dict(geo), dict(rots)


def shunt_geo(p, rot):
    pos, nrm, b = p.geom(rot)
    x0, y0, x1, y1 = b
    for q in pos.values():
        x0, x1, y0, y1 = min(x0, q[0]), max(x1, q[0]), min(y0, q[1]), max(y1, q[1])
    ext = {"EAST": 0, "WEST": 0, "SOUTH": 0, "NORTH": 0}
    for pin in p.rail:
        ext[E.SIDE[nrm[pin]]] = STUB
    x0, x1, y0, y1 = x0 - ext["WEST"], x1 + ext["EAST"], y0 - ext["NORTH"], y1 + ext["SOUTH"]
    x0, y0 = math.floor(x0 / G) * G, math.floor(y0 / G) * G
    x1, y1 = math.ceil(x1 / G) * G, math.ceil(y1 / G) * G
    return dict(origin=(x0, y0), size=(x1 - x0, y1 - y0), body=b, pos=pos, nrm=nrm)


def measure(routes, net_of, boxes):
    n = len(routes)
    corners = sum(max(len(r) - 2, 0) for r in routes.values())
    jogs = sum(1 for r in routes.values() for a, b in zip(r[1:-2], r[2:-1]) if math.dist(a, b) < 2.54 - 1e-6)
    segs = [(net_of[k], (a, b)) for k, r in routes.items() for a, b in zip(r, r[1:])]
    cross = sum(1 for (n1, s1), (n2, s2) in itertools.combinations(segs, 2) if n1 != n2 and seg_cross(s1, s2))
    area = (max(b[2] for b in boxes.values()) - min(b[0] for b in boxes.values())) * \
           (max(b[3] for b in boxes.values()) - min(b[1] for b in boxes.values()))
    return dict(connections=n, corners_per_conn=round(corners / n, 2), crossings=cross, jogs=jogs, area_mm2=round(area))


def run(name):
    OUT.mkdir(exist_ok=True)
    parts, nets, best, cands = build(name)
    m, routes, lay, geo, rots, var = best
    svg, png = OUT / f"{name}_hybrid.svg", OUT / f"{name}_hybrid.png"
    w, h = E.render(svg, parts, nets, lay, geo, routes, name + " hybrid", m)
    subprocess.run([r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--headless=new", f"--screenshot={png}",
                    f"--window-size={w},{h}", str(svg)], capture_output=True, timeout=60)
    print(name, "variant", var, m, {r: v for r, v in rots.items() if len(cands[r]) > 1 or len(parts[r].pins) == 2})


if __name__ == "__main__":
    for n in sys.argv[1:] or ["demo_buck", "demo_lm317"]:
        run(n)
