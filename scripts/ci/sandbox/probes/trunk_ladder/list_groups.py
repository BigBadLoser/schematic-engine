from scripts.ci.sandbox.sbx import report, placement_replay as pr
cs = pr.load_circuits(report.resolve_run("auto-st3"))
for i,c in enumerate(cs):
    r=c["refine"]; s=r["args"][0]
    print(i, sorted(c), len(s), sum(x.movable for x in s), ",".join(x.reference for x in s)[:90], [len(x.geometry) for x in s][:6])
print("----")
for i,c in enumerate(cs):
    s=c["refine"]["args"][0]; refs={x.reference for x in s}
    if {"T1","D3","R5"}<=refs or "L101" in refs: print(i, sorted(c), len(s), sum(x.movable for x in s), sorted(refs), [len(x.geometry) for x in s])
