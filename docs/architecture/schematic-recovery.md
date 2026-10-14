# Schematic engine recovery boundaries

Baseline: commit `5e1472ff2f8058abc5459ed75c096dd304012052` on
`scratch/elk-layout-ab`. Recovery branch: `fix/schematic-engine-recovery`.

> **The trial loop described below has been retired.** `scripts/schematic_scratch_workflows.py`,
> `schematic_elk_planning.py`, `schematic_mcu_fixture.py`, `schematic_engine_mcu_smoke.py`, `backend/schematic/workflow.py`
> and the modules only they used (`block_flow`, `composition_tidy`, `macro_compaction`, `placement_audit`,
> `placement_roots`, `quality_selection`, `relation_inference`, `signal_flow`) were deleted when the direct path
> (`backend/schematic/direct_build.py` on `backend/schematic/direct_workflow.py`) became the only way a sheet is built.
> The sections that follow are the history of how the engine got there and are kept as written.

## Modules

| Boundary | Owner | Responsibility |
| --- | --- | --- |
| Direct build | `backend/schematic/direct_build.py` | Build a sheet once from a finished placement: create, place, terminals and routes, fields, checks |
| Stage methods | `backend/schematic/direct_workflow.py` | The MCP Pro calls those stages make (routing, power terminals, labels, fields, paper) and the acceptance checks |
| Integration harness | `scripts/schematic_direct_smoke.py` | Build one fixture at a placement in a temporary project, render, save reports |
| Electrical acceptance | `backend/schematic/connectivity.py` + KiCad CLI XML netlist | Exact intended pin equivalence and no-connect verification |
| Layout iteration | `backend/schematic/layout_iteration.py` | Placement room policy per symbol, failure classification, and the next policy to try; pure |
| Terminals and labels | `backend/schematic/terminal_placement.py` | Stub plus end symbol (power symbol or label) planning against pins, wires and text; fail-closed |

The sandbox's experiments patch a throwaway copy of the MCP implementation (`scripts/ci/sandbox/overlays`). These
patches are **temporary adapters**, not a desirable permanent engine boundary. Do not add new geometry
features by duplicating more source-string rewrites.

## Current integration sequence

1. Build coordinate-free semantic intent and validate pin/net membership.
2. Delegate symbol creation to MCP Pro.
3. Build ELK macro floorplan and relation placements through MCP tools.
4. Route local grouped edges using libavoid, including explicit junction legs.
5. Validate final KiCad XML netlist against the original intent.
6. Run connectivity/ERC and visual QA; render SVG and PNG.

**Do not treat visual QA warnings as electrical correctness failures, or
electrical correctness as proof of visual acceptability.** The validated MCU
baseline passes netlist/connectivity checks but scores only 56/100 cosmetically.

## Geometry ownership for the next pass

This is a **target design**, not implemented by the refactor.

- Symbol bodies and true electrical pin endpoints come from measured KiCad
  geometry; pin text and fields are distinct presentation items.
- Place related symbols jointly before routing; resolve shared-node geometry
  in that placement stage.
- Reserve power symbol glyphs, fields, pin-text and routing escape corridors
  against **the same final placed scene**, rather than recomputing them after
  routing or silently moving text behind the router's back.
- The router sees the final presentation obstacles. Only an endpoint's own
  permitted escape may intersect its own graphics, and a wire must never
  cross foreign text, bodies, or pin tips.
- Every mutation requires fresh KiCad netlist verification. Render and
  measure geometry regressions separately.
- Prefer one reusable clearance/ownership rule to symbol- or net-specific
  exceptions.

## Regression policy

Fast branch CI (`schematic-recovery-check.yml`) compiles the extracted modules,
verifies fixture electrical invariants, applies the grouped-routing CI patch,
and runs schematic unit tests. The complete MCU render was run successfully
against this refactor at GitHub Actions run `37720660899`, with
10 verified nets, netlist PASS, connectivity PASS, and pixel-identical PNG output
versus the baseline. Cosmetic score remains 56/100.

Future presentation changes must report: netlist status, connectivity gate,
visual collision findings, cosmetic score, and an actual rendered screenshot.
Do not change the snapshot oracle or semantic intent to make an aesthetic test pass.

## Next incremental change

Port **only** the useful primitive-level presentation geometry and attachment
ownership concepts from the Codex experiment. Add a focused regression for
power-flag/rail text intersecting an attachment wire. Verify collision repair
against the unchanged MCU fixture and ensure the previously clean NRST straight
junction route does not regress. Avoid moving Codex's full joint-composition
pipeline wholesale until the new geometry rules pass independent tests.

