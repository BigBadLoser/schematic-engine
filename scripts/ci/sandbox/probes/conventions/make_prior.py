"""Turn out/parts.csv (extract.py) into backend/schematic/orientation_prior_data.py.

    PYTHONPATH=. python scripts/ci/sandbox/probes/conventions/make_prior.py
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "out" / "parts.csv"
TARGET = HERE.parents[4] / "backend" / "schematic" / "orientation_prior_data.py"
MIN_N = 10
TWO_PIN_FEATURE = {"shunt_ground": "rail_pin_dir", "shunt_supply": "rail_pin_dir", "series": "axis", "across_rails": "axis"}


def canonical(rotation: int, mirror: str) -> str:
    """Mirror y is mirror x after a half turn; the same form is used at lookup."""
    if mirror == "y":
        rotation, mirror = (rotation + 180) % 360, "x"
    return f"{rotation % 360}/{mirror if mirror in ('x', 'y') else 'none'}"


def build(rows) -> dict[tuple[str, str], dict]:
    counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    features: dict[tuple[str, str], str] = {}
    for row in rows:
        cls, ctx = row["cls"], row["context"]
        if ctx == "multipin":
            feature, value = "orientation", canonical(int(row["rotation"]), row["mirror"])
        elif ctx in TWO_PIN_FEATURE:
            feature = TWO_PIN_FEATURE[ctx]
            value = row[feature]
        else:
            continue
        if not value:
            continue
        counts[(cls, ctx)][value] += 1
        features[(cls, ctx)] = feature
    prior = {}
    for key in sorted(counts):
        n = sum(counts[key].values())
        if n >= MIN_N:
            prior[key] = {
                "feature": features[key], "n": n,
                "shares": {v: round(c / n, 4) for v, c in sorted(counts[key].items(), key=lambda kv: (-kv[1], kv[0]))},
            }
    return prior


def main() -> None:
    with open(SOURCE, newline="", encoding="utf-8") as handle:
        prior = build(list(csv.DictReader(handle)))
    lines = [
        '"""How often humans draw a part each way: generated, do not edit.',
        "",
        "Source: scripts/ci/sandbox/probes/conventions/out/parts.csv (every part on the KiCad demo sheets, by extract.py).",
        "Regenerate: PYTHONPATH=. python scripts/ci/sandbox/probes/conventions/make_prior.py",
        '"""',
        "from __future__ import annotations",
        "",
        "PRIOR: dict[tuple[str, str], dict] = {",
    ]
    for (cls, ctx), entry in prior.items():
        lines.append(f"    ({cls!r}, {ctx!r}): {entry!r},")
    lines.append("}")
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(prior)} entries -> {TARGET}")


if __name__ == "__main__":
    main()
