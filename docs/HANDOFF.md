# Handoff: where the schematic engine stands

Read this first, then `docs/specs/ladder-placer.md` (the specification: rules, the plan/legaliser seam in 4b, multi-unit
parts in 4c, and a record of every solver experiment with its numbers). Last updated 2026-10-17.

## What the engine is for

It draws a KiCad 10 schematic from a netlist so that a person would accept it as drawn by hand. It is judged against
KiCad's demo sheets and a corpus of open-hardware sheets, by pictures first and counts second.

## How the owner wants the work done

These are standing rules from the owner; keep them.

- **Readability over compactness.** A crossing is a readability fault, but some crossings are necessary: crossings are
  priced (50 mm of wire each), never banned. A wire through a body is priced at 150 mm, a short at 300 mm.
- **General rules only.** No rule may name a circuit or a part. A fix must be a rule that would apply to any sheet.
- **Established libraries over home-made heuristics** (CP-SAT for placement, KiCad and MCP Pro for drawing). Downloading
  packages and cloning repositories is fine.
- **Evidence with every change:** the 14-fixture table (below), pictures for sheet changes, and a plain statement of
  what got worse. The four demos must not get worse; their sheets are the regression gate.
- **Labels only for power nets** until the microcontroller fan-out work starts.
- **Time every real-sheet test** (placement seconds and build seconds) so the direct path can be compared over time.
- **Working style:** one orchestrating session reviews and merges; cheaper agents implement in their own git worktrees
  under `.worktrees/` and hand back with tables. Resume a finished agent with a new item rather than starting a fresh one
  (a fresh agent has to rebuild its context, which costs the most). Agents commit on their branch only; the orchestrator
  merges to `main` and pushes (standing permission).

## Layout of the repository

| path | what it is |
|---|---|
| `backend/schematic/ladder_roles.py` | roles: chains, rails, trunks, hangs, rungs, lanes, stacks, clamps, units |
| `backend/schematic/ladder_plan.py` | the plan (rows, hangs, spans, columns; no coordinates) and `plan_routes` |
| `backend/schematic/ladder_layout.py` | the heuristic layout of a plan: the baseline the solver is scored against |
| `backend/schematic/ladder_solve.py` | the CP-SAT legaliser; `legalise(plan, symbols, nets, seeds=)` |
| `backend/schematic/ladder_check.py` | the checker: counts of a placement and whether it keeps the plan |
| `backend/schematic/direct_build.py`, `direct_workflow.py` | draw a placement through MCP Pro, run the checks, render |
| `backend/kicad/` | MCP Pro client, in-process server, extended server tools (`mcp_extended_server.py`) |
| `tests/fixtures/ladder/` | the 14 fixtures (4 demos, 10 corpus circuits) the solver and checker run on |
| `tests/fixtures/direct/` | committed placements and expected counts for the CI demo build |
| `scripts/ci/sandbox/` | the experiment sandbox; its `.work/` (ignored, about 4 GB) holds the venv, runs and corpus |

## Running things

Use the sandbox's Python for everything: `scripts/ci/sandbox/.work/venv/Scripts/python.exe`, with `PYTHONPATH=.` from
the repository root. In a worktree, also set `SBX_WORK_DIR` to the main clone's `scripts/ci/sandbox/.work`.

```bash
python -m unittest discover -s tests/unit
python -m backend.schematic.ladder_check tests/fixtures/ladder/amp.json
python scripts/ci/sandbox/sandbox.py run --label NAME --placement ladder --placement-from rec-1 --placement-pick demo_buck=7 --fixtures demo_buck
```

Solver switches for the sandbox: `KICAD_AI_LADDER_SOLVER=cpsat` (solver instead of the heuristic) and
`KICAD_AI_LADDER_SEEDS=7,11,13,17,19` (the seed portfolio). Demo picks used for every comparison:
`demo_buck=7, demo_lm317=5, demo_opamp=11, demo_ampac=5`. Corpus circuits run as `corpus_<name>` fixtures.

`python scripts/ci/check_all.py` builds all 14 sheets from scratch (solver, five seeds; `--seeds 1` for a pass/fail check),
prints one row per sheet and exits non-zero when a sheet stopped building or got worse in throughs, shorts or crossings
against `tests/fixtures/check_all/expected.json` (`--update` rewrites it; known failures are recorded there). The solver's
answers are cached under `scripts/ci/sandbox/.work/cache/placements/`, keyed by the plan, symbols, nets, seeds, budget and
the source of `ladder_solve.py`, `ladder_plan.py` and `placement_refinement.py`; `KICAD_AI_LADDER_CACHE=0` turns it off.
A cold run took about 58 minutes, a warm one (all cached) about 8.

CI (`.github/workflows/`) runs the unit suite and builds the four demos from committed placements.

## Results now

Done since the last handoff (2026-10-17): field text continues to a larger budget only when a solve ends unproven
with collisions left (tally_timecode 3 to 0 and tally_power 6 to 0 colliding labels; the demos never trigger it); a net
joining opposite pins of one part is a side row beside it (the op-amp follower loop).