## Verified presentation recovery (October 2026)

All three renders use the same declarative MCU fixture. The full-render
workflow is `.github/workflows/schematic-recovery-visual.yml`: future runs
are manually dispatchable, rather than running on every recovery commit.

| State | GitHub Actions run | XML netlist | Reported symbol/text overlaps | Cosmetic score |
| --- | --- | --- | --- | --- |
| Recovery baseline | `37720660899` | PASS | 5 | 56 |
| Uniform flag terminals | `37722753433` | PASS | 3 | 56 |
| Uniform singleton rails | `37723188087` | PASS | 0 | 96 |

The final run verifies 10 nets and 9 no-connects, and its MCP connectivity
gate passes. ERC has warnings but no errors (primarily CI library table
configuration warnings). The only remaining MCP visual-QA warning is
`title_block_missing`.

Changes were deliberately small:

1. Non-rail power endpoints, including PWR_FLAG, now use one clearance-aware
   `sch_add_pin_power_terminals` call and receive physical wire stubs. This
   removes directly superimposed power-flag and power-symbol graphics.
2. Collapsed/singleton relation rails reuse that same terminal operation
   rather than attaching the power symbol directly at the component pin.
3. The local-node CI patch avoids importing the heavy KiCad parser when
   there are no shared nodes; a lightweight test verifies the non-rail
   auxiliary routing contract.

**The final render is not yet a 96/100 human-readability result.** Visual
inspection still reveals crowded MCU pin labels and crowded SWD/UART
connector annotations, plus long ground terminal escapes at the crystal
cluster. The MCP cosmetic score and QA do not detect every such problem.
No numerical cosmetic improvement should be claimed as comprehensive
visual correctness.

## Next geometry increment

Use one measured presentation scene with explicitly distinct reservations
for component graphic bodies, electrical pin endpoints/strokes, pin
names/numbers, reference/value fields, power glyph/text, net labels, and
wire/junction geometry. Every reserved item should carry an owner and
net association, allowing *only* declared attachment intersections.

Measure/reference the **final KiCad placement** before libavoid routing.
Position fields and reserve their rendered text regions before routing,
rather than relying on a post-route `sch_fix_readability` pass that can
invalidate wire clearance. Add focused no-overlap tests with real rendered
images, without teaching the engine component- or net-specific exceptions.
Keep manual screenshot comparison alongside exact XML netlist, connectivity,
and ERC gates.


## Phase 1: Circuit-independent intent and test fixtures

The reusable semantic-grammar workflow consumes `SchematicIntent.relations`.
It no longer imports MCU fixture relations or seeds placement with `U1`,
`J2`, or any other literal reference. `initial_placed_references` derives
its anchors and eligible unmoved symbols from compiled relation dependencies.
Connector-facing preference uses semantic net driver/endpoint order rather
than an MCU-target preference. The wire hub similarly comes from an explicit
driver, or endpoint order if the design declares no driver. Signal names and
classes do not select different routing algorithms.

The four semantic cases currently exercised are the MCU, RC filter,
non-inverting amplifier, and regulator. The same relation compiler is used
for each. Pure Python tests check that renaming references leaves relation
constraint types and initial placement roots equivalent. These tests
**do not establish rendered layout equality** under renaming: that still
requires a KiCad full-render regression.

All hand-authored Level-1 relations live in fixture intent. They are optional
semantic design input, not a separate generator mode for each circuit.

## Phase 2: Typed presentation scene, read-only foundation

`backend/schematic/presentation_geometry.py` provides typed reservations for:

- symbol body and graphic primitives,
- electrical pin endpoints and strokes,
- pin names and pin numbers,
- reference/value fields,
- power glyphs/text,
- net labels, wires, and junctions.

Each reservation records an owner, bounding extent in millimetres, optional
net, explicit allowed attachments, and confidence provenance. The
`GeometryScene` collision policy makes **only explicitly declared same-net
terminal/junction attachments** legal conductor intersections; shared net
names alone do not legalize wire crossings. Text does not inherit a blanket
same-symbol collision exemption.

`backend/schematic/kicad_geometry.py` extracts real coordinates, vector
graphics, pins, wires, labels, and positioned fields from authored KiCad
`.kicad_sch` S-expressions. It marks estimated text metrics, arcs, and
mirrored envelopes appropriately, and records unsupported or unknown
geometry in the coverage report. It does not invent exact glyph rectangles
or wire-net ownership.

