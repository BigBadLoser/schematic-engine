# Measured findings

What the sandbox measured on branch `fix/schematic-engine-recovery`, commit `888ec07`, native Windows
(KiCad 10.0.4, MCP Pro 4.0.1, ortools 9.12.4544, `@mr_mint/elkjs-libavoid` 0.5.0, libavoid-js 0.5.0-beta.5).
Every number below comes from a run under `.work/runs/` (labels in brackets); each run's `manifest.json`
records the git head and the overlays. Variants are patches applied to a throwaway copy of the source, with one
exception: the junction-planner fix of section 6 (`shared_net_routing.py`, and `connects_or_overlaps` in
`route_simplification.py`) is in the working tree on branch `engine/net-aware-routing`, uncommitted, so the later runs
(`p1`-`p3`) include it; it only affects the non-`verified` profiles. From section 9 on the engine itself carries the
recipe, the layout loop and the shared planner (also uncommitted); sections 9 to 11 were measured through patches that
have since become engine features, section 12 runs the engine end to end, and section 13 runs it again after the work on
how the sheets read (the engine of `auto-v22`, also uncommitted), and section 14 runs it after the work on composition and
on circuits it was not built on (four of KiCad's demos, `auto-v27` and `auto-demo10`, also uncommitted). Reproduce a run with
`sandbox.py run --label X ...`; the commands are in the README.

Trust notes: the engine is deterministic, so one run per variant is a measurement, not a sample. KPIs are
computed from the saved `.kicad_sch` by `sbx/kpi.py`, independent of the engine's own score. The replay lab is
a model of the engine's router post-processing; see "How far to trust the replay lab".

Later commits: the remote branch has since gained 15 commits (up to `777f9e4`): a read-only router probe and
benchmark on synthetic graphs, CI jobs, and an opt-in `KICAD_AI_ROUTER_EXPERIMENT` switch for alternate libavoid
parameter sets. The default is unchanged: the shipped `verified` profile run on `777f9e4` [`tip-verified`] gives the
same KPIs as on `888ec07` to the last digit (499.11, 113.03, 397.51 and 129.54 mm wire for mcu, rc_filter,
amplifier and regulator). All eight overlays apply and the self-tests pass on both commits. That benchmark and this
sandbox overlap in intent but not in method: it sweeps libavoid settings on fixed synthetic graphs, this one runs
the real fixtures through the whole engine and replays the router calls the engine actually made.

## 1. What ships at HEAD

With the default `auto` profile the engine tries `verified`, `compact` and `roomy` and keeps the best that
passes. For all four fixtures the winner is `verified` [`base-auto`, `p-verified`]:

| fixture | wire mm | wired-net excess | stubs (count, mm, mm/pin) | corners | crossings | extent mm |
|---|---|---|---|---|---|---|
| mcu | 499.1 | 1.10 | 19, 193.0, 10.2 | 16 | 4 | 182 x 79 |
| rc_filter | 113.0 | 1.00 | 4, 48.3, 12.1 | 4 | 0 | 80 x 37 |
| noninverting_amplifier | 397.5 | 1.56 | 8, 99.1, 12.4 | 11 | 6 | 161 x 65 |
| linear_regulator | 129.5 | n/a | 13, 129.5, 10.0 | 1 | 0 | 184 x 30 |

A stub is a wire attached to exactly one declared pin: a power symbol or label attachment. The nominal stub
the workflow asks for is 5.08 mm. "Excess" is routed length over half-perimeter wirelength (exact for 2 and
3 pins); the amplifier's feedback net is 133 mm against a 71 mm minimum.

The three-profile search costs 3-4x a single profile (342 s for the MCU against about 115 s) and returns the
legacy profile every time.

### The newer profiles do not work on the two harder fixtures

Forced single profiles [`p-verified`, `p-roomy`, `p-compact`]:

| fixture | verified | roomy | compact |
|---|---|---|---|
| rc_filter | ok, 113.0 mm | ok, 106.7 | ok, 101.6 |
| linear_regulator | ok, 129.5 | ok, 160.0 | ok, 198.1 (4 crossings) |
| mcu | ok, 499.1 | FAIL: cross-net rule (BOOT0 route vs crystal wires) | FAIL: libavoid diagonal on OSC_OUT |
| noninverting_amplifier | ok, 397.5 | FAIL: cross-net rule (OUT) | FAIL: cross-net rule (FB) |

Where `compact` or `roomy` does pass, the engine's own policy rejects it [`quality-selection.json` of
`base-auto`]: `compare_quality` (`backend/schematic/quality_selection.py`) vetoes any candidate whose bounding-box
area exceeds the baseline's by 5%, even when its wire is shorter. On `rc_filter` compact has 13% less wire and is
rejected for +21% area; on the regulator compact has 53% more wire and two power symbols overlapping U1 and J1.

## 2. Footprint (review finding A1)

`e1_tight_footprint` gives placement, ELK sizing and compaction the tight symbol bounds plus a margin, instead
of MCP Pro's fixed 20.32 x 15.24 mm minimum box. Placement-only metrics come from the saved pre-route scene
(`sandbox.py placement`), so they exist even when routing then fails. Total half-perimeter wirelength, mm:

| margin | rc_filter | linear_regulator |
|---|---|---|
| HEAD (inflated box) | 152.4 | 185.4 |
| 1.27 | 69.9 | 115.6 |
| 2.54 | 94.0 | 134.6 |
| 3.81 | 108.0 | 167.6 |
| 5.08 | 130.8 | 190.5 |
| 7.62 | 168.9 | 242.6 |

Passive pitch (nearest-neighbour centre distance) at margin 1.27: rc_filter 17.8 -> 13.0 mm, amplifier 66.3 -> 13.1
mm, MCU 14.5 -> 10.2 mm. So the inflated box is real and costs a lot of layout area.

It cannot be removed on its own, because every later stage has its own clearances [`e1-m*`, forced compact]:

| margin | rc_filter | linear_regulator |
|---|---|---|
| 1.27 | FAIL (3 of 8 router portals trapped) | FAIL (3 of 11 trapped) |
| 2.54 | ok | FAIL: ERC, GND flag shorted to +3V3 |
| 3.81 | ok | FAIL: same ERC error |
| 5.08 | ok | ok, wire -14%, crossings 4 -> 2 |
| 7.62 | ok | ok |

At margin 5.08 the routed gain is small, and stubs grow with the margin (rc_filter 7.6-12.7 mm at 2.54,
12.7-17.8 mm at 7.62), because the power-terminal stage measures the same box (section 3).

## 3. Power terminals: stub length and an unchecked fallback

`sch_add_pin_power_terminals` (`backend/kicad/mcp_extended_server.py:417-687`) places a power symbol and wire for
each GND/supply pin. A terminal must clear the source symbol's inflated box by 3.81 mm, and the first candidate
distance is 5.08 mm. For a 7.62 mm resistor that box is 15.24 mm tall, so the first legal distance is 10.16-12.7 mm.
When no candidate passes, the code takes `fallback_direct_20.32` (lines 622-633): a straight 20.32 mm wire
with no collision check at all.

How often it fires [`p-*`, with the `e5_trace` overlay]:

| profile | mcu | rc_filter | amplifier | regulator |
|---|---|---|---|---|
| verified (ships) | 3 of 11 | 0 of 4 | 1 of 8 | 1 of 13 |
| roomy | 3 of 11 | 0 of 4 | 1 of 8 | 1 of 13 |
| compact | 3 of 11 | 0 of 4 | 3 of 8 | 6 of 13 |

So the shipped outputs contain five blind 20.32 mm wires. It is not only cosmetic: in `e1-m2.54` the regulator's
fallback wire from J2.2 runs up x = 68.58 through both pin tips of C2, including C2.1 (+3V3). That joins GND to
+3V3, and KiCad's ERC reports the GND power flag and U1's output as two connected power outputs
[`dx-m2.54-reg`, `candidate-*.kicad_sch` in its dumps]. In `dx-m1.27-rc` J1.2's fallback wire runs down
x = 160.02 straight through J2.1, a VOUT pin.

### Tight geometry in the terminal stage alone [`t-verified-tight`, overlay `e6_terminals_tight`]

Giving only this stage tight symbol bounds (placement untouched, shipped `verified` profile):

| fixture | wire mm, HEAD -> tight | stub mm/pin | corners | unchecked fallbacks |
|---|---|---|---|---|
| linear_regulator | 129.5 -> 76.2 (-41%) | 10.0 -> 5.9 | 1 -> 0 | 1 -> 0 |
| rc_filter | 113.0 -> 85.1 (-25%) | 12.1 -> 5.1 | 4 -> 2 | 0 -> 0 |
| mcu | 499.1 -> 405.1 (-19%) | 10.2 -> 5.2 | 16 -> 15 | 3 -> 0 |
| noninverting_amplifier | 397.5 -> FAIL | 12.4 -> 5.1 | | 1 -> 0 |

Stubs reach the nominal length and the fallback never fires. The amplifier fails ERC, and the cause is a
different missing link: the signal router is not told where power symbols are. R1.1's shorter GND stub now ends
at (146.05, 123.19), and the router sent OUT's feedback wire along y = 123.19 straight through that GND symbol's
pin. The KPI independently reports `shorted nets: GND, OUT` for that candidate. At HEAD the long stubs end
beyond the routing channels, so the collision never happens.

Two follow-ups:

* The clearance is not the lever, the bounds are. Lowering the 3.81 mm clearance to 1.27 mm
  [`t-verified-tight-c1.27`] gives the same MCU (405.1 mm) and `rc_filter` (85.1 mm) and a regulator at 71.1 mm
  (7% shorter than at 3.81 mm); the amplifier fails the same way.
* On the net-aware `compact` profile, which does treat earlier wires (power stubs included) as obstacles, the same
  change [`t-compact-tight`] takes `rc_filter` from 101.6 to 73.7 mm and the regulator from 198.1 to 86.4 mm with
  its crossings going from 4 to 0, against 113.0 and 129.5 mm shipped. The MCU and amplifier still fail there for
  the reasons in section 1.

## 4. Router behavior

The dumps (`e3_dump_libavoid`) hold the exact payload and libavoid's raw answer for every grouped call.

* **Trapped portals.** The engine models each pin as a 0.02 mm "portal" shape 2.54 mm beyond the pin tip, inside
  keep-outs of 0.635 mm (obstacle clearance) plus 1.27 mm (`shapeBufferDistance`). A foreign body within
  2.54 + 1.905 = 4.445 mm of a pin tip, in the pin's direction, puts the portal inside that body's keep-out.
  libavoid then has no valid start: it returns a straight line (a diagonal, or a line through the body) and the engine's
  post-checks reject it. The KPI `trapped portals` predicts this from the placement alone; it was 3 of 8 at E1
  margin 1.27 for `rc_filter` and the failing edge was one of those pins [`dx-m1.27-rc`].
