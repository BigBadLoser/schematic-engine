# Experiment sandbox

An isolated, reproducible place to run schematic-engine experiments without touching
the working tree, native on Windows (the Docker image next to it, `../Dockerfile.engine`,
pins the same versions), with experiment variants applied as patches to a throwaway copy
of the source.

Nothing here edits tracked files. Everything it creates lives under `.work/` (git-ignored).
What it has measured so far is in [FINDINGS.md](FINDINGS.md).

## Quick start

```powershell
python scripts\ci\sandbox\sandbox.py doctor        # what is ready, what is missing
python scripts\ci\sandbox\sandbox.py probes        # offline measurements, no installs
python scripts\ci\sandbox\sandbox.py bootstrap     # prints the setup plan, downloads nothing
python scripts\ci\sandbox\sandbox.py bootstrap --yes
python scripts\ci\sandbox\sandbox.py run --label baseline --placement-from REC_RUN
python scripts\ci\sandbox\sandbox.py run --label e9 --placement-from REC_RUN --overlay e9_no_direct_junction
python scripts\ci\sandbox\sandbox.py compare baseline e9
```

Run `sandbox.py selftest` for the sandbox's own unit tests (offline, a few seconds).

## Safety

- The network is touched only by `bootstrap --yes`. Without `--yes` it prints the plan:
  what is fetched, from where, how big, and which hash a file must match.
- Python packages install with `--only-binary=:all:` (no `setup.py` runs); npm installs with
  `--ignore-scripts`; the elkjs bundle is refused unless its git blob sha1 equals the pinned one.
- Dependencies install into `.work/venv` and `.work/node_modules`, never the user site or the
  repository root. The sandbox venv ignores user-site packages (the ambient MCP Pro is 3.37.0;
  the sandbox pins 4.0.1 like CI).
- `pins.json` and `package.json` are the pinned versions; `selftest` fails if they drift from
  `Dockerfile.engine` or `schematic-recovery-visual.yml`.

## How a run works

1. Snapshot `backend/`, `scripts/` and `tests/` into `.work/runs/<stamp>-<label>/src`
   (text normalized to LF, so patches apply the same on a CRLF checkout and in Linux CI).
2. Apply the requested overlays to the snapshot (`overlays/*.patch`). A patch that no longer
   applies fails loudly instead of silently doing nothing.
3. Build each fixture in this process (`scripts/schematic_direct_smoke.py`, no MCP server) at a placement: the engine's
   recorded one (`--placement-from RUN`, with the placement dumps) or the ladder placer's on that circuit
   (`--placement ladder`; `corpus_*` circuits need no recorded run).
4. Score every produced schematic with `sbx/kpi.py` and write `manifest.json`
   (git head, tree hash, overlays, versions), `kpi.json` and per-fixture artifacts in
   `artifacts/schematic-engine-<fixture>/` - the layout `../compare-runs.py` and
   `../build-review.py` already read.

## Overlays

`sandbox.py overlays` lists them; `overlays --check` proves they all apply to the current tree.

| Overlay | Knob | Tests |
|---|---|---|
| `e2_libavoid_options` | `SBX_LIBAVOID_OPTIONS` (JSON) | any libavoid option, e.g. `{"nudgeOrthogonalSegmentsConnectedToShapes": false}` |
| `e3_dump_libavoid` | writes `dumps/` automatically | exact libavoid input/output per routing call; read with `analyze-dumps` |
| `e9_no_direct_junction` | `SBX_NO_DIRECT_JUNCTION=1` | turns off the straight pin-to-junction shortcut, which never checks the other pins of the endpoint's own symbol |
| `e5_trace` | writes `dumps/` automatically | keeps every power-terminal decision (read with `terminals`) |

Five overlays, `e1_tight_footprint`, `e6_terminals_tight`, `e7_allow_crossings`, `e8_single_call_routing` and
`e10_guarded_grouped_call`, were retired when their content became engine features; their measured results
stay in `FINDINGS.md`. The engine takes the same experiments as environment variables:

| Engine knob | Replaces | Effect |
|---|---|---|
| `KICAD_AI_LAYOUT_MODE=legacy\|tight` | `e1`, `e6` | `tight` (the default) sizes symbols to body and pin tips plus a margin in placement and power terminals; `legacy` keeps MCP Pro's fixed 20.32 x 15.24 mm box for placement (power terminals are still measured by body and pin tips) |
| `KICAD_AI_LAYOUT_MARGIN_MM` | `SBX_FOOTPRINT_MARGIN_MM` | the tight margin (default 1.27) |
| `KICAD_AI_STUB_OBSTACLES=1` | `SBX_KEEP_WIRE_BOXES` | earlier wires are libavoid obstacles as well as being checked |
| `KICAD_AI_ROUTER_EXPERIMENT=compact_router` | | routes pass 1.27 mm from a symbol (not 1.905) and may turn 1.905 mm after a pin (not 2.54), so a pin tip needs 3.175 mm of free space in front of it instead of 4.445 |