The experimental workflow saves a **pre-route presentation scene** after
placement. The integration smoke runner saves the **post-route scene** after
MCP authoring and KiCad rendering. Geometry QA remains diagnostic, not an
acceptance gate: text measurements are approximate and wires do not yet
have dependable net/attachment identities. The scene export intentionally
omits raw collision counts until these inputs are reliable.

The next increment is to resolve precise pin-to-net and junction ownership
from the final KiCad netlist, measure glyph extents from KiCad's rendered
presentation, and feed the frozen scene to placement/reservation and libavoid.
Only then can a collision validator become a meaningful hard gate.

## Verification and cost policy

The lightweight `schematic-recovery-check.yml` workflow validates
all semantic and geometry contracts without installing KiCad. The opt-in
`schematic-recovery-visual.yml` performs real full renders across multiple
fixtures to expose integration/visual failures; it is not automatically
executed on every code commit.

Do not treat a successfully parsed schematic or compiled relation set as
proof that a fixture rendered, passed ERC, or looks professional. Compare
actual renders side by side at identical scale.


### Read-only replay evidence

A local replay against the electrically validated MCU artifact
(`schematic-recovery-mcu-terminal-rails`, GitHub Actions
`37723188087`) successfully parsed the authored KiCad S-expression
without KiCad/MCP running. It extracted 351 typed primitives:
12 ordinary symbol bodies, 16 power graphics, 61 pin endpoints,
61 pin strokes, 61 pin numbers, 33 pin names, 12 reference fields,
12 value fields, 16 power text regions, 8 net labels, 54 wire segments,
and 5 junctions. All embedded library definitions were found.
147 geometry items were explicitly marked as estimated.

A side-by-side diagnostic overlay was generated against the existing MCU
PNG. This is not a new layout, and the scene is not yet a safe replacement
for KiCad's renderer or the post-route netlist verifier. It demonstrates
the typed geometry model working on actual authored schematic data.


## Geometry-aware CP-SAT and libavoid integration

The first integration uses a shared, circuit-independent adapter:
`backend/schematic/geometry_constraints.py`.

- **CP-SAT**: `patch_cpsat_node_cluster.py` reads a pre-placement
  `GeometryScene` from the authored KiCad file, transforms symbol + annotation
  reservations to candidate positions and 90-degree rotations, rejects
  candidates intersecting fixed reservations, and adds pairwise candidate
  incompatibilities for cross-symbol geometry and short attachment stubs.
  The solver returns diagnostic counts of retained candidates, rejected
  candidates, and consumed geometry.
- **libavoid**: `patch_libavoid_router.py` reads the same scene; its primitive
  symbol-body obstacles are supplemented by visible annotation regions
  protruding beyond the body and by placed net-label regions. It also checks
  completed routes against those reservations. The normal own-pin escape
  exception is retained for pin names/numbers, not for foreign annotations
  or references/values.
- **Boundary**: Existing wires and inferred net names are *not* currently
  imported as hard obstacles. We cannot declare wire-to-wire crossings legal
  or illegal without real net attribution and explicit junction semantics.
  Text bounding boxes are currently estimated rather than KiCad glyph
  outlines, so these geometry checks may be conservative. Hard QA acceptance
  remains the electrical KiCad checks and normal visual verification.
- **Generality**: The same adapter handles all design classes. It does not
  inspect symbol designators, library names, circuit class, clock/reset net
  classes, or fixture names.

These adapters are active in the recovery branch's patched CP-SAT node-cluster
and grouped libavoid experimental workflows; production engine cutover has
not occurred. The lightweight CI workflow compiles both patched paths and
tests reservation rotation, field collisions, libavoid obstacle generation,
and explicit route-intersection detection.

The previous circuit-diverse render exposed further non-generic macro
assumptions (`mcu_core` anchor and hard-coded `power_entry`/`+3V3` flow).
Both were eliminated: macro anchoring now depends on block membership size,
and power/ground flow edges depend on physical wiring intent instead.

For real visual tests retain same-scale *before/after* MCU images and render
RC filter, op-amp amplifier and regulator independently. A passing CP-SAT
constraint unit test is not evidence of improved human readability.


### Verified four-fixture KiCad run (October 8, 2026)

Workflow: `Recovery KiCad Visual Regression`, GitHub Actions run
`37768251890`, artifact `schematic-recovery-geometry-and-fixture-validation`
(ID `11545822792`).