* **Detours from text keep-outs.** In the same run two pins 7.6 mm apart (Manhattan), facing each other, were
  joined by about 56 mm of wire, and a pin pair 8.9 mm apart by about 87 mm, because the Reference/Value text boxes
  and the 1.27 mm buffer close the 2.54 mm corridor between them.
* **Nudging is not the cause of the non-orthogonal failures** (review hypothesis B2). Turning
  `nudgeOrthogonalSegmentsConnectedToShapes` off gives the identical MCU failure [`b2-mcu-nudge-on`,
  `b2-mcu-nudge-off`], and the diagonal is already in libavoid's raw output. The recorded failure is a junction
  at (121.92, 57.15) and a portal at (116.84, 55.88) with a 1.27 mm offset, and the portal sits inside the buffered
  box of the BOOT0 wire that was routed first. This is routing-order and endpoint-geometry sensitivity.
* **The global buffer applies to the portal and junction shapes too.** Two portals 2.54 mm apart block each
  other, so a straight 2.54 mm connection came back as a U-shaped detour. Moving the same clearance into the real
  obstacles and setting libavoid's buffer to 0 (`buf0` in the replay lab) removes the MCU's non-orthogonal
  failure and cuts the bends on the shared edges from 6 to 3.
* **`simplify_route` does most of the shaping.** In the replay lab, over the 12 accepted edges of the `roomy` dumps,
  the bends fall from 24 in libavoid's output to 6 after the engine's simplification. Router variants then change
  which calls the engine can accept more than the shape of the ones it does accept.
* **Hyperedges are not reachable** (review experiment 0). The pinned `libavoid-js` exports no hyperedge API,
  `router.hyperedgeRerouter` is undefined, `JunctionRef` is not exposed, and only `displayRoute()` exists
  [`sandbox.py probes --only libavoid_surface`].
* **Crossings.** `verified` makes one grouped call with `protect_wires: False`
  (`scripts/schematic_scratch_workflows.py:1203-1233`); `compact` and `roomy` make one call per net with
  `protect_wires: True` (`1254-1274`), so every earlier wire is a hard obstacle and any intersection with one is
  rejected as "unknown net ownership". The cross-net failures that sink `roomy` and `compact` are of this kind, and
  KiCad would not connect a bare crossing. `verified` ships layouts with 4 (MCU) and 6 (amplifier) crossings between
  separate wire components that pass the netlist gate.

## 5. Other measured facts

* CP-SAT runs once in the whole MCU fixture, for a two-part cluster (R1 and SW1 at U1.4: 896 candidates, optimal),
  and not at all on the other three. All other placement is greedy.
* Pin directions and the MCP Pro pin transform are correct (196 pin/rotation pairs, 0 disagreements); KiCad 10.0.4
  agrees with MCP Pro's transform in 12 of 12 orientations and with `kicad_geometry._transform` in 8 of 12 (wrong
  for mirrored 90 and 270 degrees). Probes: `sandbox.py probes`.

* (The per-net scheduler and its `e4_net_order` overlay were removed when the per-net profiles became one grouped call.)
* Shuffling the net routing order [`ordnet-s1`, `ordnet-s2`] changes nothing on the shipped profile: identical KPIs
  to `p-verified` for all four fixtures. That is a null test by construction, because `verified` never calls the
  batch scheduler the overlay shuffles. Order matters on the per-net profiles, where the MCU failure comes down to
  BOOT0 being routed before the crystal nets.

## 6. Sequential versus simultaneous routing

Does committing nets one at a time cause the `compact` and `roomy` failures? Those profiles make one libavoid call
per net, with every earlier wire a hard obstacle and any intersection rejected. `e8_single_call_routing` sends them
through the single grouped call that `verified` uses instead, leaving placement and the shared-net junction
planning as they are. Wire length in mm, compact placement; the shipped profile is `verified`:

| variant | mcu | rc_filter | amplifier | regulator |
|---|---|---|---|---|
| shipped (`p-verified`) | 499.1 | 113.0 | 397.5 | 129.5 |
| sequential (`p-compact`) | FAIL | 101.6 | FAIL | 198.1 |
| single call (`s-compact`) | 534.7 | 101.6 | FAIL, short | 198.1 |
| + tight power terminals (`st-compact`) | 435.6 | 73.7 | FAIL, short | 86.4 |
| + no pin-through junction shortcut (`n-compact`) | 435.6 | 73.7 | 252.7 | 86.4 |
| + ownership checks, legal crossings (`ng3-compact`) | 425.4 | 73.7 | rejected | 86.4 |
| + planner fix, no ownership checks (`p3-compact`) | 435.6 | 73.7 | 287.0 | 86.4 |
| + planner fix and ownership checks, legal crossings (`p3g-compact`) | 425.4 | 73.7 | 243.8 | 86.4 |

`roomy` placement gives the same MCU numbers and an amplifier failure of the same kind, a short (`s-roomy`,
`st-roomy`).

* **Sequential commitment caused the MCU failure.** The single call alone fixes it on both profiles, and with tight
  terminals the MCU is 13% shorter than shipped.
* **A single call without ownership checks commits shorts.** The amplifier's FB junction was planned at
  (91.44, 137.16), in the same column as pins U1.2 and U1.3. The straight pin-to-junction shortcut
  (`mcp_extended_server.py`, `direct_junction_route`) then drew a wire from U1.2 through U1.3's pin tip, shorting FB
  to IN. The shortcut checks other symbols' bodies and text but never the endpoint's own other pins, and the grouped
  call has no foreign-pin check. Disabling the shortcut (`e9_no_direct_junction`) makes the amplifier pass:
  252.7 mm against 397.5 shipped, excess over the wired nets 1.56x down to 1.12x, crossings 6 down to 3.
* **`n-compact` passes all four fixtures** with no shorts, no foreign body hits, no overlaps and no unchecked
  fallbacks. By my own wire and footprint measurements every fixture beats the engine's acceptance rule (wire within
  2% and area within 5% of the baseline), so `auto` should pick `compact`. That has not been run through `auto`.
* **The ownership checks catch the remaining problem, but nothing repairs it.** With the net-aware checks on in the
  single call (`e10_guarded_grouped_call`) and legal crossings accepted (`e7_allow_crossings`), the amplifier is
  rejected before anything is written: IN's wire runs straight down x = 91.44 to U1.3, and the planned FB junction
  sits on that wire. That is the case for coordinated junction placement, because a junction chosen without regard to
  the other nets' pins and routes lands on someone else's wire. The engine has no way to pick another junction and
  retry, so the build fails. The guarded MCU is better than the unguarded one (425.4 mm, 13 corners, no jogs; not
  traced why).
* **Fixing the planner instead of adding a repair step works for this case.** `plan_shared_net`
  (`backend/schematic/shared_net_routing.py`) now (a) penalises a junction whose straight branch to a terminal would
  pass through another net's pin tip, and (b) rejects junction candidates inside the 1.5-grid keep-out the router
  keeps around each symbol (0.5 grid of inflation plus libavoid's 1 grid buffer). Part (a) alone moved the
  amplifier's FB junction from (91.44, 137.16) to (91.44, 146.05), which sat on the edge of U1's keep-out, and
  libavoid then routed through U1.3's pin tip; part (b) closes that. With both and the ownership checks, `p3g-compact` passes all four fixtures: no shorted nets, no foreign body hits, no overlaps,
  no unchecked fallbacks, no trapped portals; MCU 425.4 mm (13 corners, 0 jogs, excess 1.06), amplifier 243.8 mm (8
  corners, 0 jogs, excess 1.07, 5 crossings). Without the ownership checks (`p3-compact`) the amplifier is 287.0 mm
  with a jog, so the checks and the planner fix are separate contributions. Five unit tests cover the planner
  change, one of them a seeded property test over 150 random layouts.
* **Not every KPI improves.** `rc_filter` gains one jog (0 to 1) while losing a corner, and the regulator goes from
  1 to 2 corners. The amplifier's passive pitch is 66.3 mm against 21.6 shipped, a property of the compact placement
  that was already 66.3 mm before any routing change (section 2); its drawing is still smaller (118 x 55 against
  161 x 65 mm). I have not looked at why.

## 7. Text collisions: a cost the other KPIs missed

*Provenance: the counts in sections 7 to 9 were made with the first version of the geometry reader. Section 11
shows that it invented text for hidden pin numbers and drew every box too narrow; the same runs counted with the
corrected reader are given there.*

None of the numbers above sees visible text. The engine's own `overlapping_pairs` is empty for every run in this
document, and the sandbox KPIs count only bodies, wires and pins. Counting text boxes that overlap another owner's
text or a symbol's graphics in the measured presentation scene (these boxes are estimates, so read the counts as a
comparison between runs, not an absolute):

| fixture | shipped [`p-verified`] | `s-compact` | `t-verified-tight` | recipe [`p3g-compact`] |
|---|---|---|---|---|
| mcu | 0 | 5 | 3 | 5 |
| noninverting_amplifier | 0 | fails | fails | 4 |
| rc_filter | 0 | 0 | 0 | 0 |
| linear_regulator | 0 | 0 | 1 | 0 |

(Each count is text/text plus text/graphic pairs.) In the recipe the MCU has a PWR_FLAG pin number over a power
symbol's value text, power text over flag graphics, and two net labels placed on top of U1 and J3's graphics; the
amplifier has two PWR_FLAG and power-symbol pairs. Compact placement causes some on its own (`s-compact`, shipped
stubs) and the 5.08 mm power stubs cause others (`t-verified-tight`, shipped placement): a power symbol 5.08 mm from a
pin tip puts its value text in the zone of the pin's number. This is the same kind of gap as the router not knowing
where power symbols are: the terminal stage measures bodies, not text. Wires crossing text boxes are common in both
(MCU 21 shipped, 22 recipe) and are not part of this count. `scripts/ci/sandbox/.work/review/results.html` shows
the shipped and recipe drawings side by side at a common scale.

## 8. Placement stability: relation order

With routing at its floor in the recipe (wired-net excess 1.0 to 1.08; wire budget in the review page), differences
between runs of the same fixture are differences in placement. `e4_relation_order` shuffles the order in which
placement relations are processed; six seeds through the recipe (`rs1-compact` to `rs6-compact`, MCU and amplifier):