A build routes all nets in one grouped call with the ownership checks: a route may not touch another net's pin or wire,
and may cross one in both interiors, which is what `e7`, `e8` and `e10` measured.

Pass knobs with `--env KEY=VALUE` (repeatable). Overlays combine:

```powershell
python scripts\ci\sandbox\sandbox.py run --label m2.54 --placement-from rec-1 --fixtures demo_buck --env KICAD_AI_LAYOUT_MARGIN_MM=2.54
python scripts\ci\sandbox\sandbox.py run --label nodj --placement-from rec-1 --overlay e9_no_direct_junction --env SBX_NO_DIRECT_JUNCTION=1
```

To add an overlay, edit a throwaway copy of the target file, write a unified diff with `a/` and
`b/` path prefixes into `overlays/NAME.patch`, and start the file with `#` comment lines
(the first lines become the description). `overlays --check` validates it.

## KPIs (`sbx/kpi.py`)

Computed from the saved `.kicad_sch` and the fixture's declared nets, independent of the
engine's own cosmetic score:

- **wire excess** - routed length of each wired net / its half-perimeter wirelength (1.0 is ideal)
- **stubs** - length of wire components touching exactly one declared pin (power/label attachments), total and per pin
- **net ownership** - derived geometrically from wire, pin and junction contacts
- **crossings** (legal in KiCad), **t_contacts**, **shorted_nets** (one wire component, two nets)
- **body hits** (a wire inside a symbol body; "foreign" = not that symbol's own wire), **body overlaps**
- **passives** - nearest-neighbour spacing of two-pin parts, the engine's footprint floor
- corners, merged vs raw wire length, drawing extent

`sandbox.py kpi PATH` scores any artifact directory, including ones from the Docker harness.

`sandbox.py readability RUN [--corpus]` reports how the shipped and the selected sheet of each fixture *read*
(`backend/schematic/readability.py`, the same measures the engine ranks candidates by): reference/value text with a wire
through it, labels drawn vertically, wire crossings, wires drawn through a symbol's body (`thru body`), tiny jogs, long
detours, and how far each support part (a bypass or load capacitor, a pull-up, a series resistor) is from the part it
serves. `--corpus` adds the same measures over KiCad's
own demo projects (91 human-drawn sheets, cached in `.work/out/corpus-readability.json`) as the band to compare against
(FINDINGS section 13).

`i2c_sensor` and `led_switch` are circuits that declare no relations at all (an I2C sensor on a four-pin connector; a
transistor switching an LED); run them by name (`--fixtures i2c_sensor,led_switch`). A build at a recorded placement
declares and infers no relation: the `<fixture>_bare` names (a circuit with every declared relation removed) only matter
for scoring runs made before the trial loop was retired.

### KiCad's demo circuits, redrawn (`demos`, `redraw`)

The engine's fixtures are circuits it was built on. To see how it does on others, and against an engineer's own drawing,
`sandbox.py demos` rebuilds four of KiCad's demo circuits from their netlists (`kicad-cli sch export netlist`): parts,
values, which pins join, the functional blocks and flow written by hand in `sbx/demos.py` (the input every fixture has;
no relation, no coordinate) and the nets drawn with power symbols. Only demos whose symbols are all in KiCad's standard
libraries qualify (the rest draw with symbols of their own project, which cannot be rebuilt by name):
`demo_lm317` (AC source, transformer, bridge, LM317 supply), `demo_buck` (buck converter), `demo_ampac` (three-transistor
amplifier), `demo_opamp` (op-amp stage beside a one-transistor stage). A reference the schematic library refuses
(`Rmeas1`: lower case) is renamed. `demos --write` regenerates `scripts/schematic_demo_circuits.py` (plain data, run by
name like any fixture: `run --fixtures demo_lm317,demo_buck`).

`sandbox.py redraw RUN [--blind DIR]` measures the human original, the shipped (verified) sheet and the selected sheet of
each demo in a run with the same counts (wire, area, corners per connection, bent wires, crossings, text on wires, far
support parts, power symbols entered sideways). `--blind` also draws the three unlabelled, in a shuffled order and without
the human sheet's free text and extra fields, into `DIR` (`<demo>.html` stacked, `<demo>-A.svg`...); the key is in
`DIR/key.json` and is not printed.


`sandbox.py terminals RUN` lists each power-terminal decision (strategy, stub length, and whether the unchecked fallback fired) from a run made with `e5_trace`.

`sandbox.py replay RUN [--variants a,b | all]` re-routes the libavoid calls recorded by `e3_dump_libavoid` under router variants (see below).

`sandbox.py rescore RUN` re-applies the current KPI code to a past run's artifacts without rerunning the engine. `sandbox.py placement RUN [RUN ...]` reports placement-only KPIs (signal/total HPWL, passive spacing, overlaps, extent) from the saved pre-route scenes, so a variant that breaks routing can still be compared.

## Placement replay (`placement-replay`)

Runs recorded with the retired `e11_placement_inputs` overlay hold what each placement solver was asked (a pickle per
call, three per circuit and trial: seed, align, refine); the overlay went with the trial loop, so no new ones are made.
`sandbox.py placement-replay RUN [--group N] [--weights bend=8,jog=6] [--rounds N] [--orient 4] [--svg F]` runs
them again in seconds, with no MCP Pro, KiCad or libavoid: other weights, more rounds of alignment and refinement, every
orientation for the parts of three or four pins, and a drawing of the layout with the routes the placement estimates. It judges
by the placement's own estimate (bends, jogs, wire, extent), which is what the solvers optimise and not what the router draws
(`FINDINGS.md` section 15 compares the two); a change is only kept after a real run.

## Router replay lab (`replay`)

`e3_dump_libavoid` records the exact payload of every grouped libavoid call, libavoid's raw answer
and the endpoint table. `replay` rebuilds each payload under a *variant*, routes all of them in one
Node process (`probes/batch_route.mjs`, the same `@mr_mint/elkjs-libavoid` the engine uses), then applies
the engine's own post-processing and acceptance checks (grid snap, endpoint overwrite, pin stubs;
orthogonality, symbol-body and pre-existing-conductor checks). The report says per variant how many
routes the engine would accept, why the others are rejected, and the length and bends of the rest. The
`asis` control must reproduce the recorded routes (the `reproduced` column).

