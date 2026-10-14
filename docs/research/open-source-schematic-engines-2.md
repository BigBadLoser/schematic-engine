# ELK-based schematic renderers: what they do, and what our whole-circuit ELK prototype did wrong

Date 2026-10-12. Sources read: netlistsvg lib/{FlatModule,Cell,Skin,elkGraph,drawModule}.ts (master, raw.githubusercontent), d3-hwschematic README + src/d3-hwschematic.js, ELK reference option pages (eclipse.dev/elk/reference). Items marked (unverified) come from search snippets or inference. Several ELK doc pages (portConstraints value meanings, hyperedge page) 404'd or only list value names.

## 1. netlistsvg (nturley)

Graph construction (elkGraph.ts, Cell.ts, FlatModule.ts):
- A net is ONE internal Wire object with drivers / riders / laterals. It is NOT passed to ELK as a native hyperedge. elkGraph.ts converts it to ordinary edges:
  - 1 driver to N riders: N edges via route(drivers, riders), extended-edge form (sources:[x], targets:[y]).
  - 2+ drivers and no riders, or 2+ riders and no drivers: a zero-size dummy node `$d_N` (width 0, height 0, one 0x0 port, portConstraints FIXED_SIDE) with one edge per pin.
  - laterals (bidirectional pins): driver to lateral and lateral to rider, or first lateral to the others.
  - After layout, removeDummyEdges (drawModule.ts) snaps each dummy's incident edges to the nearest bend and drops junction points that are no longer real. The dummy is the junction.
- Edge options: org.eclipse.elk.layered.priority.direction = 10 on every edge not sourced from a $dff (pushes edges to point in flow direction); org.eclipse.elk.edge.thickness 1 or 2; bus-width labels with edgeLabels.inline.
- Nodes: org.eclipse.elk.portConstraints = FIXED_POS by default (Cell.ts buildElkChild). Ports sit at the x,y of the SVG symbol template (0x0 size). Skin attributes starting with org.eclipse.elk override that; org.eclipse.elk.x/y become fixed node position. Generic/split/join cells build ports from templates with prefixes in / out.
- Port sides come from the skin symbol (Skin.ts classifies by s:position top/bottom/left/right and s:dir), not from ELK: inputs on the flow-in side, outputs on the flow-out side, laterals on the cross sides.
- Constants: no shared constant net. Each constant-driven pin gets its own `$_constant_` cell (Cell.fromConstantInfo; one output port, label = hex). A constant is a tiny per-use leaf node that ELK places next to its consumer. (Yosys has no power symbols; this is the analogue of our supply/ground.)
- Graph-level options: only the skin's s:layoutEngine block, passed through. README example (search snippet, unverified): layered.spacing.nodeNodeBetweenLayers=5, spacing.nodeNode=35, a direction. It sets NO layering.strategy, nodePlacement.strategy, thoroughness, mergeEdges, hierarchyHandling, edgeRouting, partitioning or layerConstraint (ELK defaults apply: NETWORK_SIMPLEX layering, ORTHOGONAL). Flat graph, no hierarchy.
- Post-processing: dummy removal and junction cleanup only; wires are drawn from ELK sections (startPoint, bendPoints, endPoint, junctionPoints).

Take-away: it works because every pin is on a flow side, every net is split into driver-to-rider edges with priority.direction, constants are per-use leaf nodes, and multi-pin nets use a 0x0 junction dummy rather than a star from one pin.

## 2. d3-hwschematic (Nic30)

- Format: children + edges per node (hierarchy kept; hwtGraph notes hierarchyHandling must be set on at least two successive levels). Ports carry properties.side (EAST etc.) and index (clockwise from top-left; needed only for FIXED_ORDER).
- Node properties: org.eclipse.elk.portConstraints = FIXED_ORDER (or FREE), org.eclipse.elk.layered.mergeEdges = 1.
- Nets: simple edge (source/sourcePort/target/targetPort) or hyper edge with sources / targets lists of [nodeId, portId]. So native ELK hyperedges are used. ELK docs say none of its algorithms "explicitly support hyperedges"; a forum post shows several edges on one port get interpreted as one hyperedge.
- Code (src/d3-hwschematic.js): edgeRouting ORTHOGONAL; layered.compaction.postCompaction.strategy = EDGE_LENGTH if unset. Layout cached. Top-level ports are nodes.
- Port sides: inputs WEST, outputs EAST (hardware has no hanging parts).

## 3. ELK option facts relevant to hanging parts

