import csv
from pathlib import Path
rows = list(csv.DictReader(open(Path(__file__).parent / "out" / "parts.csv", encoding="utf-8")))
for r in rows:
    if "buck_conv" in r["sheet"] and r["ref"] in ("D101", "C101", "R103"):
        print(r["ref"], r["lib_id"], r["rotation"], r["mirror"], r["context"], r["rail_pins"], r["ac_dir"])
n = 0
for r in rows:
    if r["rotation"] != "0" and r["mirror"] != "none" and r["cls"].startswith("transistor") and n < 3:
        print(r["sheet"], r["ref"], r["lib_id"], r["rotation"], r["mirror"], r["e_dir"], r["c_dir"], r["b_dir"]); n += 1