Solver placements scored by the checker on the solver's own wires, five seeds (7, 11, 13, 17, 19), deterministic budget
20 per seed, on the committed fixtures. Columns: wires through a body / clearance faults / shorts / crossings / wire mm.

| circuit | result | note |
|---|---|---|
| amp (demo) | 0 / 0 / 0 / 0 / 246.2 | the human sheet has 1 crossing |
| buck (demo) | 0 / 0 / 0 / 0 / 40.6 | |
| lm317 (demo) | 0 / 0 / 0 / 0 / 177.0 | |
| opamp (demo) | 0 / 0 / 0 / 0 / 172.5 | |
| gamma_sipm | 0 / 0 / 0 / 0 / 280.7 | |
| gamma_preamp | 0 / 0 / 0 / 0 / 185.2 | |
| piaa_power | 0 / 0 / 0 / 0 / 39.4 | |
| pmod_hv | 0 / 0 / 0 / 0 / 142.2 | |
| tally_power | 0 / 1 / 0 / 0 / 82.6 | |
| bms_supply | 0 / 4 / 0 / 0 / 141.0 | after taps |
| pico_hbridge | 0 / 3 / 0 / 2 / 271.8 | one A-B crossing is forced by the plan |
| tally_timecode | 0 / 1 / 0 / 2 / 327.8 | multi-unit LM358, taps, jacks, follower side row; was 6 / 20 / 3 / 9 / 677 |
| uno_r4_power | 0 / 0 / 0 / 2 / 539.0 | |
| bitaxe_power | 0 / 22 / 0 / 40 / 2711.7 | places, but its plan is poor |

Real-sheet builds of the four demos (netlist PASS, 0 crossings): buck 68.6 mm, LM317 205.7, op-amp 235.0, amplifier
330.2 (run `20261013T203220Z-ladder-solve-4`, before the full drop margin). tally_timecode builds on a real sheet:
run `20261014T161401Z-seeds-1`, picture `tally_final.png` in its artifact folder. A real-sheet build takes 20 to 90 s
plus the solve (1 to 4 minutes at five seeds); the old trial loop took 6 to 30 minutes per sheet.

## Open items, in priority order

1. **Two corpus sheets stopped building** (found 2026-10-17): bms_supply fails in routing (a wire through test point
   TP5) and tally_power at the connectivity gate (the battery switch SW6's unused pin 3). Both built on 2026-10-13
   (run `corpus-direct-3`). Suspects are the taps, multi-unit, power-unit and connector-facing merges; bisect, then fix
   with a general rule. Lesson: the gate checks the demos on real sheets but the corpus only at plan level; a from-scratch
   corpus build on every merge would have caught this the day it landed.
2. **tally_timecode's bypass capacitor C19** still stands alone at the bottom. Two rules were tried and dropped (spec
   4b experiment log; branch `experiment/bypass-rail-hang`): holding it beside the power unit pulls the amplifier stage
   down and costs over 100 mm. A future try must place it clear of the stage's rows.
3. **LM317's last label "collision"** is a margin disagreement, not an overlap: net labels are written after the field
   stage and one lands 0.8 mm from a value, inside the field placer's 1.0 mm margin. Fix by making the label step keep
   the same margin from field text (preferred), or by placing labels before fields. Owner to choose.
F loops round U5B.
4. **Seeds as the default for real builds.** The library default is one seed; the sandbox uses five via
   `KICAD_AI_LADDER_SEEDS`. One seed can land in a spread layout (tally_timecode: 505 mm, once 2000 mm); five converge.
5. **bitaxe_power's plan** (grammar): its second group stands far from the controller, and the plan forces many
   crossings. See the diagnosis in the spec (section 4b log) before changing rules.
6. **Package it for the companion:** rename `backend.schematic` and `backend.kicad` to a proper package name, add a
   `pyproject.toml`, and expose a small surface (intent in, sheet out). Do it when no agent has a branch open, as one
   mechanical commit.
7. **Refresh the demo fixtures' baselines.** The committed demo fixtures predate refinement-scoring changes; regenerate
   them with `sandbox.py ladder-fixtures` and record old and new baseline numbers in the commit. Sheets do not change.
8. **Deterministic sheet writing.** KiCad's written symbol and pin order varies between identical runs; make the writer
   emit a stable order so sheets can be compared as text.
9. **Later:** labels beyond power nets and the microcontroller fan-out work; corpus circuits beyond the ten fixtures.

The companion repository's PR #8 (BigBadLoser/kicad-ai-engineering-companion) is the old home of this engine and is
still open; the owner decides whether to close it.

## Known quirks

- KiCad writes a sheet's symbols and pins in an order that changes between runs of the same code. Compare sheets in a
  canonical form (uuids normalised, children sorted), not as raw text.
- The heuristic layout of tally_timecode cannot be routed (the jacks' tangle); only the solver's placement builds.
- `bitaxe_power` places but poorly: its plan is the problem, not the solver.
- The solver's wall-clock guard (650 s) is a safety net only; results are decided by the deterministic budget and seeds.
