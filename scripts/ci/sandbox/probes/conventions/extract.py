"""Measure how humans orient parts in KiCad's demo schematics.

Run: PYTHONPATH=. scripts/ci/sandbox/.work/venv/Scripts/python.exe scripts/ci/sandbox/probes/conventions/extract.py [--no-render]
Writes out/parts.csv, out/summary.md, out/png/*.png, out/png/manifest.json (next to this file).
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
DEMOS = Path(r"C:\Program Files\KiCad\10.0\share\kicad\demos")
CLI = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
REQUIRED = [  # sbx/demos.py
    "simulation/power_supplies/LM317_power_supply/power_supply.kicad_sch",
    "simulation/power_supplies/buck_conv/buck_conv.kicad_sch",
    "simulation/amplifier-ac/amplifier-ac.kicad_sch",
    "simulation/generic_models/generic_opamp_bip.kicad_sch",
]


# ---------------------------------------------------------------- s-expressions
_TOK = re.compile(r'"((?:[^"\\]|\\.)*)"|([()])|([^\s()"]+)')


def parse_sexp(text: str):
    stack: list[list] = [[]]
    for m in _TOK.finditer(text):
        if m.group(1) is not None:
            stack[-1].append(("s", m.group(1)))  # quoted string marker
        elif m.group(2) == "(":
            stack.append([])
        elif m.group(2) == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(m.group(3))
    return stack[0][0]


def val(x):
    return x[1] if isinstance(x, tuple) else x


def kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def kid(node, name):
    r = kids(node, name)
    return r[0] if r else None


# ---------------------------------------------------------------- geometry
def vec_for(angle: int) -> tuple[int, int]:
    """Library angle (y up, CCW) -> unit vector in y-up space."""
    a = angle % 360
    return {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[a]


def transform(v, rot: int, mirror: str):
    """v in y-up lib space. Rotate CCW by rot, then mirror (x flips y, y flips x). Returns y-up vector."""
    x, y = v
    for _ in range((rot // 90) % 4):
        x, y = -y, x
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return x, y


def dir_name(v) -> str:
    x, y = v  # y-up
    if abs(x) >= abs(y):
        return "right" if x > 0 else "left"
    return "up" if y > 0 else "down"


def page_dir(dx, dy) -> str:  # page vector (y down)
    return dir_name((dx, -dy))


# ---------------------------------------------------------------- classification
GROUND = re.compile(r"^(?:[ADPSC]?GND\w*|VSS\w*|EARTH\w*|0)$", re.I)


def rail_kind(name: str) -> str:
    n = name.rsplit("/", 1)[-1]
    if GROUND.match(n):
        return "ground"
    if n.startswith("-") or re.match(r"^VEE", n, re.I):
        return "negative"
    return "supply"


def ref_prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z#]+", ref)
    return m.group(0) if m else ""


def classify(lib_id: str, ref: str, pin_names: set[str]) -> str:
    lib, _, name = lib_id.rpartition(":")
    pre = ref_prefix(ref).upper()
    nl = name.lower()
    if lib.startswith("Regulator_"):
        return "regulator"
    if lib.startswith("Amplifier_Operational") or "opamp" in nl:
        return "opamp"
    if pre == "T" or lib.startswith("Transformer") or nl.startswith("transformer"):
        return "transformer"
    if "bridge" in nl:
        return "bridge"
    if re.search(r"(^|_)led", nl) or pre == "LED":
        return "led"
    if pre == "Q" or lib.startswith("Transistor"):
        if "BJT" in lib or {"B", "C", "E"} <= pin_names or nl in ("npn", "pnp"):
            return "transistor_bjt"
        if "FET" in lib or "MOS" in lib or {"G", "D", "S"} <= pin_names or "mos" in nl:
            return "transistor_fet"
        return "other"
    if pre == "D" or lib.startswith("Diode"):
        return "diode"
    if pre == "CP" or "polarized" in nl or re.match(r"cp(_|$)", nl):
        return "capacitor_polarized"
    if pre == "C":
        return "capacitor"
    if pre == "R" and "pot" not in nl:
        return "resistor"
    if pre == "L":
        return "inductor"
    if pre in ("V", "I", "BT"):
        return "source"
    if pre in ("J", "P") or lib.startswith("Conn"):
        return "connector"
    if pre == "SW":
        return "switch"
    if pre == "Y":
        return "crystal"
    if pre == "U":
        return "ic"
    return "other"


# ---------------------------------------------------------------- netlists
def netlist_for(root_sch: Path, cache: Path) -> dict[tuple[str, str], tuple[str, int]]:
    cache.mkdir(parents=True, exist_ok=True)
    xml = cache / (root_sch.parent.name + "__" + root_sch.stem + ".xml")
    if not xml.exists():
        subprocess.run([CLI, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml), str(root_sch)],
                       capture_output=True, timeout=180)
    m: dict[tuple[str, str], tuple[str, int]] = {}
    if not xml.exists():
        return m
    for net in ET.parse(xml).getroot().iter("net"):
        nodes = net.findall("node")
        name = net.get("name", "")
        for n in nodes:
            m[(n.get("ref"), n.get("pin"))] = (name, len(nodes))
    return m


# ---------------------------------------------------------------- sheet reading
def read_sheet(path: Path):
    tree = parse_sexp(path.read_text(encoding="utf-8", errors="replace"))
    libs = {}
    ls = kid(tree, "lib_symbols")
    for s in kids(ls or [], "symbol"):
        name = val(s[1])
        is_power = kid(s, "power") is not None
        pins = []  # (unit, number, name, x, y, angle, etype)
        for sub in kids(s, "symbol"):
            m = re.search(r"_(\d+)_(\d+)$", val(sub[1]))
            unit, style = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
            if style > 1:
                continue
            for p in kids(sub, "pin"):
                at = kid(p, "at")
                pins.append((unit, val(kid(p, "number")[1]), val(kid(p, "name")[1]),
                             float(at[1]), float(at[2]), int(float(at[3])), p[1]))
        libs[name] = (is_power, pins)
    placed = []
    for s in kids(tree, "symbol"):
        if kid(s, "lib_id") is None:
            continue
        lib_id = val(kid(s, "lib_id")[1])
        at = kid(s, "at")
        mir = kid(s, "mirror")
        props = {val(p[1]): val(p[2]) for p in kids(s, "property")}
        refs = [props.get("Reference", "")]
        inst = kid(s, "instances")
        if inst:
            for pr in kids(inst, "project"):
                for pa in kids(pr, "path"):
                    r = kid(pa, "reference")
                    if r:
                        refs.append(val(r[1]))
        unit = kid(s, "unit")
        placed.append(dict(lib_id=lib_id, x=float(at[1]), y=float(at[2]), rot=int(float(at[3])) % 360,
                           mirror=val(mir[1]) if mir else "none", unit=int(unit[1]) if unit else 1,
                           refs=refs, value=props.get("Value", "")))
    return libs, placed


def project_dir(sh: Path, roots) -> Path:
    d = sh.parent
    while d != DEMOS and d not in roots and d != d.parent:
        d = d.parent
    return d


def main():
    render = "--no-render" not in sys.argv
    OUT.mkdir(exist_ok=True)
    sheets = sorted(DEMOS.rglob("*.kicad_sch"))
    roots = {}
    for pro in DEMOS.rglob("*.kicad_pro"):
        r = pro.with_suffix(".kicad_sch")
        if r.exists():
            roots[pro.parent] = r
    data = {}
    power_names: dict[Path, set[str]] = defaultdict(set)
    for sh in sheets:
        libs, placed = read_sheet(sh)
        data[sh] = (libs, placed)
        d = project_dir(sh, roots)
        for p in placed:
            lib = libs.get(p["lib_id"])
            if lib and lib[0] and not p["lib_id"].endswith("PWR_FLAG"):
                power_names[d].add(p["value"])
    nets_cache: dict[Path, dict] = {}
    rows, skipped, nsyms = [], Counter(), {}
    for sh in sheets:
        libs, placed = data[sh]
        d = project_dir(sh, roots)
        if d not in nets_cache:
            nets_cache[d] = netlist_for(roots[d], OUT / "netlists") if d in roots else {}
        nl = nets_cache[d]
        rails = power_names[d]
        nsyms[sh] = len(placed)
        rel = sh.relative_to(DEMOS).as_posix()
        for p in placed:
            is_power, pins = libs.get(p["lib_id"], (False, []))
            if is_power or p["lib_id"].endswith("PWR_FLAG"):
                continue
            ref = next((r for r in p["refs"] if any(k[0] == r for k in nl)), None) if nl else None
            if ref is None:
                skipped[rel] += 1
                continue
            mypins = [q for q in pins if q[0] in (0, p["unit"])]
            pn = {q[2] for q in mypins}
            cls = classify(p["lib_id"], ref, pn)
            info = []
            for (_, num, name, px, py, ang, et) in mypins:
                out = transform(vec_for(ang + 180), p["rot"], p["mirror"])
                lx, ly = transform((px, py), p["rot"], p["mirror"])
                X, Y = p["x"] + lx, p["y"] - ly
                net = nl.get((ref, num))
                if net is None or et == "no_connect" or net[1] < 2 or net[0].startswith("unconnected-"):
                    kind, nname = "nc", net[0] if net else ""
                else:
                    nname = net[0]
                    stripped = nname.rsplit("/", 1)[-1]
                    kind = rail_kind(nname) if (nname in rails or stripped in rails) else "signal"
                info.append(dict(num=num, name=name, dir=dir_name(out), X=X, Y=Y, kind=kind, net=nname))
            row = dict(sheet=rel, ref=ref, lib_id=p["lib_id"], unit=p["unit"], rotation=p["rot"],
                       mirror=p["mirror"], cls=cls, npins=len(info), context="multipin", axis="",
                       rail_pin_dir="", ac_dir="", e_dir="", c_dir="", b_dir="", emitter_lower="", rail_pins="")
            row["rail_pins"] = "|".join(f"{i['name']}:{i['kind']}:{i['dir']}" for i in info
                                        if i["kind"] in ("ground", "supply", "negative"))
            gs = [i for i in info if i["kind"] == "ground"] or [i for i in info if i["kind"] in ("supply", "negative")]
            if gs:
                row["rail_pin_dir"] = gs[0]["dir"]
            if len(info) == 2:
                a, b = info
                ks = sorted([a["kind"], b["kind"]])
                rl = {"ground", "supply", "negative"}
                if "nc" in ks:
                    ctx = "other"
                elif ks == ["signal", "signal"]:
                    ctx = "series"
                elif a["kind"] in rl and b["kind"] in rl:
                    ctx = "across_rails"
                elif "ground" in ks and "signal" in ks:
                    ctx = "shunt_ground"
                elif "signal" in ks:
                    ctx = "shunt_supply"
                else:
                    ctx = "other"
                row["context"] = ctx
                dx, dy = b["X"] - a["X"], b["Y"] - a["Y"]
                row["axis"] = "horizontal" if abs(dx) >= abs(dy) else "vertical"
                if cls in ("diode", "led", "capacitor_polarized"):
                    anode = next((i for i in info if i["name"] == "+" or i["name"].startswith("A")), None)
                    cath = next((i for i in info if i["name"] == "-" or i["name"].startswith("K")), None)
                    if anode and cath and anode is not cath:
                        row["ac_dir"] = page_dir(cath["X"] - anode["X"], cath["Y"] - anode["Y"])
            if cls in ("transistor_bjt", "transistor_fet"):
                names = ("E", "C", "B") if cls == "transistor_bjt" else ("S", "D", "G")
                by = {i["name"]: i for i in info}
                if all(n in by for n in names):
                    row["e_dir"], row["c_dir"], row["b_dir"] = (by[n]["dir"] for n in names)
                    row["emitter_lower"] = str(by[names[0]]["Y"] > by[names[1]]["Y"]).lower()
                else:
                    row["e_dir"] = "pins:" + "/".join(i["num"] for i in info)
            rows.append(row)
    with open(OUT / "parts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    write_summary(rows, len(sheets), skipped)
    if render:
        render_sheets(nsyms)
    print(f"sheets={len(sheets)} parts={len(rows)} skipped={sum(skipped.values())}")


def pct(c: Counter) -> str:
    t = sum(c.values())
    return ", ".join(f"{k or '-'} {100 * v / t:.0f}%" for k, v in c.most_common())


def write_summary(rows, nsheets, skipped):
    g = defaultdict(list)
    for r in rows:
        g[(r["cls"], r["context"])].append(r)
    L = ["# Orientation conventions in KiCad demos", "",
         f"Sheets: {nsheets}; parts read: {len(rows)}; symbols skipped (not in netlist): {sum(skipped.values())}", "",
         "| class | context | n | features |", "|---|---|---|---|"]
    for (cls, ctx), rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        feats = []
        for col, label in (("axis", "axis"), ("rail_pin_dir", "rail_pin_dir"), ("ac_dir", "anode->cathode"),
                           ("emitter_lower", "emitter_lower"), ("e_dir", "emitter_dir")):
            vals = Counter(r[col] for r in rs if r[col] != "")
            if vals:
                feats.append(f"{label}: {pct(vals)}")
        if ctx == "multipin":
            feats.append("rot/mirror: " + pct(Counter(f"{r['rotation']}/{r['mirror']}" for r in rs)))
        L.append(f"| {cls} | {ctx} | {len(rs)} | {'; '.join(feats)} |")
    (OUT / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- rendering
def render_sheets(nsyms):
    png = OUT / "png"
    png.mkdir(exist_ok=True)
    svgs = OUT / "svg"
    svgs.mkdir(exist_ok=True)
    by_dir = defaultdict(list)
    for sh, n in nsyms.items():
        if 10 <= n <= 120:
            by_dir[sh.parent].append((abs(n - 50), str(sh), n))
    chosen = [DEMOS / r for r in REQUIRED]
    queues = [sorted(v) for _, v in sorted(by_dir.items(), key=lambda kv: str(kv[0]))]
    while len(chosen) < 40 and any(queues):
        for q in queues:
            if q and len(chosen) < 40:
                sh = Path(q.pop(0)[1])
                if sh not in chosen:
                    chosen.append(sh)
    manifest, names = [], set()
    for sh in chosen:
        base = re.sub(r"[^A-Za-z0-9_-]", "_", f"{sh.parent.name}__{sh.stem}")
        if base in names:
            continue
        names.add(base)
        d = svgs / base
        d.mkdir(exist_ok=True)
        subprocess.run([CLI, "sch", "export", "svg", "--exclude-drawing-sheet", "--no-background-color",
                        "-o", str(d), str(sh)], capture_output=True, timeout=180)
        cand = d / (sh.stem + ".svg")
        if not cand.exists():
            found = sorted(d.glob("*.svg"))
            if not found:
                continue
            cand = found[0]
        head = cand.read_text(encoding="utf-8", errors="replace")[:600]
        mw = re.search(r'width="([\d.]+)mm"', head)
        mh = re.search(r'height="([\d.]+)mm"', head)
        ratio = float(mh.group(1)) / float(mw.group(1)) if mw and mh else 0.7
        H = min(max(int(1600 * ratio) + 4, 200), 4000)
        html = d / "v.html"
        html.write_text(f'<html><body style="margin:0;background:#fff"><img src="{cand.name}" '
                        f'style="width:1600px;display:block"></body></html>')
        out = png / f"{base}.png"
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--screenshot={out}",
                        f"--window-size=1600,{H}", html.as_uri()], capture_output=True, timeout=120)
        if out.exists():
            manifest.append(dict(name=base, sheet_path=str(sh), png_path=str(out), symbols=nsyms[sh]))
    (png / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print("rendered", len(manifest))


if __name__ == "__main__":
    main()