| MCU, relation order | pre-route HPWL (all nets) | routed wire mm | corners | extent mm | text overlaps |
|---|---|---|---|---|---|
| fixture's own order [`p3g-compact`] | 593.1 | 425.4 | 13 | 160 x 79 | 5 |
| seed 6 | 590.6 | 417.8 | 15 | 160 x 75 | 11 |
| seeds 2 and 5 (same layout) | 824.2 | 510.5 | 16 | 194 x 108 | 9 |
| seed 1 | 831.9 | 523.2 | 13 | 194 x 108 | 13 |
| seeds 3 and 4 | placement fails: `Could not solve routing-aware relation for R2` | | | | |

* **The result depends on the order the relations are listed.** Two of six orders fail to place at all, three land
  in a larger arrangement with 20 to 23% more wire, one matches the fixture's own order (1.8% shorter). The
  amplifier is identical in all six. The greedy per-relation planner (`sch_place_pin_relation`) commits each
  relation once with earlier placements as obstacles, so an early choice can leave a later part, here R2, with no
  candidate that both clears the placed boxes and admits a projected route (the planner's own error), and nothing
  backtracks. I have not traced which of the two conditions fails for seeds 3 and 4.
* **Shuffling is not a source of improvement.** The fixture's own order is already among the best of the passing
  samples, so this measures fragility, not headroom. On the shipped profile one shuffle (`ordrel-s1`) gives the MCU
  604.5 mm against 499.1; a second seed in that run (`ordrel-s2`) never finished and says nothing.
* **A pre-route score ranks the layouts.** HPWL over all nets, computed on the placement before routing, orders the
  five passing layouts exactly as the routed wire does, and separates the two arrangements by a wide margin (590
  to 593 against 824 to 832). Signal-net HPWL alone does not order the two good layouts (245.1 against 247.7 while
  the routed wire is 425.4 against 417.8). Five points in two clusters is thin evidence for a score that could steer
  placement without routing each candidate.
* **Text overlaps are uncontrolled in every arrangement** (5 to 13 on the MCU), so they are not a property of one
  bad layout.

## 9. Tighter placement on top of the recipe, and why builds fail

The footprint sweep of section 2 (`e1_tight_footprint`: placement, ELK sizing and compaction use tight symbol
bounds plus a margin) re-run on top of the recipe [`fp3.81-recipe`, `fp2.54-recipe`, `fp1.27-recipe`; compact,
single call, ownership checks, legal crossings, tight terminals]. Wire in mm; 6 of the 12 builds fail:

| fixture | default footprint [`p3g-compact`] | margin 3.81 | margin 2.54 | margin 1.27 |
|---|---|---|---|---|
| rc_filter | 73.7 | 58.4 | 57.1 | fails |
| linear_regulator | 86.4 | 86.4 | fails | fails |
| noninverting_amplifier | 243.8 | fails | 231.1 | 184.2 |
| mcu | 425.4 | fails | fails | 317.5 |

Where a tight placement builds it is much smaller: wire is a further 21 to 25% lower on the MCU, amplifier and
rc_filter (against shipped: MCU 317.5 vs 499.1, amplifier 184.2 vs 397.5, rc_filter 57.1 vs 113.0), and the
drawing area roughly halves (MCU 160 x 79 to 86 x 67 mm, amplifier 118 x 55 to 65 x 39 mm). The price is routing
effort and clutter: wired-net excess rises to 1.32 (MCU) and 1.43 (amplifier), the MCU has 13 text overlaps (5
at the default footprint) and one trapped portal. So placement has large headroom that the downstream stages
cannot yet use, and failures do not follow the margin: the MCU fails at 3.81 and 2.54 but builds at 1.27.

Every failure traced so far is a seam between stages, not a placement or routing algorithm:

* **Power-terminal stage, unchecked fallback** (regulator, margin 2.54 [`dx2.54-reg`]): 3 of 13 terminals take
  `fallback_direct_20.32`. `J2.2`'s 20.32 mm GND wire runs up x = 68.58 through C2.1 (+3V3) and C2.2, so GND is joined
  to +3V3 and ERC reports two power outputs connected (`#FLG2` and U1.2). The same cause as section 3, now reachable
  at larger margins because dense placements leave no clear candidate. At margin 1.27 the build ends with the same
  ERC error (I did not trace its terminals), and 3 of 11 portals are also trapped.
* **Label stage, stub extended through another net's stub** (MCU, margin 3.81 [`dx3.81-mcu`]): 0 terminals on the
  fallback. U1.18's label stub, normally 5.08 mm, is 12.7 mm long (wire from (139.7, 66.04) to (152.4, 66.04)), ending
  at its label at x = 152.4. It runs over J3.3's GND stub (146.05 to 151.13) and through J3.3's pin tip, merging
  GND with UART_RX; the netlist gate reports 12 pins shorted. MCP Pro's `sch_add_pin_labels` appears to lengthen a
  stub until its label clears existing graphics (here the GND power symbol at (146.05, 66.04)), without checking the
  wire it creates against other pins and stubs. That is an inference from the geometry; I did not read the
  tool's extension logic.
* **Router versus earlier stubs** (amplifier at 3.81, MCU at 2.54): the ownership check rejects a net branch that
  touches or overlaps a pre-existing stub. The recipe no longer gives libavoid boxes around earlier wires
  (`e7_allow_crossings`), so it cannot avoid them.
* **Trapped portals** (rc_filter and regulator at 1.27: 3 of 8 and 3 of 11): libavoid has no path from a trapped
  portal, and the build ends with `libavoid route intersects symbol body`. These are predictable from the placement
  alone; the other failures above had 0 trapped portals.

The label stage is a third source of unchecked wire, after the terminal fallback and the router. None of the
generators checks its wires against the others', so each stage is safe only while the placement leaves room.

**Keeping boxes around earlier wires is not a fix.** The same sweep with `SBX_KEEP_WIRE_BOXES=1` (earlier wires stay
libavoid obstacles while crossings stay legal) [`fpb3.81-recipe`, `fpb2.54-recipe`, `fpb1.27-recipe`]:

| fixture, margin | no boxes | boxes kept |
|---|---|---|
| amplifier, 3.81 | fails (touches a stub) | 194.3 mm |
| amplifier, 2.54 | 231.1 | fails (libavoid returns a diagonal) |
| amplifier, 1.27 | 184.2 | fails (same) |
| rc_filter, 2.54 | 57.1 (excess 1.16) | 52.1 (excess 1.0) |
| mcu, 2.54 | fails (touches a stub) | fails (netlist short, GND merged with another net) |
| mcu, 3.81 and 1.27; rc_filter, 3.81 and 1.27; regulator, all margins | unchanged | unchanged |

The boxes rescue one build and improve another, but close the amplifier's channel at 2.54 and 1.27 (libavoid
finds no path and the engine rejects the diagonal) and move the MCU's failure at 2.54 from a rejection to a short.
Neither setting is robust alone. Taking the better of the two for each fixture and margin, every fixture has a
passing tight build, and the best are rc_filter 52.1, regulator 86.4, amplifier 184.2 and MCU 317.5 mm, which is 33
to 54% shorter than shipped (113.0, 129.5, 397.5, 499.1). Reaching them takes a search over placement and routing
settings in which a failing candidate is cheap and its reason is precise.

Visual cost: the tight MCU is smaller but visibly more cluttered (13 text overlaps against 5), with PWR_FLAGs and
power symbols crowding the connectors. Wire length and area alone would rate it better than it reads.

## 10. What an iterating placement would need (measured inputs)

* **Quality is predictable before routing.** Pre-route HPWL over all nets (`sandbox.py placement`) ordered 33 of 33
  pairs of placements of the same fixture the way the routed wire did (4 fixtures, 3 to 11 placements each, compared
  within one router setting, from the order trials and the footprint sweeps). Many pairs are far apart; one close pair
  (MCU, HPWL 590.6 against 593.1, routed 417.8 against 425.4 mm) is also right. It says nothing about text clutter or
  about whether a build succeeds.
* **Feasibility is not.** 4 of the 6 failed builds in section 9 had no pre-route warning (0 trapped portals); only the
  rc_filter and regulator cases at margin 1.27 were visible from the placement. The rest came from the terminal, label
  and router stages, so those stages have to act as the oracle and report located reasons (which pins, which zone)
  instead of error strings.
* **One oracle pass is slow today.** An MCU build spends about 159 s in 233 tool calls [`dx3.81-mcu`]: the single
  CP-SAT call (`sch_place_node_cluster`, a two-part cluster) 36 s (solving and model building not separated),
  `sch_add_wire` 19 s over 45 calls, `sch_auto_place_functional` 12 s, power terminals 12 s, the grouped libavoid call
  11 s. Every authoring call reads and rewrites the schematic file, so a wire costs 0.42 s. An inner loop therefore
  has to run on an in-memory scene, and oracle passes are worth a handful, not dozens.
* **An in-memory scene needs the right transform.** `kicad_geometry._transform` agrees with KiCad in 8 of 12
  orientations and is wrong for mirrored 90 and 270 degrees; MCP Pro's transform agrees in 12 of 12
  (`sandbox.py probes`).

## 11. Text boxes, measured against KiCad's own rendering

KiCad's SVG export wraps every text in `<g class="stroked-text"><desc>TEXT</desc>...`, so the bounding box of those
paths is the real extent of the text. Compared with the geometry reader's estimates on the MCU fixture (147 rendered
texts; ratio is estimated width over real width, offset is the mean distance between box centres):

| kind | width ratio before | after | centre offset before | after |
|---|---|---|---|---|
| net label | 0.77 | 0.98 | dx 0.10, dy 0.36 | dx 0.03, dy 0.03 |
| pin name | 0.64 | 1.00 | dx 2.22, dy 0.44 | dx 0.33, dy 0.11 |
| pin number | 0.79 | 1.00 | dx 0.14, dy 0.92 | dx 0.03, dy 0.11 |
| power text | 0.73 | 0.90 | | |
| reference | 0.83 | 0.97 | | |
| value | 0.77 | 0.96 | | |

What the reader got wrong, and what the engine now models:

* **Hidden pin text.** A library symbol hides its pin numbers and names with a symbol-level
  `(pin_numbers (hide yes))`. Power symbols, resistors and capacitors all do. The reader looked only at the pin's own
  node, so it reported 142 texts in the MCU where KiCad draws 97. Those phantom boxes counted as text overlaps and
  fed the router's text keep-outs.
* **Width.** KiCad's stroke font advances about 0.87 of the text height per character, less 0.22 of a character of side
  bearing. The reader used 0.65, so every box was about a quarter too narrow.
