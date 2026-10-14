"""Where do humans put the Reference and Value texts of two-pin parts?

For every placed R/C/L/D symbol (power symbols skipped) in KiCad's demo schematics,
records which side of the part anchor the Reference and Value texts sit on, split by
orientation (vertical = rotation 0/180, horizontal = 90/270) and by class.

Side rule (page y grows down): left if dx < -0.5 mm, right if dx > 0.5 mm, else
above if dy < 0, else below. Hidden properties (effects hide yes) are excluded from
that field's side statistics and reported separately.

Run: PYTHONPATH=. scripts/ci/sandbox/.work/venv/Scripts/python.exe scripts/ci/sandbox/probes/field_sides/measure.py
Writes out/fields.csv (one row per symbol and field) and out/summary.md next to this file.
Parsing helpers are imported read-only from ../conventions/extract.py.
"""
from __future__ import annotations

import csv
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "conventions"))
from extract import kid, kids, parse_sexp, val  # noqa: E402  (read-only reuse)

OUT = HERE / "out"
DEMOS = Path(r"C:\Program Files\KiCad\10.0\share\kicad\demos")
CLASSES = {"R": "resistor", "C": "capacitor", "L": "inductor", "D": "diode"}
SIDES = ["left", "right", "above", "below"]
FIELDS = ["Reference", "Value"]
ORIENTS = ["vertical", "horizontal"]
CLS_KEYS = ["R", "C", "L", "D"]


# ---------------------------------------------------------------- rules
def orient_of(rot: int) -> str:
    if rot in (0, 180):
        return "vertical"
    if rot in (90, 270):
        return "horizontal"
    return "other"


def side_of(dx: float, dy: float) -> str:
    if dx < -0.5:
        return "left"
    if dx > 0.5:
        return "right"
    return "above" if dy < 0 else "below"


def find_prop(sym, name):
    for p in kids(sym, "property"):
        if len(p) > 2 and val(p[1]) == name:
            return p
    return None


def hidden(prop) -> bool:
    eff = kid(prop, "effects") or []
    for node in (prop, eff):  # older files put (hide yes) directly on the property
        h = kid(node, "hide")
        if h is not None and len(h) > 1 and val(h[1]) == "yes":
            return True
    return False


def lib_info(ls) -> dict:
    """lib name -> (is_power, pin count over style-1 sub-symbols)."""
    info = {}
    for s in kids(ls or [], "symbol"):
        power = kid(s, "power") is not None
        npins = 0
        for sub in kids(s, "symbol"):
            m = re.search(r"_(\d+)_(\d+)$", val(sub[1]))
            if m and int(m.group(2)) > 1:
                continue
            npins += len(kids(sub, "pin"))
        info[val(s[1])] = (power, npins)
    return info


# ---------------------------------------------------------------- scan
def scan():
    recs, stats = [], Counter()
    files = sorted(DEMOS.rglob("*.kicad_sch"))
    stats["sheets"] = len(files)
    for f in files:
        rel = f.relative_to(DEMOS).as_posix()
        tree = parse_sexp(f.read_text(encoding="utf-8", errors="replace"))
        libs = lib_info(kid(tree, "lib_symbols"))
        for s in kids(tree, "symbol"):
            stats["symbols"] += 1
            lid = kid(s, "lib_id")
            ref_p = find_prop(s, "Reference")
            at = kid(s, "at")
            if lid is None or ref_p is None or at is None or len(ref_p) < 3:
                continue
            ref = val(ref_p[2])
            cls = ref[:1]
            if cls not in CLASSES:
                continue
            stats["rcld_placed"] += 1
            lib_id = val(lid[1])
            power, npins = libs.get(lib_id, (False, -1))
            if power:
                stats["power_skipped"] += 1
                continue
            x, y = float(at[1]), float(at[2])
            rot = int(float(at[3])) % 360 if len(at) > 3 else 0
            rec = dict(sheet=rel, ref=ref, cls=cls, lib_id=lib_id, npins=npins, rot=rot,
                       orient=orient_of(rot), x=x, y=y, fields={})
            for field in FIELDS:
                p = find_prop(s, field)
                pat = kid(p, "at") if p is not None else None
                if pat is None:
                    rec["fields"][field] = None
                    continue
                px, py = float(pat[1]), float(pat[2])
                dx, dy = px - x, py - y
                rec["fields"][field] = dict(
                    text=val(p[2]) if len(p) > 2 else "", hidden=hidden(p),
                    side=side_of(dx, dy), dx=dx, dy=dy, dist=math.hypot(dx, dy))
            recs.append(rec)
    return recs, stats


# ---------------------------------------------------------------- aggregation
def pct(k: int, n: int) -> str:
    return f"{100 * k / n:.0f}%" if n else "-"


