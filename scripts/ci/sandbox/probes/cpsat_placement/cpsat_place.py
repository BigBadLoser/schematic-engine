"""Offline CP-SAT placement prototype vs the greedy search, on recorded placement calls (see results.md).

Run from scripts/ci/sandbox with PYTHONPATH=. : python probes/cpsat_placement/cpsat_place.py
"""
from __future__ import annotations

import math
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from ortools.sat.python import cp_model

from sbx import placement_replay as pr, report
from backend.schematic import placement_refinement as pe

G = 1.27
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(__file__).parent / "out"
GROUPS = [4, 10, 20, 42, 58, 76, 84, 110]
W = pe.Weights()
S = 100  # integer cost = round(S x mm-equivalent)
BIG = 10 ** 4


def cells(v: float) -> int:
    return round(v / G)


def solve(symbols, nets, reach, half_widths, seconds=20.0):
    model0 = pe._Model(symbols, nets, reach, W, half_widths)
    m = cp_model.CpModel()
    start = {s.reference: (s.position, s.orientation) for s in symbols}
    sc = {r: (cells(p[0]), cells(p[1])) for r, (p, _o) in start.items()}
    X, Y, xo, Lf, Rt, To, Bo = {}, {}, {}, {}, {}, {}, {}
    cost: list = []
    ix, iy = [], []

    def rect(g):
        return (math.floor(g.box[0] / G + 1e-6), math.ceil(g.box[2] / G - 1e-6),
                math.floor(g.box[1] / G + 1e-6), math.ceil(g.box[3] / G - 1e-6))

    for s in symbols:
        r = s.reference
        if not s.movable:
            X[r], Y[r] = sc[r]
            l, rr, t, b = rect(s.geometry[s.orientation])
            ix.append(m.NewIntervalVar(X[r] + l, rr - l, X[r] + rr, f"fx{r}"))
            iy.append(m.NewIntervalVar(Y[r] + t, b - t, Y[r] + b, f"fy{r}"))
            Lf[r], Rt[r], To[r], Bo[r] = X[r] + l, X[r] + rr, Y[r] + t, Y[r] + b
            continue
        span = 40 if s.window is None else math.ceil(s.window / G)
        X[r] = m.NewIntVar(sc[r][0] - span, sc[r][0] + span, f"X{r}")
        Y[r] = m.NewIntVar(sc[r][1] - span, sc[r][1] + span, f"Y{r}")
        xo[r] = {o: m.NewBoolVar(f"x{r}{i}") for i, o in enumerate(s.geometry)}
        m.AddExactlyOne(xo[r].values())
        Lf[r], Rt[r], To[r], Bo[r] = (m.NewIntVar(-BIG, BIG, f"{k}{r}") for k in "LRTB")
        for o, lit in xo[r].items():
            l, rr, t, b = rect(s.geometry[o])
            ix.append(m.NewOptionalIntervalVar(X[r] + l, rr - l, X[r] + rr, lit, f"ix{r}"))
            iy.append(m.NewOptionalIntervalVar(Y[r] + t, b - t, Y[r] + b, lit, f"iy{r}"))
            m.Add(Lf[r] == X[r] + l).OnlyEnforceIf(lit)
            m.Add(Rt[r] == X[r] + rr).OnlyEnforceIf(lit)
            m.Add(To[r] == Y[r] + t).OnlyEnforceIf(lit)
            m.Add(Bo[r] == Y[r] + b).OnlyEnforceIf(lit)
            # orientation cost: the engine's (learned table when given, else turn x quarter turns + flip)
            if s.orientation_cost is not None:
                c = s.orientation_cost[o]
            else:
                q = ((o.rotation - s.orientation.rotation) // 90) % 4
                c = W.turn * min(q, 4 - q) + (W.flip if o.mirror != s.orientation.mirror else 0.0)
            cost.append(round(S * c) * lit)
        m.AddHint(X[r], sc[r][0])
        m.AddHint(Y[r], sc[r][1])
        for o, lit in xo[r].items():
            m.AddHint(lit, 1 if o == s.orientation else 0)
    m.AddNoOverlap2D(ix, iy)

    pins: dict = {}

    def pin(ref, name):
        if (ref, name) in pins:
            return pins[ref, name]
        s = model0.symbols[ref]
        if not s.movable:
            g = s.geometry[s.orientation].pins[name]
            px = sc[ref][0] + cells(g.point[0])
            py = sc[ref][1] + cells(g.point[1])
            hz = m.NewConstant(int(abs(g.normal[0]) > abs(g.normal[1])))
            pins[ref, name] = (px, py, hz)
            return pins[ref, name]
        px = m.NewIntVar(-BIG, BIG, f"px{ref}{name}")
        py = m.NewIntVar(-BIG, BIG, f"py{ref}{name}")
        horiz = []
        for o, lit in xo[ref].items():
            g = s.geometry[o].pins[name]
            m.Add(px == X[ref] + cells(g.point[0])).OnlyEnforceIf(lit)
            m.Add(py == Y[ref] + cells(g.point[1])).OnlyEnforceIf(lit)
            if abs(g.normal[0]) > abs(g.normal[1]):
                horiz.append(lit)
        hz = m.NewBoolVar(f"hz{ref}{name}")
        m.Add(hz == sum(horiz))
        pins[ref, name] = (px, py, hz)
        return pins[ref, name]

    start_world = {r: model0.world(r, *start[r]) for r in start}
    BEND = round(S * W.bend)
    npair = 0
    for net in model0.nets:
        if net.style == "wire":
            members = list(net.pins)
            if len(members) <= 3:
                pairs = [(a, b) for i, a in enumerate(members) for b in members[i + 1:]]
            else:
                order = sorted(members, key=lambda p: (start_world[p[0]][0][p[1]][0][0], start_world[p[0]][0][p[1]][0][1]))
                pairs = list(zip(order, order[1:]))
            for a, b in pairs:
                if not model0.symbols[a[0]].movable and not model0.symbols[b[0]].movable:
                    continue
                npair += 1
                (ax, ay, ah), (bx, by, bh) = pin(*a), pin(*b)
                dx = m.NewIntVar(0, BIG, "dx")
                dy = m.NewIntVar(0, BIG, "dy")
                ex = m.NewIntVar(-BIG, BIG, "ex")
                ey = m.NewIntVar(-BIG, BIG, "ey")
                m.Add(ex == ax - bx)
                m.Add(ey == ay - by)
                m.AddAbsEquality(dx, ex)
                m.AddAbsEquality(dy, ey)
                cost.append(round(net.weight * S * G) * (dx + dy))
                bothH, bothV = m.NewBoolVar("bH"), m.NewBoolVar("bV")
                m.AddBoolOr([ah.Not(), bh.Not(), bothH])
                m.AddImplication(bothH, ah)
                m.AddImplication(bothH, bh)
                m.AddBoolOr([ah, bh, bothV])
                m.AddImplication(bothV, ah.Not())
                m.AddImplication(bothV, bh.Not())
                aH, aV = m.NewBoolVar("aH"), m.NewBoolVar("aV")
                m.Add(ay == by).OnlyEnforceIf(aH)
                m.AddImplication(aH, bothH)
                m.Add(ax == bx).OnlyEnforceIf(aV)
                m.AddImplication(aV, bothV)
                same = m.NewBoolVar("same")
                m.AddBoolOr([bothH.Not(), same])
                m.AddBoolOr([bothV.Not(), same])
                m.AddBoolOr([same.Not(), bothH, bothV])
                L, Z = m.NewBoolVar("L"), m.NewBoolVar("Z")
                m.AddBoolOr([same, L])                    # axes differ: an L, one bend
                m.AddBoolOr([same.Not(), aH, aV, Z])      # same axis and not aligned: a Z, two bends
                cost.append(BEND * L + 2 * BEND * Z)
        elif net.style == "power" and net.facing is not None:
            for ref, name in net.pins:
                s = model0.symbols[ref]
                if not s.movable:
                    continue
                for o, lit in xo[ref].items():
                    al = pe._dot(s.geometry[o].pins[name].normal, net.facing)
                    c = W.power_away if al < -0.5 else W.power_sideways if al < 0.5 else 0
                    if c:
                        cost.append(round(S * c) * lit)
    minL, maxR, minT, maxB = (m.NewIntVar(-BIG, BIG, k) for k in ("minL", "maxR", "minT", "maxB"))
    for r in X:
        m.Add(minL <= Lf[r])
        m.Add(maxR >= Rt[r])
        m.Add(minT <= To[r])
        m.Add(maxB >= Bo[r])
    cost.append(round(S * W.extent * G) * ((maxR - minL) + (maxB - minT)))
    m.Minimize(sum(cost))
    sv = cp_model.CpSolver()
    p = sv.parameters
    p.num_workers = 1
    p.max_deterministic_time = seconds
    p.random_seed = 0
    p.max_time_in_seconds = 6 * seconds        # safety net only
    t = time.perf_counter()
    status = sv.Solve(m)
    wall = time.perf_counter() - t
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, wall, sv.StatusName(status), npair
    pose = {}
    for s in symbols:
        r = s.reference
        if not s.movable:
            pose[r] = start[r]
            continue
        x, y = sv.Value(X[r]), sv.Value(Y[r])
        o = next(o for o, lit in xo[r].items() if sv.Value(lit))
        pose[r] = (s.position if (x, y) == sc[r] else (x * G, y * G), o)
    return pose, wall, f"{sv.StatusName(status)} obj={sv.ObjectiveValue():.0f} bound={sv.BestObjectiveBound():.0f}", npair


def posed(symbols, pose):
    return [replace(s, position=pose[s.reference][0], orientation=pose[s.reference][1]) for s in symbols]


def main():
    OUT.mkdir(exist_ok=True)
    circuits = pr.load_circuits(report.resolve_run("auto-st3"))
    lines = []
    for gi in GROUPS:
        c = circuits[gi]
        call = c["refine"]
        symbols, nets = list(call["args"][0]), call["args"][1]
        reach = call["kwargs"].get("reach") or {}
        hw = call["kwargs"].get("half_widths")
        score = pe._Model(symbols, nets, reach, W, hw)    # one scorer for every variant, relative to the recorded start
        start = {s.reference: (s.position, s.orientation) for s in symbols}

        def row(pose):
            k = score.cost(pose)
            return dict(total=k.total, mm=k.length, bends=k.bends, jogs=k.jogs, cross=k.crossings, through=k.through, over=k.overlaps)

        res = {"a start": (row(start), 0.0, start)}
        t = time.perf_counter()
        g = pr.replay(c, rounds=2, orient=0)
        tg = time.perf_counter() - t
        gp = {s.reference: (s.position, s.orientation) for s in g.symbols}
        res["b greedy"] = (row(gp), tg, gp)
        cp, tc, status, npair = solve(symbols, nets, reach, hw)
        if cp is None:
            print(gi, "CP-SAT failed", status, flush=True)
            continue
        res["c cpsat"] = (row(cp), tc, cp)
        t = time.perf_counter()
        out = pe.refine_placement(posed(symbols, cp), nets, **{**call["kwargs"], "weights": W})
        tp = time.perf_counter() - t
        dp = dict(cp)
        for r, pl in out.placements.items():
            dp[r] = (pl.position, pl.orientation)
        res["d cpsat+polish"] = (row(dp), tc + tp, dp)
        for name, (_r, _t, pose) in res.items():
            f = OUT / f"g{gi}_{name[0]}.svg"
            f.write_text(pr.svg(pr.Replay(symbols=posed(symbols, pose), nets=nets, model=score), f"group {gi} {name}"))
            if name[0] in "bd":
                w, h = (int(v) for v in re.search(r'width="(\d+)" height="(\d+)"', f.read_text()).groups())
                subprocess.run([CHROME, "--headless=new", "--disable-gpu", f"--screenshot={f.with_suffix('.png')}",
                                f"--window-size={w},{h}", f.resolve().as_uri()], capture_output=True)
        lines.append(f"\n### group {gi}: {len(symbols)} symbols, {sum(s.movable for s in symbols)} movable, {len(score.nets)} nets, "
                     f"{npair} wire pairs; CP-SAT {status}\n\n| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |\n|---|---|---|---|---|---|---|---|---|")
        for name, (r, t, _p) in res.items():
            lines.append(f"| {name} | {r['total']:.1f} | {r['mm']:.1f} | {r['bends']:.0f} | {r['jogs']} | {r['cross']} | {r['through']} | {r['over']} | {t:.1f} |")
        print("\n".join(lines[-7:]), flush=True)
    (Path(__file__).parent / "results_raw.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