* **Positions.** A local label's text is centred on its anchor and rises 0.82 of its size above the wire. A pin name
  runs inward from the pin's inner end, past the symbol's pin-name offset. A pin number sits above the middle of a
  horizontal pin, or left of a vertical one. A global label's flag and text run away from its anchor.
* **Rotated symbols.** KiCad stores a field's angle relative to its symbol: a connector placed at 90 degrees with a
  field angle of 0 draws that text vertically. The reader ignored the symbol's rotation, so every field of a rotated
  part was boxed the wrong way round (the regulator's J1 and J2 values, 6.8 mm tall in the rendering, were boxed 6.4 mm
  wide).

The text-overlap counts of sections 7 to 9, recounted with the corrected reader (text/text plus text/graphic):

| fixture | shipped | recipe [`p3g-compact`] | tight [`fp1.27-recipe`] |
|---|---|---|---|
| mcu | 0 | 7 | 10 |
| noninverting_amplifier | 0 | 2 | 1 |
| rc_filter | 0 | 0 | |
| linear_regulator | 0 | 2 | |

(Before, with hidden text counted and narrow boxes: MCU 5 and 13, amplifier 4 and 2.) The direction of the finding
stands: the recipe's outputs are more cluttered than the shipped ones, and the sources are the same, power symbol text
over flag graphics and labels over symbol graphics. The calibration is covered by unit tests, and the table above is
reproduced by `sandbox.py probes --only text_boxes`, which reads a run's SVG, pairs each text with the nearest
estimate and fails if a kind drifts outside 0.8 to 1.2 of the real width or 0.6 mm of its centre.

## 12. The engine end to end: placement iterating with the stages after it

Sections 1 to 11 measured pieces one at a time, mostly as sandbox patches. The routing recipe, a layout policy that
iterates, and one planner for everything that hangs off a pin are now in the engine
(`docs/architecture/schematic-recovery.md`; uncommitted on `engine/net-aware-routing`), so `auto` can be measured as
it ships: the verified baseline, then the compact profile on a tight layout, where whatever fails or clutters the sheet
is answered by more room for the symbols it names, then the compact profile on MCP Pro's boxes, and the best candidate by
`quality_rank` is kept. `sandbox.py run --label X --overlay e5_trace` (the overlay only keeps what failed builds
discard). Four fixtures, one deterministic build each.

**Result** [`auto-v13`]. The shipped column is the verified trial of the same run, so both columns come from the same
measuring code (`quality_selection.py`).

| fixture | wire mm: shipped | engine | | area mm2: shipped | engine | | text overlaps | trials | s | what was kept (margins in mm) |
|---|---|---|---|---|---|---|---|---|---|---|
| mcu | 497.8 | 412.8 | -17% | 16834 | 8001 | -52% | 0 to 0 | 4 | 410 | tight: C1 2.54, C2 2.54 |
| noninverting_amplifier | 408.9 | 281.9 | -31% | 9890 | 4961 | -50% | 0 to 0 | 6 | 153 | tight: J1 3.81, R1 2.54, R2 2.54, U1 3.81 |
| rc_filter | 116.8 | 71.1 | -39% | 3667 | 2635 | -28% | 0 to 0 | 8 | 142 | tight: C1 3.81, J1 3.81, J2 3.81, R1 3.81, boxes round earlier wires |
| linear_regulator | 129.5 | 86.4 | -33% | 7977 | 3455 | -57% | 0 to 0 | 4 | 135 | MCP Pro's boxes |

Every selected sheet passes ERC and the exported-netlist check and has no symbol overlap and no text overlap (the
measure of section 11). Wire is 17 to 39% shorter than shipped and the drawing 28 to 57% smaller.

**Which family wins.** Tight placement and MCP Pro's boxes with tight terminals, each on its own, ranked by the engine's
`quality_rank` (wire + 0.015 per mm2 of area + 2.54 per corner + 20 per overlap + 8 per text overlap) [`auto-v12`,
`lt-v12`]:

| fixture | tight: wire mm, area mm2, rank | MCP Pro's boxes: wire, area, rank | kept |
|---|---|---|---|
| mcu | 412.8, 8001, 581.0 | 445.8, 14887, 709.7 | tight |
| noninverting_amplifier | 281.9, 4961, 389.4 | 284.5, 7119, 431.9 | tight |
| rc_filter | 71.1, 2635, 120.8 | 73.7, 3394, 132.2 | tight |
| linear_regulator | 111.8, 2495, 167.0 | 86.4, 3455, 150.9 | boxes |

Tight placement is the smaller drawing everywhere and the shorter wire on three of four; on the regulator MCP Pro's boxes
give 23% less wire for a 38% larger drawing, and the engine keeps whichever ranks better. (The earlier recipe of section
9 [`p3g-compact`] also gave 86.4 mm there; its other sheets carried the text overlaps of section 11, and the planner now
refuses those places.)

**What each trial history shows** [`auto-v12`, the tight family]. A trial that does not build names the stage that
stopped it and the symbols involved; the next one gives exactly those symbols 1.27 mm more room (3.81 at most), or
switches the router's treatment of earlier wires.

