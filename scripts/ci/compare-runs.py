#!/usr/bin/env python3
"""Compare two recovery-harness artifact roots for behavioural equivalence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

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
    presentation = report.get("presentation_geometry") or {}
    workflow = report.get("workflow") or {}
    cosmetic = workflow.get("cosmetic_score") or {}
    summary = {
        "fixture": report.get("fixture"),
        "cosmetic_score": cosmetic.get("cosmetic_score"),
        "cosmetic_status": cosmetic.get("status"),
        "connectivity": find_gate_status(report, "connectivity"),
        "visual_qa": find_gate_status(report, "visual qa"),
        "geometry_qa_gate": presentation.get("geometry_qa_gate"),
        "presentation_status": presentation.get("status"),
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
        if report_path.is_file():
            summary = summarize(report_path)
            fixtures[summary["fixture"] or directory.name] = summary
    return fixtures


def main(argv: list) -> int:
    if len(argv) != 3:
        print("usage: compare-runs.py BASE_ROOT HEAD_ROOT")
        return 2

    base = load_root(argv[1])
    head = load_root(argv[2])
    regressions: list = []

    for fixture in sorted(set(base) | set(head)):
        print(f"== {fixture} ==")
        before = base.get(fixture)
        after = head.get(fixture)
        if before is None or after is None:
            side = "base" if before is None else "head"
            print(f"  missing in {side}")
            regressions.append(f"{fixture}: missing fixture")
            continue
        for key in sorted(set(before) | set(after)):
            bv = before.get(key)
            hv = after.get(key)
            marker = "" if bv == hv else "  <-- changed"
            print(f"  {key}: {bv} -> {hv}{marker}")
            if key == "connectivity" and bv == "PASS" and hv != "PASS":
                regressions.append(f"{fixture}: connectivity {bv}->{hv}")
            if (
                key == "cosmetic_score"
                and isinstance(bv, (int, float))
                and isinstance(hv, (int, float))
                and hv < bv
            ):
                regressions.append(f"{fixture}: cosmetic {bv}->{hv}")

    if regressions:
        print("\nREGRESSIONS:")
        for item in regressions:
            print(f"  - {item}")
        return 1
    print("\nNo hard regressions.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