ANCHOR_MM = 0.01


def items(recs, field, orient=None, cls=None, pred=lambda r: True, anchor=None):
    """Visible (not hidden) field records matching the filters.

    anchor: None = all, "only" = text within ANCHOR_MM of the symbol anchor,
            "exclude" = everything else.
    """
    out = []
    for r in recs:
        if not pred(r) or (orient and r["orient"] != orient) or (cls and r["cls"] != cls):
            continue
        f = r["fields"].get(field)
        if not f or f["hidden"]:
            continue
        at = f["dist"] < ANCHOR_MM
        if (anchor == "only" and not at) or (anchor == "exclude" and at):
            continue
        out.append(f)
    return out


def share_row(prefix: str, its) -> str:
    n = len(its)
    c = Counter(f["side"] for f in its)
    cells = [f"{pct(c[s], n)} ({c[s]})" for s in SIDES]
    return f"| {prefix} | {n} | " + " | ".join(cells) + " |"


def dominant(its) -> str:
    if not its:
        return "-"
    s, k = Counter(f["side"] for f in its).most_common(1)[0]
    return f"{s} {pct(k, len(its))} (n={len(its)})"


def pairs(recs, orient, pred=lambda r: True):
    out = []
    for r in recs:
        if r["orient"] != orient or not pred(r):
            continue
        a, b = r["fields"]["Reference"], r["fields"]["Value"]
        if a and b and not a["hidden"] and not b["hidden"]:
            out.append((a["side"], b["side"]))
    return out