All four circuits passed the **strict KiCad netlist verification** and MCP
connectivity acceptance gate. ERC was WARN, with no blocking errors, on
every fixture. Visual QA was WARN in all four, so the generated diagrams are
*not* yet acceptable as high-quality engineering documentation.

| Fixture | Verified nets | Verified NC | Cosmetic score |
| --- | ---: | ---: | ---: |
| RC filter | 3 | 0 | 93 |
| Non-inverting amplifier | 6 | 3 | 81 |
| Linear regulator | 3 | 0 | 86 |
| MCU | 10 | 9 | 96 |

Actual side-by-side comparisons show **only modest MCU visual change**.
The RC and amplifier demonstrate that the cosmetic metric does not currently
represent human readability: the RC is stacked vertically around its
connectors and the amplifier uses large wire detours. Do not convert this
baseline into circuit-specific placement rules. Improve shared composition
objectives, compactness, pin-facing orientation, and the text/reservation
measurement model instead.

Integration discovered a **generic power-net bug**: `NetIntent.kind="power"`
does not imply that `power:<net-name>` exists in the installed KiCad
symbol library. With an arbitrary rail such as `VIN`, an attempted power
symbol could leave endpoints electrically isolated. The MCP terminal writer
now probes real KiCad library symbol geometry. If no symbol is present, it
adds **global net labels**, preserving connectivity without hard-coded rail
names. The independent fixtures now declare actual power drivers through
ordinary `PWR_FLAG` authoring auxiliary symbols and appropriate net intent,
not through circuit-specific engine branches.

Libavoid's geometry review recorded four approximate-text crossings in
the MCU grouped-route transaction, four in the RC filter, three in the
amplifier. These are surfaced explicitly in `presentation_constraints`
rather than aborting electrical authoring on uncalibrated glyph envelopes.
Verified graphic obstacles remain strict. The label- and wire-attachment
model needs accurate rendered glyph measurements and exact net attribution
before stronger acceptance gates are justified.

Next targeted work: improve candidate composition across all relation
motifs (not only CP-SAT node clusters), introduce length/bend/flow objectives,
and make field placement a reserved pre-routing step. The new fixture
gallery and before/after MCU image should be retained for real regressions.


## 2026-10-08 generic composition regression and safety rollback

The experimental geometry-scored per-component placement was **not**
accepted as safe. The full KiCad run `37791235662` failed the strict
connectivity gate because BOOT0, OSC_IN and OSC_OUT formed an unintended
physical connection. Earlier iterations also found that arbitrary terminal
stub fallback wires could cross unrelated nets. The electrical acceptance
gate detected these bugs correctly.

For now, production-path MCP authoring was restored from the last
electrically validated revision (`86d784515`). The new modular
`terminal_placement.py` remains an independent
experimental building block, with unit tests, but **is not active in the
validated path**. Do not re-enable their placement mutations without an
explicit net-aware route feasibility model that checks pin ownership,
already routed wires, crossing semantics, and possible shorts **before**
authoring. Geometric overlap tests by themselves are insufficient.

The generic `block_flow.py` macro ordering, inferred strictly from
declared input/output interfaces, remains active when no explicit horizontal
block hints are supplied; user block hints take precedence. The descriptive
`composition_metrics.py` reporter remains active.

The follow-up full KiCad run `37792315512` (artifact
`schematic-recovery-stable-routing-semantic-flow`) passed strict netlist
verification for all four fixtures, with ERC warnings but no blocking
errors. Same-native-scale before/after images were rendered directly from
KiCad SVG exports. Results:

| Fixture | Before wire mm | After wire mm | Before extents mm | After extents mm | Before/after cosmetic |
| --- | ---: | ---: | --- | --- | --- |
| RC filter | 219.71 | 153.67 | 27.43 × 104.08 | 120.08 × 43.81 | 93 / 96 |
| Amplifier | 914.40 | 735.33 | 165.48 × 220.28 | 280.10 × 112.72 | 81 / 81 |
| Regulator | 142.24 | 129.54 | 53.21 × 97.16 | 188.66 × 41.91 | 86 / 96 |
| MCU | 508.00 | 508.00 | 209.42 × 90.17 | 209.42 × 90.17 | 96 / 96 |

These are **not unqualified visual improvements**. In particular, generic
left-to-right macro flow reduces wire length while increasing drawing
width; the amplifier still overlaps symbols. A reduced wire-length metric
must not be treated as sufficient proof of improved readability.


## 2026-10-08 post-route quality selection and dogleg reduction

