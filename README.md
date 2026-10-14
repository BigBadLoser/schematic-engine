# KiCad schematic engine

Draws readable KiCad 10 schematics from a netlist: which parts a circuit has and which pins each net joins. The goal is a
sheet a person would draw, judged against KiCad's own demo sheets and a corpus of open-hardware projects.

## How a sheet is made

```text
intent (parts, nets, rails)
  -> roles          backend/schematic/ladder_roles.py    chains, rails, trunks, hangs, rungs, lanes, stacks
  -> plan           backend/schematic/ladder_plan.py     rows, hangs, spans, columns: the ladder, without coordinates
  -> placement      backend/schematic/ladder_solve.py    CP-SAT legaliser (ladder_layout.py is the heuristic baseline)
  -> direct build   backend/schematic/direct_build.py    symbols, routes, fields, labels, checks, render through MCP Pro
```

The specification, with the rules, the plan/legaliser contract and every solver experiment and its result, is
[docs/specs/ladder-placer.md](docs/specs/ladder-placer.md).

`backend/kicad/` holds the MCP Pro client (`mcp_client.py`; `mcp_inprocess.py` runs the server in-process), the
extended server tools the build uses (`mcp_extended_server.py`) and KiCad CLI helpers. Schematic writes are opt-in
behind `KICAD_AI_ENABLE_SCHEMATIC_WRITES=1`.

## Requirements

- KiCad 10 with `kicad-cli` (Windows: `C:\Program Files\KiCad\10.0\bin`; not on `PATH` by default)
- Python 3.13, `kicad-mcp-pro==4.0.1`, `ortools==9.12.4544` (9.15 needs protobuf 6, which kicad-mcp-pro's kicad-python
  does not accept)
- Node with `@mr_mint/elkjs-libavoid@0.5.0` for the router bridge (`scripts/libavoid_route.mjs`)

The sandbox under `scripts/ci/sandbox` pins all of these in its own venv; see its README.

## Checks

```bash
python -m unittest discover -s tests/unit
python -m unittest discover -s tests/integration
python -m backend.schematic.ladder_check tests/fixtures/ladder/buck.json
python scripts/ci/check_direct_build.py .kicad-ai/direct
```

`ladder_check` scores a placement of a fixture against its heuristic baseline (through-body wires, overlaps,
clearance, shorts, crossings, wire length) and checks it keeps the plan. `check_direct_build.py` builds the four
KiCad demos from committed placements (`tests/fixtures/direct/`) and compares netlist, wire length, corners and
crossings with `expected.json`; CI runs it in `.github/workflows/schematic-direct.yml`.

Experiments run in the sandbox, on a patched copy of the tree:

```bash
python scripts/ci/sandbox/sandbox.py run --label NAME --placement ladder --placement-from rec-1 --fixtures demo_buck
```
