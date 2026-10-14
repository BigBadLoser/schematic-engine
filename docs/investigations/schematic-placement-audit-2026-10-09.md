# Schematic placement audit — 2026-10-09

This is a **measured baseline**, not a claim that the new placement candidates improve
the schematic. Evidence: the `combined` artifact from
[KiCad Visual Regression run 37915262202](https://github.com/BigBadLoser/kicad-ai-engineering-companion/actions/runs/37915262202)
(`schematic-recovery-router-combined`, artifact `11608893550`).
All four circuits selected the conservatively verified placement.

## Observed trial outcomes

| Fixture | Verified wire (mm) | Compact candidate | Roomy candidate | Selected |
|---|---:|---|---|---|
| MCU | 505.46 | Rejected: routing intersects existing conductor with unknown net owner | Rejected: wire crosses symbol body | Verified |
| RC filter | 116.84 | 101.6 mm, rejected: drawing area regression | 106.7 mm, rejected: drawing area regression | Verified |
| Non-inverting amplifier | 406.40 | Rejected: unknown existing-conductor net owner | Rejected: unknown existing-conductor net owner | Verified |
| Linear regulator | 129.54 | 198.1 mm, rejected: longer wires, drawing height and new symbol overlaps | 160.0 mm, rejected: longer wires and drawing height | Verified |

These are **post-route** outcomes. The RC filter is an especially useful counterexample:
shorter wiring does not automatically imply a better schematic.

### MCU block-envelope distortion

The measured post-ELK `power_entry` *full* block envelope was **97.79 mm**
wide, while the semantic `J2` connector-only footprint was **20.32 mm**.
The block includes two `PWR_FLAG` authoring auxiliaries, which are far apart
in the rendered drawing. Including those faraway auxiliaries in floorplan
envelopes distorts the apparent area and reports one block-envelope overlap
with `mcu_cluster`. Such envelope overlap is **not itself proof of a symbol
collision**.

This is a placement-model issue worth solving before treating ever larger
bounding-box clearances as the answer.

## Implementation begun

- The existing `verified → compact → alternate → roomy` transactional search
  has been retained. There is no parallel placement engine.
- For unconstrained block arrangements, `vertical_flip` provides a
  genuinely different seed while preserving each block's footprint.
- For explicit block-order hints, `constraint_aware` allows grid compaction
  while penalizing and ultimately rejecting violations of *required*
  left/right/above/below relationships.
- Every trial still runs the actual KiCad routing and electrical/quality gates.
- `placement-audit.json` categorizes observed failures without treating
  an unknown-net collision as proof of a placement error.

## Remaining blockers and next work

1. Compare new placement trials using their **real rendered PNGs** and exported
   quality metrics. Do not infer improvement from pre-routing HPWL alone.
2. Model authoring auxiliaries separately from semantic functional envelopes,
   while preserving their own real glyph/wire collision reservations.
3. Attribute existing wire segments to electrical nets before attempting
   routing-aware rip-up, selective symbol movement, or per-net congestion
   penalties. Without attribution, one cannot safely tell whether crossing
   an existing conductor would short distinct nets.
4. Once attribution is sound, implement a bounded placement feedback pass:
   rank high-detour nets, propose group/symbol moves obeying explicit
   constraints, reroute affected nets, run full KiCad verification, and
   accept only netlist-equivalent non-regressive candidates.

No net, component reference, or circuit-class heuristics should enter the
generic solver's placement constraints or scoring.