A post-authoring quality selector (`backend/schematic/quality_selection.py`)
now measures **actual authored KiCad wires and drawing extents**, rather
than trusting ELK/CP-SAT estimated scores. For each new scratch schematic,
the workflow independently builds the previously validated floorplan
profile, and then evaluates compact and roomier macro variants. Each trial
begins from the same input schematic, uses a file transaction, and MUST pass
strict exported KiCad netlist verification and ERC. New symbol-overlap pairs,
materially longer wire length, or increased total area are disqualifying.
When width and height trade off, a taller but smaller-area, shorter-wired
diagram is permitted. The selected schematic and its diagnostic JSON are
restored from the same winning trial. Per-trial KiCad PNGs are now retained
for visual inspection.

Libavoid's experimental grouped-route writer imports
`backend/schematic/route_simplification.py`. It tests orthogonal
shortcuts to existing routes and accepts a replacement only if it does
not contact measured obstacles, foreign pin endpoints (including unused
pins of the same endpoint symbol), existing wires of unknown ownership,
or previously routed foreign-net conductors. This is intentionally
conservative. The full exported KiCad netlist check remains mandatory;
the simplifier is not a substitute for electrical topology verification.

Full four-fixture validation:
`https://github.com/BigBadLoser/kicad-ai-engineering-companion/actions/runs/37823081409`,
artifact `schematic-recovery-selected-quality-plus-trial-renders`.
All four fixtures passed the strict KiCad netlist gate; unit check
`37823114241` also passed.

| Circuit | Previously released wire mm | Selected wire mm | Selected profile | New overlaps |
| --- | ---: | ---: | --- | --- |
| RC filter | 153.67 | 104.14 | compact | none |
| Amplifier | 570.23 | 629.92 | compact | none (existing U1/R1 overlap remains) |
| Regulator | 175.26 | 129.54 | verified | none |
| MCU | 508.00 | 510.54 | compact | none |

The selector compares against the conservative **same-run** baseline, not
against an archived golden output from earlier software revisions. Thus an
amplifier can improve over that run's conservative baseline (727.71 mm)
while still regressing against a previous revision's compact result
(570.23 mm). CI currently surfaces this comparison; it does not enforce
historical release-to-release nonregression. A persisted prior-accepted
baseline will be needed for that additional guarantee.

The MCU grouped router reduced two characteristic paths from three bends
to one: `BOOT0:U1.1->R2.1` and `OSC_IN:X1.1->C3.1`. However,
`OSC_IN:X1.1->U1.2` still uses six bends, because the safe local shortcut
search cannot reroute around the surrounding BOOT0/OSC_OUT geometry.
Meaningful further improvement requires net-attributed multi-route
planning/Steiner junction and placement refinement, not MCU-specific
special cases or unchecked wire crossing.

All comparison PNGs should retain a shared physical pixel scale within
each side-by-side before/after pair. Deliver these inline in chat rather
than requiring archive extraction.

## 2026-10-09 placement, terminals and routing as cooperating stages

The sandbox (`scripts/ci/sandbox`, see its `FINDINGS.md` for the measurements behind every point below) showed that
the engine's failures were mostly seams between stages, not weaknesses of ELK, CP-SAT or libavoid: placement, the
power-terminal stage, the label stage and the router each modelled symbols, text and wires differently, and a stage
that could not live with another's output either failed late or drew an unchecked wire. The engine now works as
follows.

**One model of the sheet.** `backend/schematic/kicad_geometry.py` builds the typed scene every stage reads. Its text
boxes are calibrated against KiCad's own rendering (`sandbox.py probes --only text_boxes` checks them), it honours a
library symbol's hidden pin numbers and names, and field text turns with its symbol.

**One planner for everything that hangs off a pin.** `backend/schematic/terminal_placement.py` plans a stub and the
symbol at its end for power symbols and for net labels. It keeps the end symbol (graphic and text), the stub, other
pins, wires and all text clear of one another, lets a stub cross a wire at a cost (KiCad connects nothing there) and
refuses to touch or overlap one. When nothing fits it fails with the pin and what turned its candidates away. The
unchecked 20.32 mm fallback survives only in the legacy layout, which is the rollback baseline. The one symbol on a
rail that several pins share (`sch_route_pin_rail`, used for decoupling banks) is placed by the same module
(`choose_rail_symbol`): the middle of the rail if it is free, else the nearest free place along it or just past its
ends, in a tight layout.