(eclipse.dev/elk/reference/options/*)
- portConstraints: UNDEFINED (default), FREE, FIXED_SIDE, FIXED_ORDER, FIXED_RATIO, FIXED_POS (page lists values only). Layered option allowNonFlowPortsToSwitchSides: ports on non-flow sides (N/S for direction RIGHT) may switch sides if the node is FIXED_SIDE or FIXED_ORDER, decided purely by crossing minimisation. With FIXED_POS a signal pin on the N/S face is a non-flow port, so ELK must loop the wire around to reach it: our observed loop.
- layering.layerConstraint: NONE, FIRST, FIRST_SEPARATE, LAST, LAST_SEPARATE. First/last layer only; no "same layer as X". layering.layerChoiceConstraint (integer layer) is evaluated ONLY by InteractiveLayeredGraphVisitor, not in default configs. crossingMinimization.inLayerPredOf / inLayerSuccOf (blog "Layered: Constraining the Model", 2023) order nodes inside a layer, they do not assign layers.
- partitioning.activate + partition: lower index goes left of higher (direction RIGHT); ordering of whole groups only.
- mergeEdges (default false): port-less edges are merged to touch nodes at common points. Irrelevant with fixed ports.
- nodePlacement.favorStraightEdges: default on for ORTHOGONAL; works with NETWORK_SIMPLEX and BRANDES_KOEPF. priority.straightness / shortness / direction: per-edge weights, default 0.
- compaction.postCompaction.strategy: NONE default; EDGE_LENGTH used by hwschematic.
- unnecessaryBendpoints default false. crossingMinimization.strategy default LAYER_SWEEP. Neither changes column assignment.
- hierarchyHandling: INCLUDE_CHILDREN lays the compound and descendants in one run (cross-hierarchy edges routed together); SEPARATE_CHILDREN (INHERIT at root behaves like it) lays each compound separately and treats it as an opaque box.
- elk.algorithm = fixed: keeps org.eclipse.elk.position of nodes and given bend points; no routing, no spacing, no modification. Usable per compound for hand-placed children.

What ELK cannot do: put a hanging part in its host's layer by option alone. Layers come from edge direction (NETWORK_SIMPLEX minimises edge span); no same-layer constraint exists.

## 4. Other tools

- DigitalJS / yosys2digitaljs: also elkjs layered on Yosys netlists (source path 404'd, not read; unverified). Same flow-axis ports.
- hwtGraph (Python generator for d3-hwschematic): hierarchical ELK JSON with E/W ports.

## What our prototype did wrong

1. Signal pins on N/S with FIXED_POS: every comparable tool puts pins on flow sides. Hanging parts are what hardware netlists never have, so ELK has no concept for them: own layer plus a loop.
2. Multi-pin nets as stars from one pin: netlistsvg uses a 0x0 FIXED_SIDE junction dummy (deleted after layout) plus priority.direction; hwschematic uses native sources/targets hyperedges.
3. No edge priorities (netlistsvg: priority.direction 10 everywhere).
4. Leaving power out was right in spirit (netlistsvg: per-use leaf cells for constants), but the parts hanging off those nets stayed in the flow as full nodes.
5. Flat graph; tools with clusters use compounds so a group lays out as one box.

## Ranked things to try

1. Collapse satellites into their host: composite node per host plus hanging 2-pin parts (compound child with elk.algorithm "fixed" and org.eclipse.elk.position offsets from our own satellite placer), portConstraints FIXED_POS, only host flow-side signal ports exposed, hierarchyHandling SEPARATE_CHILDREN. ELK never sees hanging pins. We route host-to-satellite stubs ourselves (fixed does no routing).
2. Give hanging 2-pin parts flow-side ports: portConstraints FREE (or FIXED_SIDE with signal port WEST) plus org.eclipse.elk.layered.allowNonFlowPortsToSwitchSides true; rotate the symbol afterwards. Orientation becomes an output of layout.
3. netlistsvg junction dummy per multi-pin net: node {width 0, height 0, one port, portConstraints FIXED_SIDE}, one edge per pin, strip after layout; add org.eclipse.elk.layered.priority.direction 10 on all edges and priority.straightness high on host-satellite edges.
4. Satellites outside the sweep: run layered on the signal path only (layering.strategy NETWORK_SIMPLEX, nodePlacement.favorStraightEdges true, compaction.postCompaction.strategy EDGE_LENGTH), then place satellites at host.x above/below the pin and route a 1-2 corner stub ourselves.
5. partitioning.activate true with partitioning.partition = path-order index to stabilise column order (weakest; ordering only). layerConstraint and layerChoiceConstraint do NOT help (section 3).
