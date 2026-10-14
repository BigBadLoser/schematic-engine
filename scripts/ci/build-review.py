#!/usr/bin/env python3
"""Build a side-by-side before/after HTML review of two harness runs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import cairosvg
except Exception:  # pragma: no cover - only needed when rasterizing
    cairosvg = None


NUMERIC_KEYS = (
    ("composition_metrics", "wire_length_mm"),
    ("composition_metrics", "wire_corner_count"),
    ("composition_metrics", "wire_segment_count"),
    ("composition_metrics", "drawing_width_mm"),
    ("composition_metrics", "drawing_height_mm"),
)


def find_gate_status(report: dict, needle: str):
    hits: list = []

    def walk(node) -> None:
        if isinstance(node, dict):
            gate = node.get("gate") or node.get("gate_name")
            if isinstance(gate, str) and needle.lower() in gate.lower():
                hits.append(node.get("status") or node.get("verdict"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(report)
    return hits[0] if hits else None


def summarize(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    workflow = report.get("workflow") or {}
    cosmetic = workflow.get("cosmetic_score") or {}
    summary = {
        "cosmetic_score": cosmetic.get("cosmetic_score"),
        "cosmetic_status": cosmetic.get("status"),
        "connectivity": find_gate_status(report, "connectivity"),
    }
    for section, key in NUMERIC_KEYS:
        summary[f"{section}.{key}"] = (report.get(section) or {}).get(key)
    return summary


def load_root(root_arg: str) -> dict:
    root = Path(root_arg)
    if (root / ".kicad-ai").is_dir():
        root = root / ".kicad-ai"
    fixtures: dict = {}
    for directory in sorted(root.glob("schematic-engine-*")):
        report_path = directory / "report.json"
        svg = next(iter(sorted(directory.glob("*.svg"))), None)
        if report_path.is_file():
            fixtures[directory.name.replace("schematic-engine-", "")] = {
                "dir": directory,
                "summary": summarize(report_path),
                "svg": svg,
            }
    return fixtures


def rasterize(svg: Path | None, out_png: Path) -> bool:
    if svg is None or cairosvg is None:
        return False
    cairosvg.svg2png(
        bytestring=svg.read_bytes(), write_to=str(out_png), output_width=1600
    )
    return True


def cell(label: str, before, after) -> str:
    changed = before != after
    cls = " class=\"changed\"" if changed else ""
    return f"<tr{cls}><th>{label}</th><td>{before}</td><td>{after}</td></tr>"


def main(argv: list) -> int:
    if len(argv) != 4:
        print("usage: build-review.py BASE_ROOT HEAD_ROOT OUT_DIR")
        return 2
    base = load_root(argv[1])
    head = load_root(argv[2])
    out = Path(argv[3])
    out.mkdir(parents=True, exist_ok=True)

    sections = []
    for fixture in sorted(set(base) | set(head)):
        info = head.get(fixture) or base.get(fixture)
        base_info = base.get(fixture)
        head_info = head.get(fixture)
        name = fixture
        base_png = out / f"{name}-base.png"
        head_png = out / f"{name}-head.png"
        have_base = rasterize(base_info["svg"] if base_info else None, base_png)
        have_head = rasterize(head_info["svg"] if head_info else None, head_png)

        rows = []
        b_sum = (base_info or {}).get("summary", {})
        h_sum = (head_info or {}).get("summary", {})
        for key in ("cosmetic_score", "cosmetic_status", "connectivity", *(
            f"{section}.{key}" for section, key in NUMERIC_KEYS
        )):
            rows.append(cell(key, b_sum.get(key), h_sum.get(key)))

        imgs = []
        if have_base:
            imgs.append(f'<figure><figcaption>before</figcaption>'
                        f'<a href="{base_png.name}"><img src="{base_png.name}"></a></figure>')
        if have_head:
            imgs.append(f'<figure><figcaption>after</figcaption>'
                        f'<a href="{head_png.name}"><img src="{head_png.name}"></a></figure>')
        sections.append(
            f"<h2>{name}</h2>"
            f"<div class=\"row\">{''.join(imgs)}</div>"
            f"<table><tr><th>metric</th><th>before</th><th>after</th></tr>"
            f"{''.join(rows)}</table>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Schematic engine review</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:24px;background:#111;color:#eee}}
 h1,h2{{font-weight:600}}
 .row{{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start;margin-bottom:8px}}
 figure{{margin:0;min-width:0}}
 img{{width:100%;height:auto;display:block;border:1px solid #444;background:#fff}}
 figcaption{{font-size:12px;opacity:.7;margin-bottom:4px}}
 table{{border-collapse:collapse;margin:12px 0 32px}}
 th,td{{border:1px solid #333;padding:4px 10px;font-size:13px;text-align:left}}
 tr.changed td{{color:#ffd166}}
</style></head><body>
<h1>Schematic engine review</h1>
<p>before = baseline artifacts &nbsp; after = current run. Click an image to open full size.</p>
{''.join(sections)}
</body></html>"""
    (out / "index.html").write_text(html, encoding="utf-8")
    print(str(out / "index.html"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