def build_summary(recs, stats) -> list[str]:
    L: list[str] = []
    add = L.append
    add("# Reference / Value side for two-pin parts (KiCad 10 demos)")
    add("")
    add(f"Sheets scanned: {stats['sheets']}. Symbols: {stats['symbols']}. "
        f"R/C/L/D placed: {stats['rcld_placed']}. Power skipped: {stats['power_skipped']}. "
        f"Measured: {len(recs)}.")
    add("Counts are per placed symbol across all demo files; copies of one design are counted each time.")
    add("")
    add("Side rule: dx < -0.5 mm left, dx > 0.5 mm right, else above if dy < 0 else below (page y down). "
        "Vertical = rotation 0/180, horizontal = 90/270, other rotations excluded from orientation tables. "
        "A hidden property is excluded from that field's side shares and pair counts.")
    add("")
    by_orient = Counter(r["orient"] for r in recs)
    add("Orientation: " + ", ".join(f"{k} {v}" for k, v in sorted(by_orient.items())))
    add("Class counts: " + ", ".join(f"{c} {sum(1 for r in recs if r['cls'] == c)}" for c in CLS_KEYS))
    add("npins of measured symbols: " + ", ".join(
        f"{k}: {v}" for k, v in sorted(Counter(r["npins"] for r in recs).items())))
    add("")

    # shares
    add("## Side shares (visible texts; share and count)")
    add("")
    add("| orient | field | class | n | left | right | above | below |")
    add("|---|---|---|---|---|---|---|---|")
    for orient in ORIENTS:
        for field in FIELDS:
            for cls in [None] + CLS_KEYS:
                add(share_row(f"{orient} | {field} | {cls or 'all'}", items(recs, field, orient, cls)))
    add("")

    # same side
    add("## Reference vs Value on the same side (both visible)")
    add("")
    for orient in ORIENTS:
        ps = pairs(recs, orient)
        n = len(ps)
        same = sum(1 for a, b in ps if a == b)
        if orient == "vertical":
            both = sum(1 for a, b in ps if a == b == "right")
            add(f"- vertical: pairs {n}; same side {same} ({pct(same, n)}); both right {both} ({pct(both, n)}).")
        else:
            ba = sum(1 for a, b in ps if a == b == "above")
            bb = sum(1 for a, b in ps if a == b == "below")
            add(f"- horizontal: pairs {n}; same side {same} ({pct(same, n)}); both above {ba} ({pct(ba, n)}); "
                f"both below {bb} ({pct(bb, n)}); above-or-below both {ba + bb} ({pct(ba + bb, n)}).")
    add("")

    # dominant per orientation, all vs 2-pin
    add("## Dominant side per orientation (all measured vs npins == 2)")
    add("")
    add("| orient | field | all | 2-pin only |")
    add("|---|---|---|---|")
    for orient in ORIENTS:
        for field in FIELDS:
            add(f"| {orient} | {field} | {dominant(items(recs, field, orient))} | "
                f"{dominant(items(recs, field, orient, pred=lambda r: r['npins'] == 2))} |")
    add("")

    # at-anchor sensitivity
    add("## Texts sitting exactly on the anchor (dist < 0.01 mm) and shares without them")
    add("")
    add("| orient | field | at anchor / visible | shares excluding at-anchor: left | right | above | below |")
    add("|---|---|---|---|---|---|---|")
    for orient in ORIENTS:
        for field in FIELDS:
            vis = items(recs, field, orient)
            at = items(recs, field, orient, anchor="only")
            rest = items(recs, field, orient, anchor="exclude")
            c = Counter(f["side"] for f in rest)
            n = len(rest)
            cells = " | ".join(f"{pct(c[s], n)} ({c[s]})" for s in SIDES)
            add(f"| {orient} | {field} | {len(at)} / {len(vis)} | {cells} |")
    add("")
    at_sheets = Counter(r["sheet"] for r in recs for fld in FIELDS
                        if r["fields"].get(fld) and not r["fields"][fld]["hidden"]
                        and r["fields"][fld]["dist"] < ANCHOR_MM)
    add("Sheets with the most at-anchor texts: " + ", ".join(
        f"{s} ({k})" for s, k in at_sheets.most_common(8)) + ".")
    add("")

    # axis caveat
    add("## On-axis texts (|dx| <= 0.5 mm, classed above/below by dy alone)")
    add("")
    for orient in ORIENTS:
        for field in FIELDS:
            its = items(recs, field, orient)
            on = sum(1 for f in its if abs(f["dx"]) <= 0.5)
            add(f"- {orient} {field}: {on} of {len(its)} ({pct(on, len(its))})")
    add("")

    # distance
    add("## Median offset from symbol anchor, by side (mm, visible texts)")
    add("")
    add("| field | orient | side | n | median dist |")
    add("|---|---|---|---|---|")
    for field in FIELDS:
        for orient in ORIENTS:
            for side in SIDES:
                ds = [f["dist"] for f in items(recs, field, orient) if f["side"] == side]
                med = f"{statistics.median(ds):.2f}" if ds else "-"
                add(f"| {field} | {orient} | {side} | {len(ds)} | {med} |")
    add("")

    # hidden
    add("## Hidden texts")
    add("")
    add("| field | class | hidden | total | share |")
    add("|---|---|---|---|---|")
    for field in FIELDS:
        for cls in CLS_KEYS:
            tot = [r["fields"][field] for r in recs if r["cls"] == cls and r["fields"].get(field)]
            h = sum(1 for f in tot if f["hidden"])
            add(f"| {field} | {cls} | {h} | {len(tot)} | {pct(h, len(tot))} |")
    add("")
    per_sheet = defaultdict(Counter)
    for r in recs:
        c = per_sheet[r["sheet"]]
        c["syms"] += 1
        v = r["fields"]["Value"]
        if v and v["hidden"]:
            c["val_hidden"] += 1
        rf = r["fields"]["Reference"]
        if rf and rf["hidden"]:
            c["ref_hidden"] += 1
    with_hidden = {s: c for s, c in per_sheet.items() if c["val_hidden"]}
    all_hidden = [s for s, c in with_hidden.items() if c["val_hidden"] == c["syms"]]
    add(f"Sheets with any hidden Value: {len(with_hidden)} of {len(per_sheet)}; "
        f"sheets where every Value is hidden: {len(all_hidden)}.")
    add("")
    if with_hidden:
        add("| sheet | Value hidden / symbols | Reference hidden / symbols |")
        add("|---|---|---|")
        for s, c in sorted(with_hidden.items(), key=lambda kv: -kv[1]["val_hidden"])[:40]:
            add(f"| {s} | {c['val_hidden']} / {c['syms']} | {c['ref_hidden']} / {c['syms']} |")
        if len(with_hidden) > 40:
            add(f"| ... {len(with_hidden) - 40} more sheets | | |")
    add("")
    return L


def main():
    recs, stats = scan()
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "fields.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sheet", "ref", "class", "lib_id", "npins", "rot", "orient", "field", "text",
                    "hidden", "side", "dx_mm", "dy_mm", "dist_mm", "sym_x", "sym_y"])
        for r in recs:
            for field in FIELDS:
                f = r["fields"][field]
                if f is None:
                    continue
                w.writerow([r["sheet"], r["ref"], r["cls"], r["lib_id"], r["npins"], r["rot"], r["orient"],
                            field, f["text"], int(f["hidden"]), f["side"], round(f["dx"], 3),
                            round(f["dy"], 3), round(f["dist"], 3), r["x"], r["y"]])
    (OUT / "summary.md").write_text("\n".join(build_summary(recs, stats)), encoding="utf-8")
    print(f"measured {len(recs)} symbols; wrote {OUT / 'summary.md'} and {OUT / 'fields.csv'}")


if __name__ == "__main__":
    main()
