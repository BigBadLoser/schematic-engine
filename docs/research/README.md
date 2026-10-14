# Research on schematic generation: what it said and what we did with it (2026-10-12)

Three surveys, written by subagents from web sources and tool code:

- `open-source-schematic-engines-1.md`: SKiDL, circuit-synth, tscircuit (tools that write schematics from code).
- `open-source-schematic-engines-2.md`: netlistsvg, d3-hwschematic and ELK's own options (why our whole-circuit ELK prototype failed).
- `schematic-generation-literature.md`: Cadence patents (US7917877, US8504953, US6980211), Weave (arXiv 2607.03835), and what could be found of the classic papers.

## Convergent lessons

1. Two-pin parts to a rail are attachments of their host (host's column, hung toward the rail, moved with the host): a
   structural rule, not a cost term.
2. The main signal chain is a pinned row with a hard left-to-right order; a soft flow cost reintroduces wander.
3. Columns come from signal depth and rows from current flow (supply at the top, ground at the bottom).
4. Multi-pin nets are drawn as a trunk with drops, often as a post-routing cleanup.
5. Labels are chosen by net class and routed length; named nets get labels.
6. ELK-based tools put every port on the flow axis and route hyperedges through junction nodes; ELK has no
   "same column as X" constraint, so hanging parts cannot be kept with their host by options alone.

Not worth copying: SKiDL's force-directed placement and private router; circuit-synth's row-wrap placement;
tscircuit's template corpus of sub-circuits (circuit-specific).

## What we tried

- Lessons 4 and 5 were already in hand (trunk-and-drops estimate, tidy pass, names on wires) and stay.
- Lesson 3's row half is the learned orientation prior (ground pin down, supply up); its column half is the ELK
  block floorplan plus the soft flow term (`signal_flow.py`, retired with the trial loop), which no other open-source tool has.
- Lessons 1 and 2 were prototyped offline on recorded placement inputs (`scripts/ci/sandbox/probes/attachments/`),
  three iterations: chains came out right (LM317: V1 -> T1 -> D1 -> U1 -> D2; amplifier: V1 -> C1 -> Q2 -> Q3 -> Q1
  -> R12 -> L2) and the LM317 row flat, but the engine's own placement model scored every variant worse
  (wires through bodies, more bends), hanging from pins or from trunks alike. Verdict: these rules need a placer
  designed around them (columns, rows, composite nodes), not constraints bolted onto the greedy search. Parked.
