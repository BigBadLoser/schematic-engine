import csv, glob, os, collections
import xml.etree.ElementTree as ET
O="scripts/ci/sandbox/probes/conventions/out/"
nls={}
for f in glob.glob(O+"netlists/*.xml"):
    d=os.path.basename(f).split("__")[0]; m={}
    for net in ET.parse(f).getroot().iter("net"):
        n=net.findall("node")
        for x in n: m[(x.get("ref"),x.get("pin"))]=len(n)
    nls[d]=m
c=collections.Counter(); miss=0
for r in csv.DictReader(open(O+"parts.csv",encoding="utf-8",errors="replace")):
    if r["context"]!="series" or r["npins"]!="2": continue
    parts=r["sheet"].split("/")[:-1]
    m=None
    for i in range(len(parts),0,-1):
        k=parts[i-1]
        if k in nls: m=nls[k];break
    if not m: miss+=1;continue
    a,b=m.get((r["ref"],"1")),m.get((r["ref"],"2"))
    if a is None or b is None: miss+=1;continue
    k="rung" if a>=3 and b>=3 else "series"
    c[(k,r["axis"])]+=1
print(dict(c),"missing",miss)
for k in("rung","series"):
    t=sum(v for (kk,a),v in c.items() if kk==k); print(k,t,{a:round(v/t,3) for (kk,a),v in c.items() if kk==k})