**Placement room is a policy, and the policy iterates.** `backend/schematic/layout_iteration.py` holds a
`LayoutPolicy`: how much room each symbol keeps (MCP Pro's fixed box, or the symbol's body and pin tips plus a margin
per reference), and how the router treats earlier wires. `SemanticGrammarScratchWorkflow.build` runs the verified
baseline, then the compact profile on a tight layout. A failed build, or a build that verifies but overlaps itself, is
classified (`classify_failure`, `clutter_failure`, since removed) into the symbols it involved, and the next attempt gives exactly
those symbols more room, or switches the router's treatment of earlier wires; a candidate that built but still has
clutter is tried again the same way. The compact profile also runs on MCP Pro's boxes with tight terminals, and the
better candidate by `quality_rank` is kept (tight placement is smaller everywhere, but its wire is not always shorter).
Every candidate still has to pass ERC and the netlist check and must not
regress against the baseline in wire, area, new symbol overlaps or text clutter (`quality_selection.py`). Symbol
overlaps are measured from the library graphics in the same geometry scene as the other metrics
(`symbol_overlap_pairs`), not taken from MCP Pro's visual-QA finding: that finding widens a symbol by a pin length
past its pin tips and names power symbols by random numbers, so it called a power symbol 7 mm from a body an overlap and
could not tell one candidate's from another's. A build that fails keeps the sheet it stopped on
(`quality-trials/NN-profile/failed.kicad_sch`) so the placement a stage could not live with can be looked at.

**Routing.** Every profile routes all nets in one grouped libavoid call. The non-baseline profiles add ownership
checks: a route may not touch another net's pin or wire (it may cross one in the interior of both), and the shared-net
junction planner keeps clear of other nets' pins and of the router's keep-outs. The per-net batch scheduler was removed.

**Tool calls inside tools.** MCP Pro's `call_tool` turns a tool's exception into an error result, so a tool that
called another and ignored the result carried on as if it had worked. Nested calls now go through `call_checked`.

Experiments use environment variables rather than source patches: `KICAD_AI_LAYOUT_MODE`, `KICAD_AI_LAYOUT_MARGIN_MM`,
`KICAD_AI_STUB_OBSTACLES` and `KICAD_AI_LAYOUT_ATTEMPTS` (see the sandbox README; the last went with the trial loop).

## 2026-10-09 reading like an engineer

Wire length, corners and area say how compact a drawing is, not whether it reads well, and a short wire can still
leave a decoupling capacitor 50 mm from its chip. The engine is now also measured, ranked and gated by what a reader
notices (the measurements, the human reference band and the evidence for each change are in the sandbox's
`FINDINGS.md`, section 13).