`replay --list` shows the variants: option ablations (`nudge_off`, `penalties_off`, `plain`), obstacle
changes (`no_text`, `inflate0`, `buf0` = clearance baked into the obstacles with libavoid's own buffer
at 0), the portal distance (`portal1.27`), and real ports on a symbol node (`ports*`).

What it cannot say: anything that depends on how earlier nets were routed (calls are frozen, so order
sensitivity is out of scope), `simplify_route` shortcuts, and the engine's retry ladder.

Since 2026-10-10 the engine poses each problem to libavoid from a grid corner (`router_origin`: the same layout is routed
alike wherever it stands, FINDINGS section 14.5), so the payload and raw routes of a new dump are in that frame while the
endpoint table is in the sheet's. `sbx.dumps.absolute` puts a dump back in the sheet's frame (`analyze-dumps` and `replay`
both read dumps through it; a dump from before the change is returned as it is).

## Probes (offline)

`sandbox.py probes` needs no installs and runs against whichever MCP Pro the interpreter has
(the sandbox venv once bootstrapped, otherwise the current Python):

| Probe | Question |
|---|---|
| `footprint_probe` | how big is the box placement uses versus the tight body? |
| `pin_audit` | do MCP Pro's pin outward normals match the pin's side of the body? |
| `transform_xcheck` | do `kicad_geometry._transform` and MCP Pro agree per rotation/mirror? |
| `transform_oracle` | which of them does KiCad itself agree with? (builds a schematic, exports the netlist with `kicad-cli`) |
| `libavoid_surface` | which libavoid features does the pinned `libavoid-js` binding expose (hyperedges, junctions, `route()`)? Runs after bootstrap. |
| `text_boxes` | do the geometry reader's text boxes match the text KiCad renders? Reads a run's SVG; fails if a kind drifts outside 0.8 to 1.2 of the real width or 0.6 mm of its centre. |
| `terminal_feasibility` | can the terminal planner give every power pin of a saved placement a stub and a power symbol? Plays the engine's terminal stage on a run's `pre-route-presentation-scene.json` (newest run, or `SBX_FEASIBILITY_RUN`) and names the pins that have no exit and why. |
| `field_justify` | does the text model put a reference or value where KiCad draws it, for every symbol rotation, mirror, field angle and justification? Builds one sheet with a resistor per combination, has `kicad-cli` draw it and compares each text box with the one `kicad_geometry.text_box` predicts (needs a finished run for its library symbols). |

Re-run `probes` after `bootstrap` to repeat the measurements against the pinned MCP Pro 4.0.1.

## Status

Verified: environment discovery, snapshots, all overlays (apply, compile, compose, and byte-match
`git apply` for every file they touch), KPI maths, dump analysis, pin-drift guards, the bootstrap
plan, the Python probes against MCP Pro 3.37.0 and 4.0.1 with KiCad 10.0.4, and real engine runs
on Windows (the native `rc_filter` run reproduces the Docker harness's cosmetic score of 96.0).

Not built: a standalone router lab (ports versus portals, single-session routing, native
hyperedges); `e2`/`e3` cover option ablations and raw-output analysis inside the real pipeline,
but not changes to how endpoints are modelled.

The Docker harness remains the reference for Linux/CI parity; this sandbox is for fast
local iteration on a machine without Docker.
