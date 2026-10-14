"""Order-insensitive comparison of two sandbox run dirs, per fixture.

usage: compare.py RUN_A RUN_B [--trials]
Compares final sheet (symbols, wires, labels, junctions, field positions) and
quality-selection.json outcomes; with --trials also each quality-trials/NN-*/ sheet.
Ignores UUIDs, timestamps, and #PWR/#FLG numbering (keyed by lib_id + position).
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

TOK = re.compile(r'[()]|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse(text):
    stack, cur = [], []
    for t in TOK.findall(text):
        if t == "(":
            stack.append(cur); cur = []
        elif t == ")":
            done, cur = cur, stack.pop(); cur.append(done)
        else:
            cur.append(t[1:-1] if t.startswith('"') else t)
    return cur[0] if cur else []


def sub(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def xy(node):
    a = sub(node, "at")
    return tuple(round(float(v), 3) for v in a[0][1:4]) if a else None


def sheet(path):
    root = parse(Path(path).read_text(encoding="utf-8"))
    out = {"symbols": [], "wires": set(), "labels": [], "junctions": []}
    for n in root:
        if not isinstance(n, list) or not n:
            continue
        k = n[0]
        if k == "symbol" and sub(n, "lib_id"):
            lib = sub(n, "lib_id")[0][1]
            ref = next((p[2] for p in sub(n, "property") if p[1] == "Reference"), "?")
            mir = tuple(sub(n, "mirror")[0][1:]) if sub(n, "mirror") else ()
            if ref.startswith(("#PWR", "#FLG")):
                ref = ref[:4]
            fields = tuple(sorted((p[1], ref if p[1] == "Reference" else p[2], xy(p)) for p in sub(n, "property") if xy(p)))
            out["symbols"].append((lib, xy(n), mir, ref, fields))
        elif k == "wire":
            pts = [tuple(round(float(v), 3) for v in p[1:3]) for p in sub(sub(n, "pts")[0], "xy")]
            out["wires"].add(tuple(sorted(pts)))
        elif k in ("label", "global_label", "hierarchical_label"):
            out["labels"].append((k, n[1], xy(n)))
        elif k == "junction":
            out["junctions"].append(xy(n))
    for key in ("symbols", "labels", "junctions"):
        out[key].sort(key=repr)
    return out


def diff(name, a, b, out):
    sa, sb = (set(map(repr, a)), set(map(repr, b))) if not isinstance(a, set) else (set(map(repr, a)), set(map(repr, b)))
    if isinstance(a, list):
        from collections import Counter
        ca, cb = Counter(map(repr, a)), Counter(map(repr, b))
        oa, ob = list((ca - cb).elements()), list((cb - ca).elements())
    else:
        oa, ob = sorted(sa - sb), sorted(sb - sa)
    for x in oa[:6]: out.append(f"  {name} only A: {x}")
    for x in ob[:6]: out.append(f"  {name} only B: {x}")
    if len(oa) > 6 or len(ob) > 6: out.append(f"  {name}: {len(oa)} only-A / {len(ob)} only-B total")


def cmp_sheet(pa, pb):
    out = []
    if not pa.exists() or not pb.exists():
        return [f"  missing sheet: {pa.exists()} / {pb.exists()}"]
    A, B = sheet(pa), sheet(pb)
    for k in A:
        diff(k, A[k], B[k], out)
    return out


def selection(p):
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return json.dumps(d, sort_keys=True, default=str)


def main():
    ra, rb = Path(sys.argv[1]), Path(sys.argv[2])
    trials = "--trials" in sys.argv
    for fa in sorted((ra / "artifacts").iterdir()):
        fb = rb / "artifacts" / fa.name
        print(f"== {fa.name}")
        res = cmp_sheet(fa / "mcu_smoke.kicad_sch", fb / "mcu_smoke.kicad_sch")
        print("\n".join(res) if res else "  final sheet identical")
        sa, sb = selection(fa / "quality-selection.json"), selection(fb / "quality-selection.json")
        if sa == sb:
            print("  quality-selection identical")
        else:
            print("  quality-selection DIFFERS")
            if sa and sb:
                ja, jb = json.loads(sa), json.loads(sb)
                for k in sorted(set(ja) | set(jb)):
                    if ja.get(k) != jb.get(k):
                        print(f"    key {k}: {str(ja.get(k))[:160]} | {str(jb.get(k))[:160]}")
        if trials:
            for t in sorted((fa / "quality-trials").glob("*")):
                for s in t.glob("*.kicad_sch"):
                    r = cmp_sheet(s, fb / "quality-trials" / t.name / s.name)
                    print(f"  trial {t.name}: " + ("identical" if not r else "DIFFERS"))
                    if r: print("\n".join(r[:8]))


if __name__ == "__main__":
    main()