* MCU, 3 trials. At 1.27 mm the terminal stage finds no free place for the one ground symbol that C1 and C2 share: the
  text "GND" is 3.0 mm wide and 6.4 mm is all there is between the capacitors' pin tips and U1's. C1 and C2 get 2.54
  mm and it builds. (With the narrower power-symbol box the planner used until `auto-v11`, "GND" fitted beside C2 and
  the first tight trial was accepted at 440.7 mm; the box was about 20% narrower than the text, so that was the
  planner's mistake. Before the rail symbol had a planner at all [`auto-v9`, `auto-v10`] the first tight trial put "GND"
  on U1's pin names, three text overlaps, and U1 had to be given 2.54 mm to clear them.)
* Amplifier, 5 trials. Three routes come back with a diagonal segment, which is libavoid saying it found no way out of
  a pin; they name R1, R2, U1 and then J1, U1, and each named symbol climbs 1.27 mm. At J1 3.81, R1 and R2 2.54, U1 3.81
  it builds.
* RC filter, 7 trials. Five router failures (a route through R1's body, a diagonal, R1 again, a route touching a
  stub, a diagonal with boxes round the earlier wires) name C1, J2, R1 and then J1; with all four at 3.81 mm and boxes
  round earlier wires it builds.
* Regulator, 3 trials. J1 has no free exit for its VIN and GND terminals at 1.27 mm; at 2.54 it builds.

**Why the router fails on a tight layout.** The raw libavoid input of the RC filter's first tight trial [`dbg-rc3`]
shows it. The router leaves a pin through a portal 2.54 mm beyond the pin tip and keeps 1.27 mm (its shape buffer) from
every obstacle, which the engine inflates by another 0.635 mm: a pin tip needs 2.54 + 1.27 + 0.635 = 4.445 mm of free
space in front of it. J2.1's portal at (157.47, 139.70) is 0.88 mm from R1's obstacle (its right edge is 156.59), so it
lies inside R1's buffer; libavoid treats it as being inside the shape and returns a route whose last leg
(145.25, 139.70) to (157.47, 139.70) runs through R1. R1.2's portal is 0.64 mm from a power symbol. In the layout of
`auto-v10` trial 4 (C1, J2, R1 at 3.81 mm, J1 at 1.27) J1.2's ground stub turns down exactly on the column of J1.1's
portal (tip + 2.54 mm) and walls that pin in. Two symbols that each keep 1.27 mm leave 2.54 mm between a pin tip and the
next body; the router needs 4.445, so the margins of the two symbols have to sum to that, and the successful layouts have
the symbols that were named at 2.54 to 3.81 mm and the rest at 1.27. The loop finds this by failing; it could be told
(a preflight on trapped portals, below).

**A router that needs less room** [`auto-v12cr`: `KICAD_AI_ROUTER_EXPERIMENT=compact_router`]. Routes pass 1.27 mm from a
symbol instead of 1.905 and may turn 1.905 mm after a pin instead of 2.54, so a pin tip needs 3.175 mm in front of it. The
knob is read for every trial, the verified baseline included, which then fails to build on the MCU and the amplifier, so
that run has no shipped column; its selected sheets against `auto-v12`'s:

| fixture | wire mm: standard router | compact | area mm2: standard | compact | trials: standard | compact |
|---|---|---|---|---|---|---|
| mcu | 412.8 | 403.9 | 8001 | 7736 | 3 | 4 |
| noninverting_amplifier | 281.9 | 276.9 (MCP Pro's boxes) | 4961 | 7219 | 5 | 5 |
| rc_filter | 71.1 | 53.3 | 2635 | 1878 | 7 | 3 |
| linear_regulator | 111.8 | 111.8 | 2495 | 2495 | 3 | 3 |

The RC filter loses another 25% of its wire and 29% of its area in 3 trials instead of 7; its first compact trial built
and the netlist gate caught a short between two nets, and giving C1, J1, J2 and R1 2.54 mm fixed it. The MCU's crystal
nets route directly (OSC_IN excess 2.75 to 1.25). The amplifier never builds a tight sheet with it, even with J1 and U1 at
3.81 mm. So the router's geometry is a second dimension of the search with no uniform winner, and the netlist gate is
doing real work: a router that passes closer to pins produced a short once. It stays an experiment.

**More room does not fix the MCU's crystal loop.** OSC_IN is 35.6 mm of half-perimeter and 98 mm of wire in `auto-v12`:
X1's pin 1 faces away from U1, so the net goes round X1 and under both load capacitors. The standard router at a uniform
2.54 or 3.81 mm [`mu2.54`, `mu3.81`] does not build at all (a route touches another net at OSC_OUT). Nothing fails in the
sheet that is kept, so the loop never sees the detour, and pre-route half-perimeter cannot see it either: trying all 8
orientations of every symbol with at most 4 pins, positions fixed, saves 2.5 mm of 176.5 [`auto-v11`'s sheet].

**Two judges were wrong, and both were found by reading these histories.**

* The acceptance gate took symbol overlaps from MCP Pro's visual-QA finding. That finding builds a symbol's box from its
  graphics and from the point one pin length beyond each pin tip, so it calls a power symbol 7.6 mm from U1's body an
  overlap (U1's QA box is 63.5 to 83.8 by 128.3 to 153.7 mm; its body is 68.6 to 78.7 by 134.6 to 144.8), and it names
  power symbols by their random numbers. The shipped amplifier carries one such pair (`#PWR5556`, U1), each tight
  candidate carried another (`#PWR21696`, `#PWR36415`), and so the gate rejected the two amplifier candidates that were
  31% and 30% shorter as having a new overlap [`auto-v9`: the engine kept the shipped sheet]. The gate now measures
  overlaps from the library graphics (`symbol_overlap_pairs`), names a power symbol by its prefix, and compares counts.
* `kicad_geometry._transform` mirrored in library space before rotating. KiCad rotates and then mirrors in page space,
  and the two orders differ for a quarter turn: 4 of 12 orientations (rotation 90 and 270 with either mirror) put
  every pin on the wrong side. MCP Pro's transform agrees with KiCad's netlist export in all 12 (`transform_oracle`);
  the repo's now does too (`transform_xcheck`: no disagreeing orientation).

**What did not help.** Reserving a 3.81 mm corridor in front of every pin and keeping power and label stubs out of it,
so a stub could not wall in the pin beside it [`auto-v11`]: the RC filter needed 9 trials and ended on MCP Pro's boxes
at 81.3 mm (71.1 without), and the amplifier's wire grew by 2.5 mm. Where there is a way round the planner already
finds it; where there is none a corridor only makes it fail later. Removed.

**Cost.** One `auto` run is 135 to 410 s per fixture, against about a minute for the shipped baseline alone. A failed
trial costs 15 to 20 s on the small circuits and about 100 s on the MCU.

**What is left.**

* The loop finds each symbol's room by failing. The 4.445 mm rule above could be applied before routing (a preflight that
  lists every trapped portal and the symbol that blocks it), which could replace most of the failed trials.
* Router geometry as a second dimension of the search, tried after the standard router fails on the same margins.
* The MCU's crystal loop (above): a cost that knows where a pin faces, and a placement that can act on it.
* The decoupling bank is oriented supply-up, ground-down whatever the host pin's side, so on the MCU the host's VDD wires
  cross the ground rail on their way up.
* Label stubs: in `auto-v12cr` the MCU's UART labels hang 25 mm below U1 because the right side is crowded.
* Placement order (section 8) is still fragile; nothing in the loop touches it.

## 13. Reading like an engineer: what a reader notices, measured and then fixed

Section 12 left the engine shorter and smaller than what shipped, and that says little about whether an engineer
would read the sheets without a second look. A schematic can be short and still have text on its wires, a decoupling
capacitor 50 mm from its chip, a feedback resistor beyond the output connector, a connector whose pin 2 sits above its
pin 1. The project's rule from here on: a support part far from the part it serves is a failure whatever the wire or
area; a large sheet is fine only when there is a reason for it (text room, a clean flow), never because a support part
was left far away.

**What is measured** (`backend/schematic/readability.py`, `wire_nets.py`; every number below is from the saved sheets, by
the same code for every column):

* *text on wire*: a reference, value or power-symbol text whose box a wire crosses; *vertical labels*: net labels drawn
  at 90 or 270 degrees;
* *crossings*: wires of different conductors crossing in the interior of both (KiCad connects nothing there; a pin joins
  the *end* of a wire, a label joins anywhere along one);
* *wires through a body*: a wire that runs more than 1 mm across the inside of a symbol's drawn body (a pin tip may sit
  a little inside the box of its graphic, as an LED's does; the wire leaving it is not through anything);
* *jogs*: a wire stepping sideways by less than a pin spacing; *detours*: a net routed with at least 1.8 times and at
  least 15 mm more than its half-perimeter;
* *support parts*: every two-pin part that is a bypass (between two rails), a shunt (one pin on a rail) or in series,
  and its Manhattan distance from the nearest pin of a part with three or more pins (or a connector) that it is wired
  to; *far* is beyond 38.1 mm (bypass, shunt) or 50.8 mm (series), and what is beyond 15.24 mm is summed as *excess*;
* *power symbols entered sideways*: the wire arrives at the symbol's pin horizontally.

**A human reference** [`sandbox.py readability RUN --corpus`]. KiCad ships 111 demo sheets; 91 are small enough to
read, and they are drawn by people. Per sheet, median and 90th percentile:

| measure | median | p90 |
|---|---|---|
| reference/value text with a wire through it (per text) | 0 | 4% |
| net labels drawn vertically (per label) | 0 | 0 |
| reference/value text drawn vertically (per field) | 1% | 29% |
| wire crossings per 100 segments | 2.2 | 24 |
| tiny jogs per 100 segments | 0 | 1.0 |
| power symbols entered from the side (per symbol) | 10% | 67% |
| decoupling distance, mm (83 parts) | 30.5 | 64.8 |
| shunt distance, mm (289 parts) | 21.6 | 81.3 |
| series distance, mm (947 parts) | 21.6 | 71.1 |
| label stub length, mm (3115 labels) | 15.2 | 26.7 |

People do not turn labels, rarely put text on a wire, and bring a wire into a power symbol along its axis nine times
in ten. Their support parts are looser than this project's limits (a bypass capacitor a median 30 mm from its pin), so
the limits above are deliberately stricter than the corpus: the project holds its own sheets to them.

**Where `auto-v13` stood** (the engine of section 12, shipped column from the same run):

| fixture | text on wire: shipped / engine | vertical labels | crossings | farthest support part, mm | far parts |
|---|---|---|---|---|---|
| mcu | 14 / 12 | 0 / 2 | 5 / 6 | 57 / 36 | 1 / 0 |
| noninverting_amplifier | 6 / 4 | 0 / 0 | 7 / 0 | 50 / 29 | 1 / 0 |
| rc_filter | 5 / 4 | 0 / 0 | 0 / 1 | 29 / 29 | 0 / 0 |
| linear_regulator | 4 / 4 | 1 / 3 | 0 / 0 | 25 / 20 | 0 / 0 |

(The shipped MCU's C3 is 57 mm from the pin it serves and its amplifier's feedback resistor 47 mm; the engine had
already pulled them in to 36 and 20 as a side effect of compaction.) Reading the sheets found these causes, each fixed
in turn:

**1. Reference and value text lay on wires.** MCP Pro writes a reference above its symbol and a value below it, on the
symbol's axis, which for a part in a vertical chain is the wire. `field_placement.py` places every visible reference,
value and power-symbol text with one small CP-SAT problem (candidates beside, above and below the symbol and inside a
large body; costs for wires, symbols, pins, labels and other text; the deterministic solver, one worker, seed 7)
over text boxes from the calibrated model of section 11, now with justification, rotation and mirror (the
`field_justify` probe: 0 of 168 combinations off). Text on wires went from 12/4/4/4 to 0/0/0/0 [`auto-v14`], each
sheet's collisions before the planner 4 to 12 for wires and 2 to 10 for symbols, after it none. A second fixer then
undid part of it: MCP Pro's own `sch_fix_readability`, triggered by its own estimate of text overlaps, re-placed the
fields of five symbols, a connector's name back on its own pins and a resistor's value vertical on its wire
[`auto-v16`: "Applied: auto-placed fields on 5 symbol(s)"]. The workflow no longer runs it over text the planner placed.
The planner itself kept a vertical text at no cost whenever nothing collided; a vertical text now costs 25, which
only a horizontal place with a collision exceeds.

**2. Labels stood on end.** A label at the end of a vertical stub is drawn vertically and no human sheet does that. A stub
whose label would be vertical costs 14 mm more, so a stub that turns once to end horizontally wins (regulator 3 to 0,
MCU 2 to 2 [`auto-v14`]); a stub that has to turn goes away from the pin beside it, because planning pins one after
another gave no way to know that turning toward a neighbour cut across its way out; and labels are planned before
power symbols, since a label wants a straight run and a power symbol can hang from a stub that turns (the MCU's UART
labels stood on end because a ground symbol had taken their corridor).

**3. A shared rail cut across the supply wires.** A decoupling bank above a chip's supply pins had its ground rail between
the capacitors and the chip, and the supply wires went up through it: 2 of the MCU's 6 crossings. A rail is drawn only
when its wire and stubs meet no wire, symbol or pin already on the sheet; otherwise each pin gets a symbol of its own
(`rail_obstructions`) [crossings 6 to 4, `auto-v16`]. While writing the test the segment-intersection helper turned out
to test the perpendicular case against the wrong end of one segment and to report crossings that were not there; fixed.

**4. Floating power flags.** Every PWR_FLAG had a stub and a supply or ground symbol of its own, drawn apart from the
circuit: 3 in the amplifier. A flag now stands on a wire of the net it marks (`flag_attachment.py`, `flag_edit.py`,
tool `sch_attach_power_flags`): a grid point on a wire that touches the net's pins, standing up from a horizontal wire
or to the side of a vertical one, clear of everything else. The first version put the flag's pin on the *middle* of a
wire and every sheet failed ERC ("pin not connected"): KiCad joins a pin to the end of a wire only. The wire is cut where
the flag stands [`auto-v15`: all four fixtures fell back to the verified profile; `auto-v16`: all pass]. The measure of
how wires are joined had the same mistake and was corrected.

**5. A part with no relation was left wherever the floorplan put it.** The amplifier's feedback resistor R2 has no
declared relation, so it ended beyond the output connector with an 84 mm wire back to the inverting input. Declaring it
as a series element beside the op-amp's pins took the sheet from 282 to 109 mm of wire (experiment `exp-amp-series`).
`relation_inference.py` derives the same from the netlist for every two-pin part that has none (a part between two pins of
one larger part is a series element, a part from a rail to a pin of a larger part hangs on that pin with its far pin
toward the rail, bypass capacitors on supply pins are a bank in line with the pins or hang beside them; connectors are
never support parts) and the compiler drops any it cannot honour. Run with every declared relation removed (`*_bare`),
the engine places the circuits as well as with them:

| fixture | wire mm, declared relations | inferred only | crossings | farthest support part, mm |
|---|---|---|---|---|
| mcu | 373 | 329 | 4 / 2 | 35.6 / 33 |
| noninverting_amplifier | 86 | 86 | 1 / 1 | 15 / 15 |
| linear_regulator | 99 | 81 | 1 / 0 | 14 / 14 |
| rc_filter | 65 | 89 | 1 / 2 | 29 / 33 |

[`auto-v16`, `auto-v16-bare`]. The RC filter has no part with three pins, so nothing is anchored and the floorplan
decides, which is the 89 mm.

**6. A connector's pins were in the wrong order.** A connector that faces right was turned half a turn, which puts its pins
on the right and reverses them: pin 2 above pin 1, in three of the four sheets, and in the RC filter and the regulator it
made a wire cross a stub. People mirror it. `orientation.py` scores all twelve orientations of a connector (four
rotations, three mirror states; a reversed pair costs 8 mm), `sch_set_symbol_mirror` writes KiCad's `(mirror y)`, and the
server's geometry helpers read each placed symbol's mirror (MCP Pro's `get_pin_positions` accepts it, its parser drops
it; the engine's `parse_schematic_file` puts it back) so terminals, rails and routes keep to the pins as drawn
[crossings: RC filter 1 to 0, `auto-v17`]. The verified baseline keeps the half turn.

**7. Power symbols were entered from the side more often than not.** 10 of 16 power symbols in the MCU, 6 of 11 in the
amplifier, against 10% in the human sheets. A stub that arrives sideways costs 8 mm, so one that turns once to arrive
along the symbol's axis (down into a ground symbol, up into a supply symbol) wins when there is room, and a symbol
entered sideways costs 4 in the ranking. The price is a little wire: RC 54.6 to 57.1, regulator 66.0 to 71.1 mm, and
a crossing appeared in the amplifier where a VEE stub turned toward the feedback wire [`auto-v17`, `auto-v18`]. With a
ground symbol that arrival rule made the cheapest stub for a connector pin that points up the one that runs straight
back down through the connector itself [`auto-v19`, the regulator's J1]; a stub may not run through the part it leaves.

**8. A candidate that builds with crossings is tried again.** A stub is drawn before the wires that are routed after it,
and the router does not see it. When a candidate has wires crossing the loop switches on the router's boxes round earlier
wires and tries again; the better of the two is kept (the same mechanism that answers overlaps).

Two new circuits that declare no relations at all, an I2C sensor on a four-pin connector (`i2c_sensor`) and a transistor
switching an LED (`led_switch`), were added to see what the engine does with a netlist alone. They turned up the rest.

**9. The cosmetic loop undid the flag planner.** After everything is placed the workflow runs MCP Pro's score-and-fix
passes. One of them, `sch_normalize_power_orientation`, turns every power symbol and flag back to 0 degrees. A flag that
stands beside a vertical wire is turned 90 degrees on purpose, so the fixer put it back on its wire and left its
`PWR_FLAG` text upright across the connector beside it: the orientation fixer applied in the MCU of `auto-v16`, `v17` and
`v18`, in the bare MCU of `v18`, in the bare RC filter of `v16` and in `led_switch` of `v19`, always where a flag stood on
a vertical wire. It also made every candidate of such a sheet fail the clutter check (all five compact trials of the MCU
in `auto-v19` failed on the flags and J2), so three of the retries were spent adding margins round the flags and none
went to a real defect. A profile
now says which fixers it leaves alone (`_planned_cosmetic_categories`); the guarded profiles skip this one [`auto-v21`:
flags beside their wires, text horizontal, on every sheet].

**10. A part that reaches no larger part stayed far away: chains.** In `led_switch` the LED hung on the transistor's
collector but its resistor (+5V, resistor, LED, collector) reached nothing larger: it was left to the floorplan, 51 mm
from the pin it served and joined by a 40 mm loop. A part that reaches no larger part but shares a node with the far pin
of a part just hung now hangs on that pin, in the direction the chain already runs (`relation_inference.py`; the
compiler already accepted a placement whose anchor is another's subject). The LED then lay on its side, because a rail
points a hung part along it but nothing pointed a diode, and the placement tool paid more for turning a part a quarter
than for a far pin pointing the wrong way. A hung part with no rail is told which way its far pin goes
(`BranchRelation.extend`) and the engine profiles weigh that direction above a quarter turn
(`ENGINE_EXTENSION_WEIGHT`, a new argument of `sch_place_pin_relation`; the verified baseline keeps the weight it had)
[`auto-v22`: the resistor and the LED stand in one column above the collector, farthest support part 51 to 22 mm].

**11. The gate refused the better candidate.** `compare_quality` let a candidate grow 1.5 times in either dimension over
its baseline, and 5% in area and 2% in wire. For the regulator the shipped sheet is a strip 190 mm wide and 42 mm tall;
the same circuit in a 62 by 67 mm block has half the area and half the wire, no crossing and no text on a wire, and was
refused for being 60% taller [`auto-v19`, `auto-v21`]. A drawing may now be reshaped but not stretched (neither side may
pass the baseline's longest by half), and a candidate that reads strictly better with nothing reading worse may exceed
the baseline's area up to 1.6 times and its wire up to 1.15 times: a sheet is larger for a reason when it has room for
its text and a clean flow. Replaying the recorded trials of `auto-v21` under the new gate changes one selection, the
regulator's, and no other.

**12. A label stood on the wire of another.** A net with no library power symbol (`VIN`) gets its labels one pin to a
call, and each call began with an empty list of what earlier calls had placed, knowing earlier labels only as soft
text a stub may cross for 8 mm: cheaper than the 14 mm a vertical label costs. C1's stub ran through U1's `VIN` label
[`auto-v21` regulator: text on a wire 1]. Labels already on the sheet are fixed obstacles (a symbol's fields are moved
afterwards, a label's text is not), and an end symbol laid over a wire already drawn costs 16 mm [`auto-v22`: 0].

**13. A wire was drawn through its own connector.** `led_switch`'s CTRL wire left pin 3 of the connector on the far
side of the body and ran straight through it to R1. The router had found a proper route round the connector (5 bends); the
shortcut pass then replaced it with a straight line. The first and last segment of a route touch their own symbol, so the
shortcut check lets them off the body test, but nothing required them to *leave the pin outward*. In the net-aware router
they now must (`leaves_pin_outward`). No measure counted it, because the pin tips are outside the body's box; a wire
through a body is now one (20 mm in the ranking, and a gate against the baseline). The shipped profile had one in the same
sheet [`auto-v22`: 0]. The route a shortcut had drawn through a body was shorter: with the rule the RC filter is 62 mm
where it was 58 (a candidate whose route ran along R1's own body from the far end to its pin tip was refused and the
retry found another), and the bare RC filter 114 mm where it was 102 with a wire through J1.

**14. A vertical label has its own retry.** A candidate with labels on end is tried again with more room for the symbols
whose pins carry them, found from the conductor each label stands on (`rotated_label_owners`); the first defect with a new
placement to try decides the next attempt, so a crossing the router already keeps clear of its stubs for does not hide a
label. It is covered by unit tests and has not yet changed a real result: in the MCU the retries go first to the
connector J3's terminal failures (no exit for its middle pin at margins of 1.27 and 2.54 mm, which passes at 3.81, the same
in `auto-v19`, `v21` and `v22`) and then to router boxes round the stubs, whose libavoid diagonals use up the six. The MCU's
vertical labels went from 4 [`auto-v19`] to 1 [`auto-v21`, `v22`] with that raised margin for J3, not with this retry.

**What the ranking weighs now** (`quality_selection.RANK_*`; millimetres of wire): 0.006 per mm2 of area (was 0.015), 2.54
per corner, 20 per symbol overlap, 8 per text overlap, 12 per text on a wire, 8 per vertical label, 8 per crossing, 20 per
wire through a body, 3 per jog, 4 per power symbol entered sideways, 0.5 per mm of detour, 2.5 per mm of every support
part's distance beyond 15.24 mm and 150 per support part beyond its limit. A candidate is also refused when it has more far
support parts, more text on wires, more than 4 more crossings or any more wires through bodies than the baseline. Wire,
corners and area say how compact a drawing is; these say how it reads. Area counts for little because a large sheet is
fine for a reason, and the reasons that are not (a part left far away, a detour) are counted where they happen.

**Results** [`auto-v22`, ten sheets in one run, every one passing ERC and the netlist check; shipped is the verified trial
of the same run, earlier is `auto-v13`, the engine of section 12]:

| fixture | wire mm | text on wire | vertical labels | crossings | wires through a body | farthest support part, mm | far support parts |
|---|---|---|---|---|---|---|---|
| rc_filter | 117 / 71 / 62 | 5 / 4 / 0 | 0 / 0 / 0 | 0 / 1 / 0 | 0 / 0 / 0 | 29 / 29 / 22 | 0 / 0 / 0 |
| linear_regulator | 130 / 86 / 69 | 4 / 4 / 0 | 1 / 3 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 25 / 20 / 15 | 0 / 0 / 0 |
| noninverting_amplifier | 404 / 282 / 107 | 6 / 4 / 0 | 0 / 0 / 0 | 7 / 0 / 1 | 0 / 1 / 0 | 50 / 29 / 19 | 1 / 0 / 0 |
| i2c_sensor | - / - / 151 | - / - / 0 | - / - / 0 | - / - / 3 | - / - / 0 | - / - / 14 | - / - / 0 |
| led_switch | 271 / - / 116 | 8 / - / 0 | 0 / - / 0 | 1 / - / 0 | 1 / - / 0 | 42 / - / 22 | 2 / - / 0 |
| mcu | 505 / 413 / 339 | 14 / 13 / 0 | 0 / 2 / 1 | 5 / 6 / 2 | 0 / 1 / 0 | 57 / 36 / 36 | 1 / 0 / 0 |
| mcu_bare | - / - / 330 | - / - / 0 | - / - / 1 | - / - / 4 | - / - / 0 | - / - / 33 | - / - / 0 |
| noninverting_amplifier_bare | 400 / - / 110 | 9 / - / 0 | 0 / - / 0 | 6 / - / 1 | 0 / - / 0 | 37 / - / 16 | 0 / - / 0 |
| linear_regulator_bare | 124 / - / 69 | 5 / - / 0 | 1 / - / 0 | 0 / - / 0 | 0 / - / 0 | 48 / - / 15 | 1 / - / 0 |
| rc_filter_bare | - / - / 114 | - / - / 0 | - / - / 0 | - / - / 1 | - / - / 0 | - / - / 33 | - / - / 0 |

(each cell: shipped / earlier / final; `-` where there is none: the fixture is newer than the earlier run, or the shipped profile fails)

The shipped profile fails ERC on `i2c_sensor` (two power flags end up connected), so that sheet has no shipped column; the
engine's flags ride on wires and it passes. Wire mm is the sum of the drawn wire of the selected candidate. The rows
ending `_bare` are the same circuits with every relation removed.

**Not kept.**

* Choosing a series element's rotation by whether its pins face the anchors (a crystal beside two adjacent pins turned
  upright): on the MCU the wire grew by 3.8 mm and the OSC_IN detour by 12.7 mm, so it was reverted. The crystal's loop is
  not an orientation problem: the router must leave a pin along its normal, a crystal beside two adjacent pins has to be
  wrapped by one of its two wires whichever way it lies, and the BOOT0 resistor and the load capacitors sit in the way.
* A cost for a power stub that turns across the way out of a pin whose net will be wired (`i2c_sensor`'s ground stub
  across the SDA and SCL wires): the sheet came out identical, and the amplifier's did too. Removed.
* Net labels in place of wires that cross. With SDA and SCL as labels the engine's tight candidate still had two
  crossings (between power stubs), and the candidate that had none was the one the area gate refused (11); the gate was
  the defect. Nets declared as wires (the MCU's crystal) are not the engine's to relabel, so no retry was built on it.

**What is left.**

* The MCU's crystal loop (OSC_IN 98 mm of wire for a 35.6 mm net) and the crossings round it. More room does not fix it,
  and the retry budget of six is spent on router failures (libavoid emitting a diagonal with boxes round the stubs) before
  a vertical label gets its turn.
* `i2c_sensor`'s three crossings. Macro compaction places the connector under the chip assuming its pins as the library
  draws them (a column facing left); the orientation stage then turns it to face the chip, and with its pins up and in
  order the two bus wires reach the chip's pins in the opposite order and cross. Orientation cannot undo that: reading
  order costs 8 mm a pair (48 for a four-pin header), the distance term prefers pins up by 34 mm, and a crossing would
  have to cost more than either. It needs the compaction to know the orientation, or an orientation stage that may move
  the connector.
* A connector with a label or terminal on every pin has no exit for its middle pin at the default margin: J3 fails twice
  before it passes at 3.81 mm in all three runs of the MCU, two trials spent every time. Starting such a connector at 3.81 mm
  would save them; it is not done because it is a guess from one circuit.
* A stub is still drawn before the wires, so a wire can still cross it (the amplifier's VEE stub across the
  inverting-input wire; a connector whose supply and ground pins sit side by side has to cross one of its neighbours).
* Alignment: parts of a bank or a column are placed by relation, not on a common grid line; jogs are rare (0 or 1 per
  sheet) but nothing makes a row a row.
* Flow and idiom conventions beyond what the relations and ELK give: supply up and ground down hold, nothing more.
* The relation inference knows four shapes (series between two pins of one part, a shunt or pull to a rail, a bypass, a
  chain) and leaves a part between two different larger parts, or between nets that reach none, to the floorplan: the
  bare RC filter, with no part of three pins to anchor anything, is the 89 mm.

## 14. Composition, orientation, and circuits the engine was not built on

Section 13 left the sheets reading well on the local counts (text on wires, far support parts, crossings) and short of
what an engineer draws on composition: straight wires, parts sharing a row, a chain that runs in one direction. The next
two steps were a placement that chooses position and orientation from the wires that will be drawn, and a test on circuits
the engine had never seen: four of KiCad's own demo circuits, rebuilt from their netlists and compared with the human
drawing by the same counts.

### 14.1 Placement refinement (`placement_refinement.py`)

The symbols no relation places (connectors, series parts) were left where MCP Pro's auto-layout dropped them, turned by a
step that did not know where they stood. `placement_refinement.py` chooses their position and orientation together:

* every wire is estimated as the route the router will draw, leaving each pin along its direction (straight, L, Z, U; a
  net of three or more pins is one run with T joins, as the router draws it); bends (5 mm each), crossings (15), a route
  through a symbol (30) and a route over a pin of another net (60) cost;
* the room in front of every pin is kept clear of other symbols' bodies and of other nets' wires, as wide as what is
  drawn there (a power symbol as wide as the name it shows), and a power symbol is entered along its own axis (a pin that
  points across costs 8 mm, one that points right away 24: the terminal planner's own price and a stub that ends up
  crossing what is beside it);
* a connector's pins read in order; coordinate descent over a lattice of positions and every orientation the symbol may
  take, then an exhaustive search over the few symbols that share a net; the result is never worse than the start, the
  search stops after a count of costed poses (not a clock), and `details` in `placement-refinement.json` names what each
  term found (which symbol, which pin, which net) before and after.

The estimator was calibrated against what the router drew in `auto-v22` (corners per net, estimate against router: 3/3,
6/7, 5/5, 2/2 on four sheets), and four defects the diagnostics found were fixed on the way: a wire estimated straight up
the column of a chip's pin tips (a short no router draws: the sideways meeting is priced at 7.62 mm and a foreign pin tip
on the line costs a short), a connector's ground pin facing a wire six millimetres in front of it (the ground symbol had to
be drawn beyond the wire, its stub across it), a wire standing in the room of a pin, and the relation-placed support parts
being fixed even when they were what blocked a pin (they may now be nudged within 6.35 mm, not turned). A failure that
names a symbol the refinement moved is answered without the refinement first (`LayoutPolicy.refine`): the libavoid
diagonal on `i2c_sensor` came from the connector standing exactly under the chip's first pin, and loosening the room round
it did not help.

### 14.2 What the demos asked

Four demo circuits whose symbols are all in KiCad's standard libraries (the others draw with symbols of their own project
and cannot be rebuilt by name): an LM317 supply (AC source, transformer, bridge, reservoir, regulator with its adjust
network, load), a buck converter, a three-transistor amplifier, an op-amp stage beside a one-transistor stage.
`sbx/demos.py` takes parts, values and nets from `kicad-cli sch export netlist` and nothing from the sheet's geometry; the
functional blocks and the flow between them are written by hand, as every fixture has them, no relation is declared, and
a net a power input is on that nothing drives gets a power flag. Measured on the human originals by the same counts (text
and annotations excluded):

| demo | parts | wire mm | area mm2 | corners per connection | crossings |
|---|---|---|---|---|---|
| `demo_lm317` | 14 | 376 | 5,791 | 0.50 | 1 |
| `demo_buck` | 8 | 138 | 3,997 | 0.29 | 0 |
| `demo_ampac` | 23 | 495 | 13,378 | 0.50 | 1 |
| `demo_opamp` | 14 | 210 | 6,087 | 0.27 | 0 |

**The first run failed on every one of them.** Four different things, none of them in the circuit:

* KiCad accepts a reference such as `Rmeas1`; the schematic library the engine writes with refuses it
  (`[A-Z]+[0-9]*[A-Z]?`): the converter renames it.
* `sch_place_node_cluster` outlasted the client's 30 s timeout (60 s on a transistor with three parts round its base):
  MCP Pro parses a whole symbol library (a megabyte) for every pin or bound it is asked for, and a cluster asks hundreds
  of times. `backend/kicad/library_cache.py` keeps each library file and the answers derived from it: 13.5 s -> 3.2 s.
* A stage that stopped on its own check (a shared node whose members moved apart because one was itself an anchor, a tool
  that timed out) ended the whole build although the verified baseline had built; now the trial is out and the baseline
  stands. A relation that cannot be solved (no free place, an infeasible group) is dropped and its parts are placed as free
  symbols; a power flag with no free place beside its pin goes beside it anyway.
* The verified profile (what ships) shorts two nets on `demo_opamp` (the transistor's collector and emitter wires touch),
  so nothing ships for it; for the LM317 supply it draws 748 mm of wire in 38,100 mm2 and for the three-transistor amplifier
  1,441 mm in 96,790 mm2 (the human originals: 376 mm in 5,791 and 495 mm in 13,378).

**Two reasons the sheets were huge, both about where things stood when the floorplan was made.** MCP Pro scatters every
symbol over the sheet; a relation puts its parts beside an anchor, a part no relation places is left where it fell, and a
block is measured by where its members stand, so seven parts of the LM317 supply made a block 222 x 136 mm. And a relation
was told to keep clear of every placed symbol on the sheet, scattered ones of other blocks included, so it left its parts
only a free place far away. Now the free parts are brought in beside their partners in their own block before the blocks
are measured (`seed_placement`), and a relation avoids what is placed in its own block only (a block moves as one piece).

### 14.3 Two placements, and the better one kept

A circuit can be placed from the relations its nets imply (a part hung on the pin it serves: right for a chip with its
bypass capacitors) or from the netlist alone. For the second, each block is laid out by ELK from how its nets join its
parts, in the order the design lists them (the existing local layout joined every part to the block's first, which is a
star), the free parts are brought in and turned by the refinement, and blocks are slid as pieces to where their
connections run straightest, never to the far side of a block they feed (`align_groups`). On the demos the netlist
placement built on the first try for all four where the relation placement failed the first trial on two (the LM317
supply failed every one of nine trials), and the engine now starts with both: its trials are verified, relations,
netlist, MCP Pro's boxes, each line of attempts with its own share of the retries (a circuit whose nets imply no relation
skips the duplicate). A candidate much smaller than its baseline that reads better may use more wire than the gate gave
it before (`WIRE_ALLOWED_PER_AREA_SAVED`): in an earlier run (`auto-demo3`) the buck converter's candidate was 45% smaller
and had 28% more wire than a shipped sheet with eight texts on wires and four parts far from what they serve, which the
earlier gate would have refused.

### 14.4 What the pictures showed that the counts did not

With the four demos building, the sheets were looked at one by one, and three defects were there that no count had named:

* **A flag 76 mm from its net.** A power flag on an ordinary net (the LM317's rectified input, which no pin drives) was
  placed beside a pin of its net only when the net was a power or ground net; on any other net it stayed where MCP Pro's
  auto-layout had dropped it, and the wire to it made the sheet 129 mm tall where the circuit is 55 mm. It is a support
  part far from what it serves, by the rule that a far support part is a fail, and a count that looks at capacitors and
  resistors did not see it. The flag is now placed beside a pin of its net in the guarded profiles like the others.
* **The text of a sheet that did not fit its page.** The auto-layout leaves the anchors where it likes, a compact drawing
  75 mm wide stood at x = 250 on a 297 mm page, `sch_visual_qa` reported a symbol off the sheet, and `sch_fix_readability`
  grew the paper and also placed every reference and value again by its own rule: the buck converter's inductor had its name
  and its value written along the part and one over the other (and the voltage source the same). The engine's own count
  of text overlaps said none, because it leaves a symbol's own texts alone. When the engine has placed the fields, an
  overflowing sheet now only grows the paper (`sch_set_sheet_size`, one size at a time) and nothing else is touched. The
  sandbox's `redraw` counts a symbol's own stacked texts too. (Bringing the drawing to the corner of the page instead, so
  that it needs no larger paper, was built, measured and dropped: see 14.5.)
* **A tool the engine had not been allowed.** The fix above calls `sch_set_sheet_size`, which the guarded client refuses
  unless it is listed, and nothing would have said so until a sheet overflowed. `tests/unit/test_engine_tool_allowlist.py`
  now scans the engine for the tools it calls by name and fails on one the client would refuse.

### 14.5 The numbers were not steady, and why

The step above that brought the finished drawing to the corner of the page changed the results, and the way it changed
them was the finding. Two runs of the same engine, the layouts the same to a hundredth of a millimetre and one of them
standing 178 mm further along the page (`auto-v25` and `auto-v26`):

| fixture | with the move | without |
|---|---|---|
| `i2c_sensor` wire | 121.9 mm | 134.6 |
| `rc_filter` corners | 1 | 3 |
| `noninverting_amplifier` jogs | 0 | 2 |
| `led_switch` wire | 50.8 mm | 47.0 |
| `linear_regulator` | same | same |

and the opamp of the demos, 215 mm of wire and 288 mm: the first trial of the relation placement failed to route (a wire
through a stub) and the retries did not get it back. The without-move runs gave the numbers of the run before them (`auto-v24`)
to the last digit, so the gains the move seemed to bring (`rc_filter` from three corners to one) were luck, and so was any
other difference of that size between two runs of this section before the cause was found.

The pre-route scenes of the two opamp runs were the same to 0.01 mm and three of eleven power terminals stood elsewhere.
Three places decided a tie or a boundary case by float noise, which depends on the absolute coordinates:

* the terminal planner: a stub 3.81 mm from a symbol is on the boundary of a 3.81 mm clearance and which side float noise puts
  it on depends on where on the sheet the pair stands; two paths of one length were told apart the same way;
* the net topology planner: the spanning tree was sorted on unrounded distances (two pairs of pins one distance apart were
  ordered by noise, not by name, so one net was cut into other branches), and a bend was counted for two coordinates that
  differed by 1e-14;
* libavoid: routing 66 recorded problems each moved three times by whole grids, 31 of 120 came out differently (a nudged
  segment 1.5 mm elsewhere). Posed from a grid corner (`router_origin`), none did.

Every boundary comparison in the two planners now carries a slack of 1e-6 mm, distances and scores are rounded, and the
router is handed the problem from a grid corner, the origin put back after the snap. Property tests move random scenarios to
six places on the sheet: the terminal planner chose differently in 21 of 400 power-terminal scenarios (and in the label and
rail variants), the topology planner failed its test on the first run, and all pass now. End to end the opamp, moved by
178 mm and by 51 mm, comes out with the same wires (0 differing segments of 44, the same 212.1 mm).

These planners and the router also run in the verified profile, so what ships changed wherever a tie was decided by noise:
of the 14 circuits of this section the shipped sheet is different on three (`rc_filter` 116.8 -> 121.9 mm of wire and one more
corner, the LM317 supply 763 -> 773 mm, the three-transistor amplifier 1,447 -> 1,449 mm with one more crossing) and identical
on the other eleven. The change is neutral on average and the shipped path is not bit-for-bit what it was.

The numbers of 14.6 are from `auto-v27` and `auto-demo10`, runs of this engine; it gives the same sheet wherever the
layout stands, so a rerun reproduces them. Anything with a tolerance in it that is changed later may change them.

### 14.6 Results

The fixtures, the engine of section 13 (`auto-v22`) against this one (`auto-v27`, selected sheet; wire mm, extent mm,
corners, crossings; every sheet passes ERC and the netlist check):

| fixture | wire | extent | corners | crossings |
|---|---|---|---|---|
| `rc_filter` | 61.0 -> 39.4 | 32x37 -> 19x34 | 4 -> 3 | 0 -> 0 |
| `rc_filter_bare` | 113.0 -> 33.0 | 33x24 -> 15x36 | 7 -> 3 | 1 -> 1 |
| `led_switch` | 115.6 -> 47.0 | 57x52 -> 19x53 | 6 -> 1 | 0 -> 0 |
| `linear_regulator` (and `_bare`) | 68.6 -> 68.6 | 53x61 -> 66x34 | 4 -> 4 | 0 -> 0 |
| `noninverting_amplifier` | 105.4 -> 90.2 | 53x44 -> 37x47 | 8 -> 10 | 1 -> 1 |
| `noninverting_amplifier_bare` | 110.5 -> 90.2 | 43x44 -> 37x47 | 10 -> 10 | 1 -> 1 |
| `i2c_sensor` | 149.9 -> 134.6 | 50x52 -> 50x50 | 4 -> 7 | 3 -> 3 |
| `mcu`, `mcu_bare` | 337.8, 328.9 (unchanged) | 94x71, 91x70 | 17, 15 | 3, 4 |

Corners per connection (human median 0.36, p90 0.67): `rc_filter` 1.00 -> 0.67 (and its wires are straight where they were
all bent: bent-wire rate 1.00 -> 0.00), `rc_filter_bare` 2.00 -> 0.67, `led_switch` 1.00 -> 0.40, the MCU unchanged at 1.29.
Worse than before, and said plainly: `noninverting_amplifier` 1.20 -> 1.60 corners per connection and three jogs where it had
none (it is 15% less wire in a smaller drawing, with more turns in it), `i2c_sensor` 0.50 -> 1.00 with two jogs and still three
crossings (two pull-ups on adjacent pins cannot both hang from the rail above, and the connector's ground stub crosses its signal
wires), and the share of power symbols entered from the side on `rc_filter` (0.00 -> 0.25) and the regulator (0.10 -> 0.20): the
supply arrow stands on the end of a horizontal wire. Text on wires is zero on every sheet; far support parts are zero on every
sheet but `led_switch` (one of three, as before); the only vertical labels are the MCU's (0.12 per label, as before).

The four demos, the human original beside what ships and what the engine selects now (`auto-demo10`; the same counts
for all three sheets, text and annotations left out; `sandbox.py redraw`):

| demo | sheet | wire mm | area mm2 | corners per connection | crossings | far support parts | power symbols from the side |
|---|---|---|---|---|---|---|---|
| `demo_lm317` | human | 376 | 5,791 | 0.50 | 1 | 1 | 0 |
| | shipped | 748 | 38,100 | 2.13 | 3 | 9 | 0.50 |
| | engine | 257 | 6,718 | 0.60 | 0 | 0 | 0 |
| `demo_buck` | human | 138 | 3,997 | 0.29 | 0 | 2 | 0 |
| | shipped | 170 | 9,523 | 1.00 | 0 | 4 | 0.14 |
| | engine | 112 | 3,290 | 0.71 | 0 | 2 | 0.17 |
| `demo_ampac` | human | 495 | 13,378 | 0.50 | 1 | 2 | 0 |
| | shipped | 1,441 | 96,790 | 1.88 | 8 | 17 | 0.17 |
| | engine | 274 | 6,089 | 0.71 | 2 | 1 | 0.13 |
| `demo_opamp` | human | 210 | 6,087 | 0.27 | 0 | 2 | 0 |
| | shipped | fails: it shorts two nets | | | | | |
| | engine | 212 | 3,674 | 0.58 | 1 | 1 | 0.47 |

Against what ships, the supply and the amplifier come out with a third to a fifth of the wire in a sixth to a sixteenth of
the area, and the buck converter with two thirds of the wire in a third of the area; the shipped sheets have 8 to 36 texts on
wires, the engine's none, and the engine's far support parts are no more than the human's. Against the human originals the
engine draws less wire on three of the four (by 19% to 45%) and as much on the op-amp stage, and an area below theirs on three
(the supply's is 16% above); that is not a better drawing, those sheets leave room for names and rows. It is behind on
composition: more corners per connection on all four (0.58 to 0.71 against 0.27 to 0.50), crossings of 2 against 1 on the
three-transistor amplifier and 1 against 0 on the op-amp stage (none on the supply, where the human's has one, and none on the
buck converter), and on the op-amp sheet almost half the power symbols are entered from the side (the human's none). In the
pictures (`sandbox.py redraw RUN --blind DIR`: the three drawings of a demo, shuffled, the key kept apart) the four human
sheets run in rows with their rails shared and their nets named; the engine's are compact clusters with a long wire between
blocks, readable, and not drawn the way a person draws them.

### 14.7 What is left

* Composition is still visibly short of a human's: blocks sit in a staircase where a person draws a row, jogs appear where two
  pins are one grid step off a line, power symbols are entered from the side, names are not written on nets, power flags
  clutter a sheet a person would not flag, and texts crowd (a value beside a flag symbol, two supply names touching).
* The verified profile (what ships) still shorts two nets on `demo_opamp`; it is the profile every guarded trial is
  compared with, so it matters only when no guarded trial builds.
* A relation-free netlist places well when its blocks are declared; a netlist with no blocks has nothing to place by.
* The estimator of the placement does not know where the terminal planner turns a stub (a ground pin pointing up gets a
  short sideways stub across whatever is beside it), nor the shared rails the router draws between power pins.
* Position independence is established for the terminal planner, the topology planner and the router's frame, and end to
  end on the opamp; the other stages were not checked one by one. A tolerance that is changed later may change every number
  of this section.
* The relation line of attempts still spends its retries on libavoid failures on some circuits (the LM317 supply failed four
  of its five relation trials); the netlist line, which has its own retries, is what builds it.

## How far to trust the replay lab

`sandbox.py replay` re-routes recorded libavoid calls under variants and applies a copy of the engine's snap,
endpoint overwrite, `simplify_route` and body and conductor checks. It omits the text-reservation, foreign-pin and
same-batch other-net checks, so it can be more permissive than the engine and should never be stricter. Its `asis`
control reproduces the recorded raw routes exactly.

`replay RUN --calibrate` scores the verdicts against the routes the engine recorded. Over three runs (`s-compact`,
`n-compact`, `ng3-compact`; 56 edges) the lab never rejected a route the engine accepted and accepted all 55 that the
engine accepted. Final points matched on 37 of those 55; the rest differ because its simplification safety check
lacks the text and foreign-pin tests. The one disagreement is the engine rejecting FB:net_branch_0 on the same-batch
check the lab does not replay. Absolute accept counts are therefore a little optimistic; comparisons between variants
are the useful part.

## Overlay checks

`apply_overlays` rejects an overlay that compiles but would fail when the code runs, as two of mine did on the newer
engine code: a function using `os` before its own local `import os` (UnboundLocalError) and a helper using a name the
engine imports only inside another function (NameError). Both checks compare against the unpatched file, so engine
code that already has the pattern is not flagged.

## Built but not yet measured

* `e7_allow_crossings` on its own on the per-net profiles (`x-compact`, `x-roomy`), and the final combinations on
  `roomy` placement (`n-roomy`, `ng-roomy`): the per-net scheduler and these profiles were retired in favour of one
  grouped call (section 6), so these are only of historical interest.
* A repair step: when the ownership check rejects a planned junction, try another candidate. The planner now keeps
  clear of pins and keep-outs, which was enough for the amplifier, but a layout where another net's wire runs into
  the planned junction still fails the whole build instead of re-planning. Not built.
* A preflight for trapped portals, router geometry as a second dimension of the layout search, an orientation cost that
  knows where a pin faces, and the decoupling bank's orientation: see the end of section 12.