**Measured.** `backend/schematic/wire_nets.py` groups the scene's wires into the conductors KiCad would (a pin joins
the *end* of a wire, a label joins anywhere along one, two wires that merely cross join nothing) and finds the
crossings. `backend/schematic/readability.py` counts reference/value text with a wire through it, labels drawn
vertically, crossings, wires drawn through a symbol's body, tiny jogs, detours, power symbols entered from the side and
how far each support part is from the part it serves, with the places they are. `quality_selection.py` ranks candidates
by them (a support part beyond its limit costs more than any wire it saves, so an area saved by pulling a part away is no
gain) and refuses a candidate that reads worse than the baseline: more far support parts, more text on wires, more
crossings, a wire through a body. A candidate that reads strictly better and nothing worse may be larger than its
baseline (up to 1.6 times the area and 1.15 times the wire; otherwise 1.05 and 1.02), and a drawing may be reshaped but
not stretched (neither side may pass the baseline's longest side by half): a sheet is larger for a reason when it has
room for its text and a clean flow, not when a part was left far away. The sandbox's
`readability` command applies the same measures to the shipped and the selected sheet of every fixture and to KiCad's
91 demo projects.

**Placed.** Every profile but the verified baseline does the following; the baseline keeps what MCP Pro wrote.

* Reference and value text is placed by `backend/schematic/field_placement.py`, one CP-SAT problem over text boxes
  measured with the calibrated text model, through the `sch_place_fields` tool: MCP Pro writes a reference above its
  symbol and a value below it on the symbol's axis, which is on the wire for a part in a vertical chain. MCP Pro's own
  readability repair (`sch_fix_readability`) is not run over text this planner placed: it auto-places fields again, back
  on pins and wires.
* A net label's stub prefers to end horizontally, a stub that has to turn goes away from the pin beside it, a power
  symbol is entered along its own axis (down into a ground symbol, up into a supply symbol), and labels are planned
  before power symbols so the labels get their straight runs (`terminal_placement.py`, `ElkScratchWorkflow` net order).
* A shared rail (`sch_route_pin_rail`) is drawn only when its wire and stubs do not meet a wire already on the sheet, a
  symbol or a pin; otherwise each pin gets a symbol of its own (`rail_obstructions`). A decoupling bank whose ground rail
  ran across the supply wires to its own capacitors is the case.
* A power flag rides on a wire of its net instead of getting a stub and a power symbol of its own
  (`flag_attachment.py`, `flag_edit.py`, the `sch_attach_power_flags` tool). KiCad joins a pin to the ends of wires only,
  so the wire is cut where the flag stands.
* A part with no declared relation is placed from the relation its nets imply (`relation_inference.py`): a part between
  two pins of one larger part is a series element beside them, a part from a rail to a pin of a larger part hangs on that
  pin with its far pin toward the rail, bypass capacitors on supply pins are a bank in line with the pins or hang beside
  them. Declared relations are never changed, and a relation the compiler refuses is dropped; a placement that cannot be
  solved with inferred relations is tried again without them (`LayoutPolicy.infer_relations`).
* A connector faces the circuit with its pins in order, pin 1 above pin 2 down a side that faces left or right. Turning
  it half a turn puts its pins on the other side but reverses them, so it is mirrored instead
  (`orientation.py`, `symbol_edit.py`, the `sch_set_symbol_mirror` tool). The server's geometry helpers read each placed
  symbol's mirror (`parse_schematic_file` adds it), so terminals, rails, routes and text keep to the pins as drawn.
* A part that reaches no larger part but shares a node with the far pin of a part just hung (an LED's resistor, the LED
  hung on a transistor's collector) hangs on that far pin, in the direction the chain already runs. A hung part with no
  rail to point it is told which way its far pin goes (`BranchRelation.extend`), and the engine profiles weigh that
  direction against a quarter turn (`orientation.ENGINE_EXTENSION_WEIGHT`) so a diode stands up in a vertical chain
  instead of lying across it; the verified baseline keeps the milder preference it always had.
* Labels already on the sheet are fixed obstacles for the stubs and end symbols planned after them (a net with no
  library power symbol gets its labels one pin to a call, and the second stub used to run through the first label), and an
  end symbol laid over a wire already drawn costs more than a crossing.
* The cosmetic loop (MCP Pro's score-and-fix passes after everything is placed) does not run its orientation fixer in the
  guarded profiles. It turns every power symbol and flag back to 0 degrees, which put a flag back on its wire with its
  text upright across the connector beside it and made every candidate of such a sheet fail `clutter`, so retries were
  spent adding margins round the flags (`_planned_cosmetic_categories`).
* In the net-aware router a segment beside its own symbol is let off the body check only if it leaves the way the pin
  points (`route_simplification.leaves_pin_outward`); a shortcut that ran straight back across a connector from a pin
  on its far side was drawn through the connector.
* A candidate is retried for what a reader notices, most pressing first: overlaps (more room for the symbols involved),
  crossings (router boxes round earlier wires), labels standing on end (more room for the symbols whose pins carry
  them). The first defect that has a new placement to try decides the next attempt.

**New tools.** `sch_place_fields`, `sch_attach_power_flags` and `sch_set_symbol_mirror` are registered in
`mcp_client.GUARDED_WRITE_TOOLS`, in `allowed_tool_names` at the end of `build_extended_server` and in the CI tool lists
of both workflows.

The sandbox runs a fixture with its declared relations removed (`<fixture>_bare`) to measure the inference against them.


## 2026-10-10 composition, and circuits the engine was not built on

Section 13 of the sandbox's `FINDINGS.md` left the sheets reading well on local counts and short on composition; this
step chooses position and orientation from the wires that will be drawn, and tests the engine on circuits it was not
built on (the evidence is section 14 of `FINDINGS.md`).

**Placement refinement** (`backend/schematic/placement_refinement.py`, pure: positions, pin geometry and nets in,
positions and orientations out; fed by `placement_plan.py` from the geometry the read tool `sch_symbol_geometry` reports).
For the symbols no relation places (and, within 6.35 mm and without turning, the support parts a relation placed) it
searches positions and orientations for the lowest cost of: the routes the router will draw (estimated leaving each pin
along its direction; a net of three or more pins is one run with T joins), crossings (stubs of labelled and power pins
included), routes through a symbol or over a pin of another net, the room in front of every pin (kept clear of bodies and
of other nets' wires, as wide as a power symbol and its name), a power symbol entered along its axis, a connector's pins in
order, and the extent. It is never worse than its start and stops after a count of costed poses. `seed_placement` brings
the free symbols that MCP Pro's auto-layout scattered in beside their partners, within their block, before the floorplan
measures the blocks; `align_groups` slides each block as one piece to where its connections run straightest after the
floorplan. `LayoutPolicy.refine` switches all three off, and a build failure that names a symbol the refinement moved is
answered without the refinement first (`next_policy(refined=...)`, since removed).

**Two placements.** A circuit is placed from the relations its nets imply (the existing recipe) and, as a second trial,
from the netlist alone (`LayoutPolicy.infer_relations` off: each block laid out by ELK from how its nets join its parts,
`schematic_elk_planning.netlist_layout_edges`, then seeded, refined and aligned). The engine's trial list starts with the
verified baseline, the relations, the netlist and MCP Pro's boxes (a circuit whose nets imply no relation skips the
duplicate), and `quality_rank` keeps the better. A relation that cannot be solved on a sheet is dropped and its parts are
placed as free symbols; a relation avoids only what is placed in its own block.

**Robustness found by redrawing KiCad's demos** (`sandbox.py demos`, `redraw`): a trial that fails for a reason no
placement answers (a tool timeout, a stage's own check) no longer ends the build when an earlier trial built;
`backend/kicad/library_cache.py` keeps MCP Pro's symbol libraries and what is derived from them, which took one
`sch_place_node_cluster` from 60 s to a few; a candidate much smaller than its baseline that reads better may use more wire
(`WIRE_ALLOWED_PER_AREA_SAVED`).

**The page and the text.** The auto-layout drops the anchors wherever it likes, so a compact drawing can stand off the edge of
the page, `sch_visual_qa` then reports a symbol off the sheet, and `sch_fix_readability` grows the paper and also places every
reference and value again by its own rule, undoing the engine's field placement (two texts of the inductor drawn along the part
and one over the other, `auto-demo7`). When the engine placed the fields itself, an overflowing sheet now only grows the paper
(`_grow_sheet`: `sch_set_sheet_size`, one size at a time, nothing else touched); the baseline is unchanged. A power flag on
an ordinary net (a regulator input no pin drives) is placed beside a pin of its net like the flags of power nets: left where
the auto-layout dropped it, it stood 76 mm below the LM317 supply.

**The result does not depend on where the layout stands on the page.** Bringing the drawing to the corner of the page was
tried (it would have kept the paper small) and measured: moving the same layout by 178 mm changed three power terminals
and the opamp's wire by a third, so the step was dropped and the cause found. Three places decided ties or boundary cases by
float noise, which depends on absolute coordinates: the terminal planner (`terminal_placement.py`: sums of grid multiples
compared with clearances that are grid multiples, scores of equal paths), the net topology planner (`shared_net_routing.py`:
the minimum spanning tree sorted on unrounded distances, a bend counted for two coordinates that differ by noise), and
libavoid itself (offline, 31 of 120 recorded problems moved by whole grids were routed differently; the engine now poses
each problem from a grid corner, `route_simplification.router_origin`, and puts the origin back after the snap). Every
boundary comparison in the two planners carries a 1e-6 mm slack and distances and scores are rounded. The invariance is
tested at each level on random scenarios moved to six places on the sheet (`test_schematic_terminal_invariance.py`,
`test_schematic_shared_net_invariance.py`, `RouterOriginTests`), and end to end: the opamp moved 178 mm and 51 mm comes out
with the same wires. The libavoid dumps of the sandbox are recorded in the posed frame; `sbx.dumps.absolute` reads them back
in the sheet's.

These planners and the router also run in the verified profile, so what ships changed where a tie was decided by noise: of the
14 circuits of the sandbox the shipped sheet is different on three (`rc_filter` 116.8 -> 121.9 mm of wire and one more corner,
the LM317 supply 763 -> 773 mm, the three-transistor amplifier 1,447 -> 1,449 mm with one more crossing) and identical on the
other eleven.

**New tools.** `sch_symbol_geometry` (read-only) is registered in `mcp_client.READ_ONLY_TOOLS`, in `allowed_tool_names` and in
the CI tool lists of both workflows. `sch_set_sheet_size` (MCP Pro's own tool, now called by the engine) is a guarded write tool
and is in the same lists, and `tests/unit/test_engine_tool_allowlist.py` fails when the engine calls a tool by name that its
client would refuse.
