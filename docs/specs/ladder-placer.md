# Spec: the ladder placer

Status: draft for outsourcing, 2026-10-12. Reviewer: the orchestrating session (code review on PRs against
`engine/straight-over-compact`). Three independent work packages (WP1-WP3) can be built in parallel against the
interfaces below; WP4 integrates them. Each WP is a separate PR.

## 1. Goal

Place a circuit the way an engineer draws it: the signal chain on one row, left to right; two-pin parts that go to a
rail hung from the part they serve; parts between two shared nets standing as rungs between those nets' rows; supply
at the top, ground at the bottom. Today's placement (ELK block floorplan, relation placement, greedy refinement)
reaches good wire and crossing counts but produces clusters; in a blind test the user preferred KiCad's hand-drawn
sheet on all four demos. The research behind this (`docs/research/README.md`) agrees across Cadence patents, ELK-based
renderers and the one recent paper: attachments and a pinned row are structural rules, not cost terms. Three offline
attempts to bolt them onto the greedy search as constraints are in `scripts/ci/sandbox/probes/attachments/`; they got
the structure right and the geometry wrong (bodies centred on the row, slots on the host box). A fourth pass with the geometry fixed (tips on the row, bodies beyond it, slots clear of boxes) got the LM317 to
zero overlaps and two wires through bodies, but spread the attachments far along the rows (extent 144 x 33 mm
against 105 x 51) because slots always started beyond the host's box; on the amplifier the slot logic broke
(21 through-body routes, 8 overlaps). Lessons for WP2: start slots at the host pin's x whenever that pin points at
the row, keep slot pitch tight, and treat a three-pin host (a transistor) like any other host. This spec builds the
placer properly, as pure modules judged offline first.

Non-goals: digital/MCU sheets (pins by function, buses, hierarchical sheets). The existing engine remains the fallback
for any part the rules do not cover.

## 2. Repository facts the implementer needs

- Repo `C:\Users\Trey\Documents\Code`, branch `engine/straight-over-compact` (PR #8). Windows. Run Python from the
  sandbox venv `scripts/ci/sandbox/.work/venv/Scripts/python.exe` with `PYTHONPATH=.`; unit tests are `unittest`
  modules under `tests/unit/` (`python -m unittest discover -s tests/unit -p "test_*.py"`, about 690 pass). The
  engine is deterministic: iterate in sorted order, no randomness, no decisions from set or dict order.
- Geometry types in `backend/schematic/placement_refinement.py`: `PlacedSymbol(reference, position, orientation,
  geometry: {Orientation: OrientedGeometry(pins: {pin: PinGeometry(point, normal)}, box, body)}, movable, ordered,
  window, orientation_cost, flow_rank)`, `Connection(net, pins, style "wire"|"label"|"power", facing)`,
  `Placement(position, orientation)`, `rotate_geometry`. `backend/schematic/orientation.py`: `Orientation(rotation
  0/90/180/270, mirror ""/"x"/"y")`. Grid 1.27 mm; page y grows down; a pin's `normal` points out of the symbol.
- Design input, `backend/schematic/intent.py`: `SchematicIntent(symbols: SymbolIntent(reference, library,
  symbol_name, role, authoring_auxiliary), nets: NetIntent(name, kind signal|power|ground, endpoints
  PinEndpoint(reference, pin), polarity, routing), blocks: FunctionalBlockIntent(name, members, inputs, outputs))`.
  Demo intents: `scripts/schematic_demo_circuits.py` (demo_lm317, demo_buck, demo_opamp, demo_ampac); fixtures:
  `scripts/schematic_validation_fixtures.py`.
- Recorded placement inputs for offline work: `scripts/ci/sandbox/sbx/placement_replay.py` (`load_circuits(run_dir)`
  over run `auto-st3`; each circuit has `refine.args = (symbols, nets)`; `svg(result, label)` renders symbols and
  estimated routes). Headless Chrome renders SVG to PNG: `"C:\Program Files\Google\Chrome\Application\chrome.exe"
  --headless=new --disable-gpu --screenshot=OUT.png --window-size=W,H file:///...`.
- Human references: KiCad's own drawings of the four demos (paths in `scripts/ci/sandbox/sbx/demos.py`) and the
  conventions measured from 115 KiCad demo sheets (`scripts/ci/sandbox/probes/conventions/out/summary.md`,
  `backend/schematic/orientation_prior_data.py`): ground pin down 73-95%, series parts horizontal 73-90%, chips at
  library orientation 78%, shunt parts hanging from their net on 39 of 40 sheets.
- Code conventions: a module docstring that says what the module does and why, in plain English; comments only where
  a reader would ask why; no circuit names in the engine (demo names belong to the sandbox); every rule has a unit
  test; nothing touches the verified (shipped) profile.

## 3. WP1: roles (`backend/schematic/ladder_roles.py`)

Pure. Inputs: `SchematicIntent` and `pin_counts: Mapping[str, int]` (pins per symbol, supplied by the caller).

```python
@dataclass(frozen=True)
class Attachment:
    host: str; net: str; rail: str; side: Literal["ground", "supply"]

@dataclass(frozen=True)
class Rung:
    net_a: str; net_b: str

@dataclass(frozen=True)
class Feedback:
    net_from: str; net_to: str

@dataclass(frozen=True)
class Lane:
    hub: str; pin: str; net: str
    series: tuple[str, ...]               # the two-pin parts in series from the pin, in the order they connect
    nets: tuple[str, ...]                 # the pin's net, then the net beyond each series part (one longer than series)

@dataclass(frozen=True)
class Roles:
    rails: dict[str, str]                 # net -> "ground" | "supply" | "negative"
    trunks: tuple[str, ...]               # signal nets with >= 3 part pins, in order of first appearance along the chain
    chains: dict[str, tuple[str, ...]]    # block -> chain parts in flow order; blocks themselves in flow order (the layout uses it to order two blocks no pin or drawn flow orders)
    attachments: dict[str, Attachment]
    rungs: dict[str, Rung]
    feedback: dict[str, Feedback]
    pairs: tuple[tuple[str, str], ...]
    other: tuple[str, ...]
    lanes: dict[str, Lane]                # "<hub>.<pin>" -> the lane from that hub pin (rule 8)

def classify(intent: SchematicIntent, pin_counts: Mapping[str, int]) -> Roles: ...
```

Rules (general; no circuit knowledge):
1. Auxiliaries (`authoring_auxiliary`, power flags) are ignored. Rails: kind ground -> "ground"; kind power with
   `polarity == "negative"` -> "negative"; other power -> "supply".
2. Chain per block. Seeds: sources (reference prefix V or I; library `Simulation_SPICE` sources), symbols with role
   `input_connector`, or, when the block declares `inputs`, the parts holding those nets' pins. Sink: the parts
   holding the block's `outputs` nets, or role `output_connector`, else the part farthest from the seed. The chain is
   the shortest path by part count over non-rail nets from a seed to the sink that passes through the block's part
   with the most pins (ties: lowest reference). Two-pin parts are chain members only when on that path. A one-member
   block's chain is that member. The nets between consecutive members are the "chain nets", and so is a net joining
   the last chain part of one block to the first chain part of the next block in flow order (an interface net: it lies
   on the chain row and is never a trunk row, even with three or more pins). A rail that a block lists among its `inputs` or `outputs` is, for that block, a chain net
   and not a rail (it seeds the block's chain at the parts holding that rail's pins, such as a regulator's VIN, and
   joins consecutive blocks like any interface net, with its power symbols drawn by the terminal stage as before),
   because power sheets chain their stages through named rails (+24V into a buck, +5V out of it into an LDO) and
   without this the second stage has no chain and falls to `other`; rails no block declares stay rails, and a
   two-pin part between such a chain rail and a true rail (an output capacitor) attaches to the chain part on the
   chain rail. Blocks that pass nothing on (no `outputs`: an indicator, a load) are ordered after those that do,
   because they cannot stand between a producer and the blocks that take its output. When a block's other members form further disjoint paths from its input nets to its output nets (the four gate resistors of an H-bridge, each between a header pin and a gate), the block yields one chain per path, named `<block>#1`, `#2`, ... (the main path first), and the layout stands them as parallel rows like any other blocks.
3. Attachment: a non-chain two-pin part with exactly one rail pin and one non-rail pin. Host: the chain part sharing
   the non-rail net; else the non-chain part with the most pins on that net. Side: ground/negative -> "ground",
   supply -> "supply".
4. Rung: a non-chain two-pin part with no rail pin whose two nets are both trunks, or one trunk and one chain net.
5. Feedback: a two-pin part between two chain nets that are not adjacent along the chain.
6. Pair: two parts with 3+ pins sharing a non-rail net through pins of the same name or number (a differential pair's
   emitters); each pair listed once, sorted.
7. Everything else -> `other`.
8. Lane: a hub is a chain part with six or more pins (a controller). Each hub pin whose net is not a rail, not a chain net
   and carries no pin of a chain part other than the hub's own starts a lane when a two-pin `other` part with both nets off
   the rails is on its net. The lane takes the lowest-referenced such part, then repeats from the net beyond it, until none
   is left: a part whose far net reaches a chain part (and no pin of the hub) is not taken, a part is in one lane only, pins
   are taken in order. Lane parts leave `other`. Shunts on a lane's nets stay attachments (rule 3) and rungs stay rungs; the
   layout hangs them from the lane. The chip's pins are fanned into lanes because that is how people draw a controller:
   the compensation network on its pin, the boot capacitor from its pin to the switch node, the bypass capacitor on its pin.

Tests: the four demos (expected: LM317 chain source -> T1 -> D1 -> U1, with D2 either on the path or feedback, state
which; attachments C1 on D1 and C3/C2/R1 on U1 or on their trunk hosts; rungs R4 and D3 between `out` and `adj`;
buck chain through Q1 and L101 with D101/C101 as attachments; amplifier pair Q2/Q3); a toy intent exercising every
rule; shuffled symbol/net order gives identical Roles.

Acceptance: tests pass; the PR body carries a table of the Roles found for all four demos and every ambiguity resolved.

## 4. WP2: layout (`backend/schematic/ladder_layout.py`)

Pure. `ladder_layout(symbols: Sequence[PlacedSymbol], nets: Sequence[Connection], roles: Roles, *, text_widths:
Mapping[str, float], gap_mm=2.54, slot_pitch_mm=7.62) -> dict[str, Placement]`. Places chain, attachment, rung,
feedback and pair parts; `other` parts are not placed (the caller keeps their existing positions). Build against the
WP1 interface with hand-made Roles in the tests.

Rules:
1. Orientation by role, chosen among the symbol's available geometries: chain two-pin parts horizontal with the
   earlier chain net on the left; chain multi-pin parts in the orientation where the chain-input pin's normal points left and the
   output pin's points right (else one where either does; else rotation 0, mirror "y" if the input then points right
   and the output left; ties: lowest rotation, then no mirror), because a transistor left with drain above source
   runs the chain vertically through its own body and every attachment route hanging from it hits that body. "Input" and "output" are decided among a part's *through* pins (a pair of pins whose normals are opposite: collector/emitter, drain/source, VI/VO); a pin off that axis (base, gate, ADJ) is a *control* pin and is never a chain pin. The block that feeds a through pin sits on the row beside the part (before it for the input pin, after it for the output pin). Hanging the control feeder (this paragraph's rule A) applies only to a part the path runs straight through, that is, one whose through pins both meet other chain parts (a FET or regulator in a power path, which may be turned to lie on the row); a transistor whose axis pins point up and down and only one of which carries the chain (a BJT stage: collector to the next stage, emitter to a support) keeps its upright orientation, is entered at its control pin from the row's side (base from the left) and leaves at its upper axis pin, and the block feeding that control pin sits on the row to its left, never below it, because people never turn a BJT and draw the stage left to right. The block that feeds a control pin of a path-through part is not on the row but hangs from that pin as a column, perpendicular to the row on the side the pin points to (a control pin carrying a column points down when an orientation allows), its last part nearest the pin, its first farthest, each part upright, and nothing else is placed in that column. People keep the power or signal path straight through a device and hang its control, so the gate drive of a switch stands below its gate instead of strung on the row; attachments vertical with the rail pin toward the rail (ground down, supply
   up); a source that feeds the row (any reference `designators.is_source_reference` accepts) stands vertical below the row's left end with its chain pin up and its tip on the row line and its other pin down, and whatever is on that other pin's net that is not a rail (a sense resistor) hangs below the source in the same column, then the rail, and the chain's first non-source part starts beside it, because people draw a signal source upright, + onto the row and - to ground, rather than lying on the row with its wire running back through its own body; rungs vertical; feedback parts horizontal; pair members that follow one another in a chain stand face to face on the chain row, the second the mirror of the first about the vertical axis so their control pins point outward and their shared net is one straight wire between the two tips, with the part hung from that net at its midpoint and each member's collector load above its own collector (a differential pair is drawn face to face, which is what the `pairs` role exists for); the chain continues from the second member's output pin on its outer side. A part whose pins all point one way (a connector, a header) at the start or end of a chain is placed at that end of the row
   with its chain pin pointing towards the circuit (an input connector's pins point right, an output connector's left),
   a ground pin below it and a supply pin above it where an orientation allows, its other pins fanning onto rows above and
   below as side rows do, because people draw a connector at the edge of the sheet with its pins facing the circuit and
   a pin that fans out says nothing of the order of the blocks. Other pair members stay at rotation 0, side by side.
2. Rows per block, top to bottom: supply attachments; the chain row (y0 = 0); one row per trunk that is not a chain
   net, in order of first appearance; ground attachments. A trunk row lies below the row above it by the tallest part
   between them plus 2*gap (a rung's pin spacing sets the distance between the two rows it joins when it is the
   tallest). An interface net between two blocks is a chain net and has no row of its own. Blocks share one chain row only when an interface net joins them in flow order (the next block meets the row's last block). A block with no interface net to the blocks already on the row starts a new row, left-aligned with the first and below the extent of the rows above by 2*gap; a block that feeds several rows (a shared source) goes once, on the left of the first row it feeds, and its net reaches the other rows as a wire (no trunk row for it). Parallel circuits fed by the same input are drawn as separate groups, not one long row with the input at its far end. A block whose net meets a part, already placed on the main row by the block before it, at a pin that points up or down (a collector fed from a load network) is not on that row but stacks over (or under) the part, its own rows running horizontally with its own hung parts and its net reaching the pin as a run along its top row and a straight drop, because people keep the main path on one line and stack a part's load above it; a part entered at a control pin from a part that leaves by an upper axis pin stays on the main row and the net takes the L the estimator makes, the row does not step up. A block joined to the rest by rails alone (a supply) stands in a column at the left, above the first row, each below the one before; a part the roles leave as `other` whose pins are all on rails (a second supply) joins that column, higher rail pin up, because it is a supply too and belongs with the others, not wherever the engine happened to leave it. Attachments hang: the signal-pin tip sits exactly on its row line and the whole body lies beyond the row
   (below for the ground side, above for the supply side). Never a body centred on a row.
3. Columns: single-part blocks are ordered by drawn flow (a multi-pin part judged upright, a pin facing right or down is an A rail pin on the same side of a multi-pin part as its chain pin occupies one slot (the slot pitch) on that side, between the chain neighbour and the part, because a power symbol hangs there (ground down, supply up) and the wire into the chain pin must pass it: the spacing on that side grows by one slot pitch per such rail pin.
   output, left or up an input; a block touching an ordered block with no direction of its own follows it, so a switch
   precedes its filter inductor and that its load); chain parts left to right; each part's x = the previous part's right box edge + the room between the two + its own left extent. The room between two consecutive chain parts (also across a block interface) is `gap` when nothing hangs from the net between them, else `gap` + (the number of parts hung from that net, above or below, whichever side has more) * the slot pitch + `gap`: the text of a horizontal part sits above or below it and needs no row width, and a hung vertical part's text sits beside it, which the slot pitch already allows for (measured: hung parts sit at about 7.62 mm pitch in human sheets, and the median nearest body gap is 6.6 mm); no other reservation is made for supports, interfaces or text. The chain's net pins lie on the chain row; for a multi-pin chain part
   whose input and output pins differ in y, the input pin lies on the incoming row and the chain continues from the output pin at that pin's own y (the row steps there and later parts of the chain align to the new y), because people keep the signal entering a stage straight and let it leave at the collector's height; entering on the output pin instead gives the input wire a jog and strands the base pull-down off the base. A chain part's pin that is not on the chain row leaves the part along its own direction and its net gets a row of its own on that side: a pin pointing up a row above the part (beyond the supply attachments between), one pointing down a row below, `gap` beyond the pin tip, and the other parts on that net hang from that row with the attachment rules; a net two chain parts share on one side is one row at the outer of their pins, so the wire between them is straight (the far side of a transformer or a bridge is wired by people with a straight run over the top and under the bottom, not an L across the body). A net that leaves a part's upper (lower) axis pin for a pin of another part at another height has such a row when parts hang between the two, and the row lies above (below) the tallest of those parts, as placed, by `gap`, not merely `gap` beyond the pin tip, because people run a wire over a divider, not through it; what hangs on the side the wire drops to stands before the row's far end and what hangs on the other side at or beyond it, so the drop passes no part. A chain pin that points up or down while the part's other chain pin faces along the row is such a pin too, and the row runs through the other one. A pin pointing left or right that is not a chain pin joins such a row only when it sits on that side of the part and shares the net with one; otherwise its attachments take the slots beside the host.
4. Attachments: a slot on the wire of their net: the trunk row, or for a chain net the y of the chain pins on it. A part
   hangs from the pin it serves, which on a wire between a two-pin and a multi-pin chain part is the multi-pin part's pin
   (a base pull-down stands by the base, not by the coupling capacitor). Candidate x: the
   host's signal-pin x when that pin points toward the hanging side (standing off along the pin by whole 2.54 mm steps
   until the two boxes clear, as an emitter resistor sits under its emitter); else, for a net between two chain parts,
   the slots along the segment between them, below it for the ground side and above for the supply side, the tip on the
   segment at the slot nearest the serving pin, then outward along the segment (2.54 mm steps), because a hung part
   belongs on the wire it joins, beside the pin it serves; then, when the segment is full (and for a net with no
   second chain pin), the host box edge + slot pitch on the side where the pin exits, then every slot pitch outward. The room for these is the spacing computation of rule 3, not a separate reservation. A slot is taken when the attachment's body grown by `gap` meets any placed body (rule 5), or its
   tip is within 2.54 mm of another part's pin tip; a part whose tip would sit nearer stands off along its wire, away from the row, by whole 2.54 mm steps (a diode under a source's tip sits one grid step down the wire). Rungs: a slot between their two rows, tips on both row lines (if the row
   distance exceeds the pin spacing, upper tip on the upper row; the lower wire extends). Feedback parts: a row above
   the supply row (or above the chain row when there is none), spanning their two nets' columns, where a feedback end that joins a multi-pin part's pin joins the wire of that net at a slot on the chain segment entering that pin, the one nearest the part after the slots of any rail-pin part (three slots out when a pin beside it points the same way, whose stub and power symbol need the room), never in the pin's own column, because a feedback return meets the input wire beside the chip, where people draw its junction, and a drop down the pin column passes the neighbouring pins. Pairs: side by side
   at one slot pitch. A pair member beside a partner already placed, and a feedback part on the same two nets as one already
   placed (parallel capacitors), take the next free slots outward along the wire, whole 2.54 mm steps, never a
   point whose body, grown by `gap`, meets a placed body or whose tip is within 2.54 mm of another part's tip,
   because two parts must never share a point. A slot's route check compares the estimated routes of the part's nets
   with and without the part and rejects only what the part adds: a wire already bound to pass a body (a far pin of
   the net beyond it) is not the part's doing and must not close every slot. When no slot is found beside a chain
   part, the gap before that part (after it when the pin points along the row) grows by the hung part's width and the
   group is placed again, up to five times, because a hung part is wider than the pitch the first spacing assumed.
5. Legalise: snap anchors to 1.27 mm; no two placed bodies may overlap (raise naming the pair if they do), where a body is the box less the 1.27 mm pin lead on each side a pin points through, and no two parts' pin tips may coincide (raise naming the pair). Pin tips are connection points and the box's lead padding is not body, so tips may share an x or y on one straight wire; but at least 2.54 mm of wire must stay between them, because in 215 human sheets two tips directly on each other with no wire are an accident (0.5% of tips, no junctions) while a stack with a short wire between the tips is the convention (0.9% of tips, 106 pairs on 48 sheets, always wire-joined).

6. Lanes: people fan a controller's pins into lanes, so that the chip's sides read as a list of its functions. A lane is a
   row of kind "lane" at the pin's height on the pin's side of the chip (a pin that points up or down, or is a chain pin
   of the row, has none: its parts stand in a row under everything, a fallback); lanes keep the pin order and the pin
   pitch, and a pin with nothing on it takes no room. The parts of a lane (`roles.lanes`, rule 8) are laid outward from
   the pin tip: each series part lies along the lane with its pin on the net nearer the chip toward it, its near tip a
   wire's length (2.54 mm) beyond the tip before and a body clear (gap) of everything placed, by the slot rules of rule 4
   (whole 2.54 mm steps outward, the nets' estimated routes crossing nothing new). The room beside the chip grows by the
   longest lane's parts end to end and a slot pitch for what hangs from its end, on that side, like the room of a hung
   part in rule 3, and only on the sides that have a lane. What hangs on a lane's nets is placed in the order it connects,
   before the next series part: shunts by rule 4 (one hung part on a pin of a hub with lanes is the degenerate lane and goes
   through the same code; its first slot is the slot rule's, a lane's later nets take the first slot a wire's length beyond
   the last tip), rungs by the span rule below. A lane's last part whose far net leaves the hub by another pin on the same
   side, and any rung between two such nets, is a span: it stands between the two lanes, the boot capacitor from BOOT across
   to SW, the upper tip on the upper lane and the lower on the lower. Where the lanes' pitch is less than the part's pin
   spacing one tip stands off its lane (the wire joins it with a jog): the tip of the lane the span hangs from when that
   lane's wire runs on past it (a T onto a run must be straight), else the tip of the lane the span ends on. Lanes that hang
   parts downward (ground side) are placed from the lowest up and those that hang upward from the highest down, so that a
   lane's hung part stands beyond the ends of the lanes it passes and the lanes end in a staircase, not a crossing. A lane
   part with no lane to stand on stands in a row under the group, so that every part of `roles.lanes` is placed.
   The plan records a lane as a `Row` of kind "lane" named for the net on the pin, `above` the hub pin ("U9.4"), `order` its
   series parts; the shunts are `Hang`s and the spans `Span`s.

7. Stacked pairs: a net that joins two chain parts of three pins or more whose pins on it face each other across rows (upright,
   one facing down and the other up, both on the part's through axis) is the junction of a stacked pair, and the two parts
   stand on one vertical column, the upper's down-facing pin directly over the lower's up-facing pin, the net one straight
   vertical wire between the tips with the net's other parts (the load) hung from or reached from that wire sideways,
   because people draw a half bridge, a totem pole or a cascode as a vertical stack, and as two chain rows the junction
   would need a trunk lying at once above the upper row's down pin and below the lower row's up pin, which no straight drop
   reaches. Neither part is on a chain row; the plan records a `Column` whose pin reads `net:<junction>` (upper first), a part
   hung on the wire as a `Hang` (side left or right, the side away from the control pins), and a two-pin part joining the
   junctions of two neighbouring pairs as a `Span` between their nets. Columns are ordered left to right: the columns a two-pin load joins are neighbours, the load's first net on the left, the rest by the order of the roles; each column's control pins face outward (alternate columns mirrored, the first facing left), so that nothing reaching a gate passes between the columns. A two-pin part alone on the net of a stacked part's control pin (a gate resistor) is that pin's feeder: it is not on a chain row but hung from the gate's wire on the pin's own side, its near tip a gap from the pin's tip and level with it (a `Hang`, host the control pin, side left or right), because a feeder standing apart on its own row interleaves with the other stack's feeders (rows are ordered by height, gates are not) and the nets between gates and junctions then cross in every realisation. The plan lists the `net:` columns left to right and a legaliser keeps that order. (A two-pin part would be turned instead so that both pins face the same trunk row; no
   such case is implemented: a pair of two-pin parts is an ordinary chain.) A connector at the start of a row is never given an
   input pin by inference: its pins fan out and say nothing of the chain.

Tests: a toy LM317-like ladder (source -> chip with VI left, VO right, ADJ down; rungs R4/D3 between out and adj;
attachment C3 on out; R1/C2 on adj; a load as chain tail plus attachment) gives one chain row with the chip's VI/VO
tips on y0, rungs vertical with tips on both rows, attachment tips on their rows with bodies beyond, no overlaps;
shuffled input gives the same placement; a slot occupied by a chain part is skipped.

Stacked-pair tests: a toy half bridge (two three-pin parts in series between +V and GND, the junction to a two-pin load) places the upper's down pin over the lower's up pin with the load beside the wire; two half bridges with a load across their junctions stand side by side with the load's tips on both wires and the control pins outward; shuffled input identical; the plan holds the `net:` columns, the hang and the span.

Lane tests: a hub in a row with a two-part lane on the left (series R, then C to ground hung from its end) and two lanes on the right with a capacitor spanning from one to the other, an empty pin that takes no room, shuffled input identical, the recorded plan holding the lanes, a lane on a chain pin leaving its parts in the row under the group.

Acceptance: tests pass; the PR carries an SVG of the toy ladder (via `placement_replay.svg`'s approach or a 30-line
renderer) that reads as a ladder.

## 4b. Plan and legaliser (the seam between the grammar and the geometry)

Decided 2026-10-12, after the first corpus runs. Section 4's rules are two things at once: a *grammar* of how engineers
arrange a sheet (a dozen structures: the signal path through a device's axis pins, supports hung from the pin they serve,
feedback over, supplies up and grounds down, pairs face to face, stages chained through rails, parallel stages stacked,
a hub's pins fanned into lanes, connectors at the edge) and a *legaliser* that turns the structure into coordinates (slot
searches, stand-offs, retries). The grammar is small and converges; the legaliser grew by patches and does not. They are
split along this contract so each can be worked on alone.

**The plan** (`backend/schematic/ladder_plan.py`, pure data, JSON-able, no coordinates):

```python
@dataclass(frozen=True)
class Row:            # one horizontal line parts sit on; rows are ordered top to bottom
    name: str          # "<block>#<n>" for a chain row, "<net>" for a trunk/side/lane row, "supplies"
    kind: Literal["chain", "trunk", "lane", "side", "supplies"]
    above: str | None  # the row this one lies under (None for the first chain row); lanes name their hub pin
    order: tuple[str, ...]   # references left to right in the order the grammar fixed (chain parts, pair members)

@dataclass(frozen=True)
class Hang:           # a part hung from a wire
    reference: str
    wire: str          # the net whose wire the tip lands on
    host: str          # the pin the part serves ("Q1.3"), which fixes where along the wire it prefers to be
    side: Literal["above", "below", "left", "right"]
    orientation: Orientation          # chosen by the grammar (rail pin toward the rail, etc.)
    rank: int          # among hangs on the same wire and side, nearest the host first (0 = nearest)

@dataclass(frozen=True)
class Span:           # a part whose two tips join two rows or two columns (a rung, a feedback part, a bridge)
    reference: str
    from_wire: str; to_wire: str
    orientation: Orientation

@dataclass(frozen=True)
class Column:         # a vertical run hung from a pin (a control feeder, a source with its sense resistor)
    pin: str; order: tuple[str, ...]; orientations: tuple[Orientation, ...]   # top to bottom

@dataclass(frozen=True)
class Plan:
    rows: tuple[Row, ...]; hangs: tuple[Hang, ...]; spans: tuple[Span, ...]; columns: tuple[Column, ...]
    orientations: dict[str, Orientation]      # every placed part
    pins: dict[str, str]                       # which pin of each chain part lies on its row ("U1.3")
```

A hang is recorded against its wire (the net its tip lands on); `host` names a pin only to rank the hangs of one wire and side and to say where along the wire they prefer to stand, and it may be a pin of any part, a hung part, a span or a lane part included (an LED under its series resistor is two hangs on the node's wire, the resistor above and the LED below, each naming the other's pin), so a legaliser must not require a host to be a chain part or to be placed before the hang.

The grammar (`ladder_layout`'s rule code, later a `ladder_grammar.py`) emits a `Plan` from the `Roles` and the symbols. It
must not emit coordinates. Every rule in section 4 is restated as a plan decision: rule 3's "row through the input pin" is
`pins`; rule 4's slot order is `Hang.rank`; rule 2's stacking is `Row.above`.

**The legaliser** (`backend/schematic/ladder_solve.py`): `legalise(plan, symbols, nets, *, gap_mm=2.54, pitch_mm=7.62)
-> dict[str, Placement]` assigns every part an anchor on the 1.27 mm grid with CP-SAT (`ortools`, one worker, a
deterministic budget, as the field placer does). Variables: x per part; y per row (rows keep their order, consecutive
rows at least the tallest part between them plus 2*gap apart); a hang's tip y is its wire's row y and its x lies within
the wire's span, nearer the host than any higher-ranked hang on that wire and side. Hard constraints: bodies (the box minus pin leads) keep at least `gap` (2.54 mm) between them, i.e. grow ONE body by `gap` and require no overlap with the other, never both (the human sheets' 10th-percentile body gap between two different passives is 2.54 mm; the median, 6.6 mm, is for the objective to reach, not the constraint); the same single-sided `gap` applies between a wire rectangle or a stub reservation and a body it is not joined to; tips of different parts are at least 2.54 mm apart; every wire is a row
segment or a straight drop, and each such segment, as a thin rectangle, does not overlap any body it is not joined to
(`AddNoOverlap2D` over bodies and wire rectangles); chain order along a row is kept; a rail pin beside a chain pin keeps
a slot for its power symbol; lanes keep pin order. Objective (revised 2026-10-13): after the hard constraints, wire length (sum of
tip-to-tip Manhattan distances along the plan's wires) plus 50 mm for every crossing (below), then extent. Three clarifications (2026-10-13): (1) every pin on a rail net
reserves a stub rectangle from its tip 7.62 mm along the pin's normal and 2.54 mm wide, kept clear of bodies and foreign
wires exactly like a body, because the build's terminal stage draws the power symbol there and fails outright when it
cannot ("No unobstructed power terminal exit"); rule 3's rail-pin slot is one consequence of it. (2) A wire touching a
foreign pin tip or ending on a foreign conductor (the checker's `short`) is a hard exclusion: the router refuses it.
(3) Two nets' wires crossing in the interior of both is a soft term and the first objective: the router allows it,
people's sheets carry zero to four, and making it hard risks infeasibility for nothing, but a crossing is a readability fault
and wire does not buy one cheaply: it costs 50 mm of wire, about a row's width. The solver then avoids a crossing whenever an
ordinary rearrangement removes it and accepts one only where avoiding it means a long excursion (a detour of hundreds of
millimetres to dodge one crossing is never drawn, and crossings are occasionally necessary). The price is finite, so a
crossing never makes a plan infeasible. `RANK_CROSSING_MM` (8 mm) is the checker's reporting weight, not the solver's trade. Infeasible plans raise naming the constraint and the parts, never silently
stand off.

WP5 makes the following interpretations explicit (general rules, not fixture settings):

- `Plan.orientations` names every part being placed, including standalone parts. A reference may belong to at most
  one row, hang, span or column. Symbols outside the plan remain the caller's responsibility; an attachment to an
  unplanned host is reported with both references. Row pins accept a bare number, `REF.number`, or an empty string
  for an anchor on the row. `Plan.sequence` preserves chain order across row steps; an empty column host denotes a
  supply column left of the frame and above its first row.
- A named trunk uses its row height. Otherwise a wire inherits its first chain endpoint's height in plan order.
  A hung net fed by a vertical chain pin pointing toward the hang instead has a free trunk height, reached by
  outward drops from chain pins. Any such feeder on the net qualifies, even when the declared host is horizontal.
  The hang's own signal tip stays exactly on the trunk, with its whole body on the declared side; only the chain
  feeder may drop to the trunk. Vertical pin drops leave along their outward normal, rather than returning through
  their own body. Hangs never stand off. Unnamed wires between multiple chain endpoints bound the
  hang's x to those endpoints' span. Rank orders absolute distance along the wire from the same declared host,
  within each wire/side/host group; it is a nondecreasing order, not a pitch or a slot index.
- A vertical span's upper tip meets its upper trunk, with a straight lower drop when needed. Horizontal feedback
  lies above both trunks; its horizontal leads and vertical returns stay clear of foreign bodies and pins.
  Column connection tips share their host's x and members keep their top-to-bottom order. Row separation accounts
  for intervening hung bodies; body clearance separately constrains row members and spans. A lane naming a hub pin
  keeps that pin's height and outward side, with at least 2.54 mm before its near tip and its parts in order outward
  (left or right as the pin faces); a lane's row keeps the rows' order and no more, because its height is the hub pin's,
  whatever the pin pitch, and body and wire clearance hold its parts apart. A series part of a lane that the plan also
  records as a span is placed as that span (a horizontal one has both tips at the lane's height and its two nets continue
  along the lane); it belongs to the lane's row only for height and order. A vertical span's lower tip may stand above
  its lower trunk with a straight drop, so the trunks need no more than the tips' separation between them.
  A hang's signal tip is the contact: a symbol's box can reach past it (padding, not body), so the part is on its
  declared side when that pin points toward the wire. A wire between chain pins that are not all at one row height, and a
  wire a sideways chain pin serves a hang on, has a free trunk height (straight drops outward along each pin's normal,
  arrival leads of at least a gap, longer when a rail pin's stub stands in the way), preferring the height of its first
  chain pin (or of the host pin) on a tie; a net whose pins face away from each other across the rows has no such
  trunk and the plan is infeasible, named as `drop direction`.
- The body is the geometry `box` inset by 1.27 mm on each side through which a pin points, rather than the scorer's
  `OrientedGeometry.body`. Body clearance is one gap measured once: one body is grown by `gap_mm` and must not overlap
  the other (minimum clearance `gap_mm`, never `2*gap_mm`); a conductor rectangle is kept `gap_mm` from every body of a
  part that is not on its net, and a rail pin's stub reservation `gap_mm` from every body that is not its own pin's.
  Euclidean tip separation includes unconnected pins and permits exactly 2.54 mm. Anchors are on the 1.27 mm grid;
  row heights may be fractional-grid because pin offsets are. Geometry is represented in integer micrometres.
- Every rail pin reserves a rectangle 7.62 mm outward from its tip along its normal, 2.54 mm wide. Foreign bodies
  and foreign wire rectangles cannot enter this reservation. The chain-side rail slot also reserves `pitch_mm`
  per same-side rail pin. These are hard constraints; they protect the terminal builder's unobstructed exit.
  Label terminals have their own 5.08 mm conductor but no power-symbol reservation.
- Bus, lead and drop rectangles are 0.002 mm thick. A bus excludes its joined endpoint bodies, and each lead/drop
  excludes only its own endpoint body. Same-net junctions are allowed. A conductor touching a foreign pin tip,
  or an endpoint touching a foreign conductor, is a hard short constraint. Different nets may cross strictly
  inside both conductors; those crossings are soft and the first objective (see Objective).
- Objective (revised 2026-10-13, three times): readability over compactness, in one weighted objective. Total wire
  length, plus 50 mm of wire for every interior crossing (a crossing is a readability fault; 50 mm is about a row's
  width, so the solver removes a crossing by any ordinary rearrangement and keeps one only where avoiding it needs a long
  excursion, and the price is finite so a crossing is never infeasible), plus extent (width plus height of the union of
  geometry boxes) at the checker's price, 0.08 mm of wire per mm (`placement_refinement.Weights.extent`), which is also why
  a part nothing pulls on (one whose wires end at parts the plan leaves out) does not stay wherever the search put it, as it
  did, a metre away, when extent came last and was never reached. A bus's horizontal span is counted once, with every
  lead/drop counted once; power/label terminal stubs do not enter wire length. The search is staged only to keep its first
  steps simple: wire and extent alone (at most 40% of the budget), then the full objective with crossings from that
  placement. Finally, ties prefer a free trunk at its sole horizontal feeder's height and its hang aligned with its sole
  vertical feeder, without trading any preceding objective (the optimum is fixed, then this is minimised). Translation is
  fixed at the first row member's anchor (or first reference), and domains come from input sizes (per axis: the sum of the
  parts' extents, a gap either side, a slot per pin, and a grid). A conductor keeps the checker's 0.5 mm route margin from
  any body of its own net that it does not end at (the length alongside it, not its ends: a drop may end on a host's tip,
  which is on the host's body edge).
- A wire may cross the body of a part it joins only where a pin exits (2026-10-13): from the pin's tip outward to the
  body's edge, the pin's width (one grid) wide. A trunk, drop or lead anywhere else through the body of a part of its
  net is a "through" in the checker, as it is for the other parts. It is a priced fault, never a reason to lose a
  sheet: each wire rectangle through a joined body costs `_THROUGH_MM` = 150 mm of wire (three crossings), in the wire and
  the weighted objectives, for the pairs that stand close together in the first placement or that a solution breaks.
  Which parts are priced: a hung part's body always; every joined body when the exclusion alone proved the plan
  infeasible. Otherwise a joined body is excluded like any other (the first attempt), because the priced version of the
  whole rule searched worse in the 20 s budget (measured over the 14 fixtures: amp 360.7 mm against 270.5, uno_r4_power
  +87 mm, pico 363.0 on the old plan against 301.1). The sheets that need the priced fallback: gamma_preamp's N_D1_3 (the
  plan's row order puts D1 between R5 and U2 with D1's pin of the net facing away from U2) and tally_timecode's U5 (two
  units merged into one 53 mm body, the pins of the second inside it, each reachable only through another net's tip).
  `Placements.joined` says which ("excluded" or "priced"). Rail and label stubs are not wires for this. A conductor is a
  line two micrometres thick, so the body is taken one micrometre smaller each side: a wire along a body's edge (a
  stack's junction wire beside the part across it) touches it and is let.
- Stacks stand left to right in the order of the plan's `net:` columns, and the tips of a span across two stacks are
  ordered by its from_wire: the from_wire stack is the left one. Both are constraints ("stack column order", "stack
  span order"), not a sort by name.
- Where the 20 s goes (2026-10-13, per-run log in the solver stats): the probe takes 2.0; the wire stage takes 75% of what is
  left (13.5) whenever no checked placement exists when it starts, which the probe's never is; the weighted stage gets the
  remaining 4.5 and on amp, uno_r4_power, tally_power and gamma_sipm is still improving when the limit hits, while the wire
  stage made its last improvement at 3 to 6 and idled for 7 to 12. Ending the wire stage at its 40% share once it holds a
  placement (`_STOP_WIRE`, off) gives the weighted stage about 11: opamp then proves its optimum (17.3), tally_power
  improves (113.0 to 92.7 mm), but amp goes from 0/0/0/0/0/274.6 to 1/0/0/0/1/256.5 and uno_r4_power from 0/0/3/0/4/537.2 to
  2/0/1/0/2/479.0 (two hung-part throughs); at a 60% share amp is 347.4. The same input gives a different local optimum for
  a different time split: the weighted stage does not converge in the time either split gives, so the split is not what
  limits it, and the default stands.
- The weighted stage is monotone already. The wire stage's result is not fixed (it is a "short search", `fix` false); only
  a proved weighted optimum is fixed, for the alignment stage. The wire stage's incumbent is the hint, and the weighted
  stage's first incumbent costs exactly what that placement costs (verified on 13 sheets by completing the placement with
  everything else optimal and comparing: amp 12,031,495 both). What makes a stage look worse is that its objective grows:
  a crossing or through term is written only when a solution breaks it, so a placement is dearer once its faults are
  found (tally_power's weighted cost goes 2.57M to 4.65M over its stage), and a final incumbent can carry faults found too
  late to search again. `_MONOTONE` (off) starts the stage from the completed placement and keeps the better of start and
  end under the objective as it ends; it never had to take the start, and it changes the trajectory (pico 4 s against 21
  s, tally_power 82.5 against 113.0 mm, amp 1 through at 251.7 against clean at 274.6), so it is not a gain.
- The placement returned is chosen by true cost (`_SELECT`). The latest `_KEPT_PER_STAGE` = 4 clean placements of each stage
  are kept; each is scored on the wires it draws with the checker's own counts (`_Model.cost` on the drawn nets), at the
  prices (wire and extent, `_CROSSING_MM` per crossing, `_THROUGH_MM` per through, `_SHORT_MM` = 300 per short: a connectivity fault, twice a through), and the cheapest is
  returned, the one the search ended on if none is cheaper. A fault pricing found too late then cannot be the shipped sheet
  when a cleaner incumbent exists. Over the 14 fixtures it changed two answers: tally_power takes its wire-stage placement
  (102.9 against 185.8 at that price: the weighted stage priced the faults it found and ended dearer) and returns 0/0/1/0/0/82.5 against
  0/0/1/0/0/113.0; tally_timecode takes its wire-stage placement (1031.7 against 1774.8 at the short price of 300) and returns 5/0/21/3/7/725.4 against
  3/0/17/6/6/753.4. The other twelve keep the placement the search ended on.
- bitaxe_power (29,267 variables, 38,878 constraints) finds no placement in 20 det because its first optimisation run, hinted
  with the probe's placement, finds none in 13.5 det (no improvement, 103 s wall), while a run without an objective finds a
  legal one in 3.7. `_FEASIBLE_FIRST` (off) does that first, from the plan's own layout hints: a placement results, but a
  poor one (0/0/20/0/35/7071.6 mm, against the heuristic's 1544 for the same parts; from the probe's placement 37654 mm),
  the search improves it five times in 15 det, and the run takes 137 s wall, past the 120 s guard. On the other 13 it
  changes the answers without a pattern (amp 0/0/0/0/2/293.4, tally_power 91.4, uno_r4_power 0/0/0/0/4/539.7). Off.
- `legalise(..., hint=layout)` takes the heuristic placer's layout of the plan (`plan_and_layout`'s second value) as the first
  search's hint: grid anchors from the origin part for the parts it has, row heights from the first member of each chain row;
  a hint, never a constraint (a test gives it an illegal and a legal layout and gets the same answer). The sandbox passes it
  only with `KICAD_AI_LADDER_HINT=heuristic`, because it is not better: bitaxe_power still has no useful placement (no
  placement at all, or 28,358 mm with feasibility first, against the heuristic's 1544), and of the others amp goes from
  0/0/0/0/0/274.6 to 0/0/0/0/2/264.4, uno_r4_power from 0/0/3/0/4/537.2 to 0/0/4/0/8/507.5 (its wire stage takes the whole
  budget), tally_timecode gains an overlap (6/1/18/3/9/676.9), gamma_sipm 280.7 to 288.3; piaa_power (39.4), bms_supply (one
  crossing fewer) and pico_hbridge (4 s, 0/0/3/0/2/271.8) gain. bitaxe_power's model: 28 parts, 8 rows, 18 wire nets on 64 pins,
  39,843 variables and 54,414 constraints after four rounds; the pairwise exclusions by family, of which the lazy mechanism
  wrote the first number: wire against wire end 3,601 of 9,292, wire against pin 1,339 of 1,842 (9,398 more implied), wire against
  body 1,673 of 3,552, rail reservation against wire 1,029 of 2,530, body against body 366 of 378 (12 implied), pin separation
  378, joined bodies 119 of 123. The wire-end and wire-pin families are three quarters of it.
- Correction to the hint measurement above: its "before" column came from a tree before the merged grammar changes, so
  piaa_power (39.4), pico_hbridge (0/0/3/0/2/271.8, 4 s), bms_supply and tally_timecode's overlap are the plans', not the
  hint's. Against the same tree without it the hint is: amp 0/0/0/0/0/274.6 to 0/0/0/0/2/264.4, uno_r4_power 1/0/3/0/7/497.3 to
  0/0/4/0/8/507.5, gamma_sipm 280.7 to 288.3, tally_power 82.5 to 83.8, bms_supply one crossing fewer, tally_timecode two
  clearances fewer: neutral to worse.
- Pruning by the rows' order (`_PRUNE`, off). The solver orders row heights, a part on a row stands at a constant offset from
  its row (a hung part from its wire's), and the least distance between two rows is the longest path over the row constraints.
  A pair of things is not written when the one's highest point is a unit above the other's lowest under those facts: the
  exclusion cannot bind. It is sound (every pruned exclusion tested against every placement kept: 0 violations in
  4,298, 930 and 746 tests on bitaxe_power, amp and uno_r4_power) but takes only a quarter of the pairs: bitaxe_power
  18,083 pairs not implied elsewhere, 13,785 after (wire end 9,292 to 6,913, wire against pin 1,842 to 1,083, wire against body
  3,552 to 2,932, rail reservation 2,530 to 1,992, joined bodies 123 to 121; bodies and pin separations untouched). The rest are pairs
  on the same or neighbouring rows and drops spanning several rows. Solved: bitaxe_power places but poorly (0/0/14/0/28/10307.1
  against the heuristic's 1544), amp goes from 0/0/0/0/0/274.6 to 0/0/0/0/5/652.8 (a worse wire-stage incumbent: the
  trajectory differs, which a sound pruning may change), uno_r4_power 1/0/3/0/7/497.3 to 0/0/3/0/6/505.4, gamma_sipm 283.2,
  tally_power 85.1, the rest equal; opamp proves its wire stage optimal at 13.8. Off. What remains is horizontal: within a
  row the order of the parts bounds how far a drop can be from a pin, which the plan fixes too but only for row members.
- Parallel workers (`_WORKERS`, 1; `interleave_search` on above 1, CP-SAT's deterministic mode). Not the same answer twice:
  with 6 workers gamma_preamp's R4 stood 2.54 mm apart in one of three runs (the deterministic times were 22.20, 20.42 and
  22.36); with 4, pmod_hv's two runs differed; with 2, three fixtures twice each (gamma_preamp, pmod_hv, bms_supply) were
  identical. And no better: `max_deterministic_time` is summed over the workers (the probe's 2.0 became 5.4 with 2 workers and
  9.4 with 6), so each searches less. amp: 0/0/0/0/0/326.6 with 2 workers, 362.2 with 6 (against 274.6); uno_r4_power
  0/0/2/0/11/774.2 and 0/0/5/0/8/2525.5 (against 1/0/3/0/7/497.3); bitaxe_power no placement with either; the wall time of
  small sheets doubled or tripled (bms_supply 22 s to 58 s and 88 s with 4 and 2 workers). One worker stays.
- NoOverlap2D prototypes (`_GLOBAL_BODY`, `_GLOBAL_WIRE`, both off). Bodies against bodies as one `AddNoOverlap2D` over every body
  grown by half the gap (the same condition as the pairwise one: touching is let, the gap is kept; the pairs the row order
  implies are in it too, redundantly). Each conductor against the bodies it keeps the full gap from (not the bodies it joins, and
  not the same-net ones that have a margin and an end exception, which stay pairwise) as one `AddNoOverlap2D` of the conductor
  grown by the gap, an optional interval present when it has length, and those bodies as they are; no two conductors share a
  constraint, for they may cross. A diagnosis keeps the pairwise form. The model written whole (nothing left out): amp 23,475
  variables and 31,859 constraints, with the bodies global 22,503 and 30,691, with the conductors too 15,954 and 21,756 (68
  constraints over 226 intervals); uno_r4_power 43,208 and 59,422, 41,864 and 57,797, 31,135 and 43,088 (93 over 292);
  bitaxe_power 80,985 and 107,964, 79,521 and 106,191, 63,465 and 84,141 (134 over 378). Answers (through / overlaps / clearance /
  short / crossings / wire mm, wall s): bodies global: amp 0/0/5/0/3/330.2 (93), uno_r4_power 0/0/3/0/8/532.9 (95), bitaxe_power
  1/0/19/0/33/1611.4 (126, against no placement, or 10307 with the pruning; the heuristic's 1544.3); with the conductors too, all
  worse: amp 0/0/2/0/9/2478.0, uno_r4_power 1/0/10/0/16/1909.8, bitaxe_power no placement (169 s). Bodies global on the other
  eleven: the same counts on seven, bms_supply one crossing fewer, opamp 175.0 against 172.5, gamma_sipm 283.2 against 280.7;
  but pico_hbridge takes 50 s against 4.6, piaa_power no longer proves its optimum (63 s against 38) and bms_supply takes 56 s
  against 22. The first bitaxe placement that is not junk, at the price of a slower search elsewhere.
- A seed portfolio (`legalise(..., seeds=)`, `_SEEDS`, default `(7,)`; the sandbox takes `KICAD_AI_LADDER_SEEDS=7,11,13,17,19`).
  With more than one seed the same model is solved once per seed, each in a process of its own (`ProcessPoolExecutor`,
  one worker per seed, one thread each, the whole budget each), and the placement that costs least as the checker draws the whole
  sheet (`ladder_check.counts` on the placed parts, the others in their row, the unplanned nets attached; the prices of the
  selection above) is returned, the earlier seed on a tie. The answer depends on the seed list and on nothing else, except through
  the 120 s wall guard, which a seed can trip (see bitaxe_power). Pricing the nets the solver drew alone picked a placement whose
  leftovers were far off (bitaxe_power, seed 13 against seed 7), hence the whole sheet. K = 1 / 3 / 5 over the 14 fixtures:
  amp 0/0/0/0/0/274.6 (seed 7) at every K, 97 / 98 / 106 s; uno_r4_power 1/0/3/0/7/497.3 to 0/0/3/0/3/495.8 (seed 11) at K = 3 and 5;
  bms_supply one crossing fewer (seed 11); gamma_preamp 2/0/0/0/0/179.1 to 1/0/0/0/0/179.1 (seed 13); opamp the same counts (seed 11);
  the other nine unchanged (seed 7); wall times 5 to 25% above one solve (five processes on six cores), pico_hbridge 5 s to 41 s
  (process start-up and the slowest seed). bitaxe_power is at the wall guard: K = 1 0/0/33/0/39/1847.6, K = 3 only seed 13 placed
  (0/0/12/0/28/10546.3), K = 5 seeds 7 and 13 placed and 7 won. K = 5 with the bodies global is worse: amp 0/0/0/0/1/358.1, uno_r4_power
  498.8 with 7 crossings, bitaxe_power no placement from any seed, pico_hbridge 122 s; only gamma_preamp (0 throughs, 189.7) is better.
- Known gap, model against checker: the one fault the solver does not see on amp (N_L2_1 through R7) is the checker's margin: a drop that does not carry a
  part's own pin keeps 0.5 mm from that part's body unless it runs over one of the part's tips, and the solver shortened the
  drop by the margin at both ends so that one landing on a host's tip is let. `_FULL_MARGIN` (off) keeps the whole drop out
  unless it runs along a tip: amp has 0 through but 2 crossings, gamma_preamp 0 through, and the model is slower
  (gamma_sipm and uno_r4_power run out of wall time), so it stays off.
- A stacked part may have several pins on the junction (a power FET's source pins), all facing the other part and at one
  height. The junction wire ends at the middle one by x; where the tips differ in x a short run at that height joins them
  (a second route in `Placements.wires`), and where they coincide there is none. A span between a junction wire and an
  ordinary wire (a bootstrap capacitor from a controller's lane to the bridge's output) is placed by its ordinary end; its
  junction pin, like the other parts' pins on the junction net that the plan does not land on the wire, is wired afterwards
  (`attach_unplanned`).
- Model size (2026-10-13). The pairwise exclusions between conductors, reservations and bodies or pins are the
  bulk of the model (about 95% of a 30-part plan's 53,000 variables and 74,000 constraints, in four families:
  wire against pin, rail reservation against wire, wire against body, wire end against wire). Three things keep the
  model small without weakening it. (a) An exclusion that another one implies is not written: two row parts in the
  plan's order (the order constraint is the same inequality) and a wire against a foreign pin tip that lies inside
  the gap its body already keeps from the wire. (b) The rest are written lazily. A first short solve of the model
  without them (at most 10% of the deterministic budget) puts the parts somewhere; an exclusion is written up front
  only if its two things stand within 30 mm in that placement. Every incumbent of the search is checked against the
  rest by the same inequalities (the check is the exclusion restated, over the solver's numbers); the first that
  breaks one stops the search, what it breaks is written in, and the search resumes from it. After 40 such rounds
  everything left out is written in. Only an incumbent that breaks nothing is kept, and a proved optimum of the
  smaller model is the optimum of the whole. The radius and the round limit change what is written when, never what is
  legal or optimal; infeasibility is proved on the model as written, which is a subset of the full one. A diagnosis
  rebuilds that subset with assumption literals. (c) Crossing terms enter the objective in the same way: for the
  segment pairs that stand near in the first placement, and later for any pair an incumbent crosses (a term added after
  an objective is fixed is held at zero). CP-SAT's probing presolve is off: it took three to five wall seconds a pass
  on the larger models and three passes a stage, for nothing the search does not find, and "not equal" between two
  expressions is written as the two inequalities, which propagate.
- The result is a `dict[str, Placement]` subclass exposing `.solver` and `.status`: `none` for empty plans,
  `cp-sat` when the weighted stage and the tie preference after it finish optimally, or `cp-sat (deterministic limit)` for a legal
  incumbent. The stages share 20 deterministic seconds and a 120 second wall safety limit (the deterministic clock runs
  at roughly a third to a fifth of wall time on the larger models), with one worker and seed 7, following
  `field_placement.py`'s clock/search conventions. The first solve (at most 10% of the budget) is the start-up
  placement of "Model size"; then wire and extent (at most 40%, hinted by that placement) leave a good placement for the
  weighted stage to improve, and the weighted stage takes what is left, with the tie preference after it when it
  finishes. Only when no incumbent comes does a plain feasibility search take the rest. The model has no
  assumption literals on that path. Only when it proves the plan infeasible is the model built a second time with one assumption
  literal per constraint group, to name the core; the result exposes `.timing` (build, feasibility, optimisation wall
  seconds) and `.deterministic_time`. A wall timeout or lack of a legal incumbent raises
  `LadderSolveError`, never `LadderInfeasible`. The latter exposes `.groups` and `.parts` from an assumption core
  (not necessarily minimal), or an immediate structural conflict. `legalise_with_status` adapts the mapping and
  status to the offline harness. No constraints are relaxed or placement retried.

**Findings from the model-size iteration (2026-10-13).**
- `pico_hbridge` is infeasible by the plan, not the solver (`drop direction`, proved in 0.2 s). `N_M1_1` joins `M1.1` (row
  5, sideways), `Q1.2` (row 4, faces down) and `Q3.3` (row 1, faces up); `N_M1_2` joins `M1.2`, `Q2.2` (row 3, faces
  down) and `Q4.3` (row 2, faces up). A trunk needs one height at or above `Q3.3` and at or below `Q1.2` while `Q1` stands
  below `Q3`: no such height. The grammar should turn one member of each pair (the upper part's pin to face down, or the
  lower part's to face up) or give the net a side row between the two rows; the solver reports it and does not route a U.
- The checker's `through` and `short` are counted on its own route estimate, which is not the solver's wire: for a net
  of three or more pins it takes the cheaper of a tree (the nearest pair, the rest attached at right angles to what is
  drawn) and a trunk with drops, and its trunk wants every vertical pin's drop at least 2.54 mm long and a sideways pin's
  lead exactly 2.54 mm. The solver's trunk may touch a vertical pin's tip (a drop of no length) and a sideways pin's lead
  may be longer, so the checker draws a tree where the solver drew a trunk, and its legs can cross a pin the solver's wire
  never touches (`amp`: `Q1.2` runs along its row to the x of `R5.2`, over the tip of `Q3.2`, where the modelled lead
  turns after 2.54 mm; or `C2.1`/`R5.2` at one height with `Q1.2` below, the checker's leg dropping over `C2.2`).
  Closing this means modelling the checker's route for these nets, not another exclusion.
- Search, not model size, limits quality on plans of 25 or more parts: the pairwise exclusions grow the model but the
  search (one worker, no neighbourhood search) is slow to improve an incumbent whichever size, and a first incumbent for
  `bitaxe_power` costs 100 to 120 wall seconds.

**Acceptance**: on the four demos (`sandbox.py ladder --run rec-1 ...`) through 0, crossings 0, short 0 and wire no
longer than the heuristic legaliser's; on the corpus harness (`sandbox.py ladder-corpus --circuit all`) at least as
many circuits placed and no count worse. When it passes, the slot search, stand-off loops and re-placement retries in
`ladder_layout.py` are deleted and `ladder_layout` becomes grammar -> plan -> legalise.

**Tools for the legaliser (2026-10-12).** `backend/schematic/ladder_plan.py` holds the plan classes (`Row`, `Hang`, `Span`,
`Column`, `Plan`, JSON round trip) and `plan_from_layout`, which records the heuristic placer's decisions as it makes them
(`ladder_layout(..., record=LayoutRecord())`; the return value is unchanged); one field is added to the sketch above,
`Plan.sequence` (each placed group's chain parts left to right across row steps), and a free column of supplies is a
`Column` with an empty `pin`. `tests/fixtures/ladder/<name>.json` holds one circuit each (the four demos and the ten
corpus circuits): the plan, every symbol's geometry in every orientation, the nets, the room in front of pins, the roles and
the heuristic's placement as the baseline; `backend/schematic/ladder_fixture.py::load(path)` returns `(plan, symbols, nets,
baseline)` with no sandbox or recording. `tally_timecode` is a fixture the heuristic cannot finish (it finds no slot for R21
on `LTC_RX`): its plan is complete, its baseline lacks the parts listed in `unplaced`. `python -m backend.schematic.ladder_check
FIXTURE PLACEMENT` prints the placement model's counts for a placement beside the baseline's and a legality verdict (the
placement format is in its docstring). `sandbox.py ladder-fixtures` rewrites the fixtures from a recorded run and the corpus.
In the harness, `KICAD_AI_LADDER_PLACEMENT=<file or folder>` takes positions from placement files instead of
`ladder_layout`, `KICAD_AI_LADDER_SOLVER=cpsat` from `ladder_solve.legalise_with_status` when that module exists.

## 5. WP3: offline harness (`scripts/ci/sandbox/probes/ladder/harness.py`)

**Done in-house (commit 00d45f8): no WP3 pull request is needed.** The harness is `python scripts/ci/sandbox/sandbox.py
ladder --run RUN --circuit lm317|buck|opamp|amp|all [--engine] [--pick shipped|last|N]` (module
`scripts/ci/sandbox/sbx/ladder.py`, selftests in `scripts/ci/sandbox/selftest/test_ladder.py`); `harness.py` is a thin
wrapper over it. It picks the recorded trial that reproduces the run's shipped sheet (distance 0.0 mm on all four demos),
runs `ladder_roles.classify` and `ladder_layout` when they are importable and the engine's placement otherwise, and
writes `out/<circuit>_engine.png`, `out/<circuit>_ladder.png`, `out/<circuit>_both.png` and `out/summary.md`. WP1 and
WP2 are judged with it. The paragraph below is the original brief, kept for the data it names.

`python harness.py --circuit lm317|amp|buck|opamp [--engine]`: loads the recorded inputs of that circuit from run
`auto-st3` (identify circuits by their references), the matching intent from `scripts/schematic_demo_circuits.py`,
pin counts from the recorded geometries; runs WP1 and WP2; keeps the engine's recorded positions for `other` parts;
prints the Roles summary and the placement-side counts (`placement_refinement._Model(...).cost(pose, found=...)`:
through, overlaps, clearance, bends, jogs, crossings, wire, extent) for the ladder placement and, with `--engine`, for
the engine's recorded placement; writes `out/<circuit>_ladder.png` and `out/<circuit>_engine.png`.

Optional, only if cheap: offline routing. The sandbox's libavoid replay lab (`scripts/ci/sandbox/sbx/replay.py`,
README section "replay") re-routes recorded problems through the Node bundle; if it can take a new problem (obstacle
boxes, pin portals, nets, built the way `backend/kicad/mcp_extended_server.py`'s grouped route builds them), add
`--route` to report routed corners per connection and crossings through
`backend/schematic/readability.measure_readability`. If that is more than a day, skip it and say so.

Acceptance: the harness runs end to end with `--engine` before WP1/WP2 exist (stand-in placement = the engine's) and
with them afterwards; through and overlaps are 0 for the ladder placement on the LM317 and the amplifier; the two PNGs
per circuit are attached to the PR for review.

## 6. WP4: integration (after WP1 and WP2 are reviewed)

**Decided 2026-10-12: the ladder output does not go through the trial loop or the MCP Pro server.** The earlier plan
(a `ladder` trial profile inside the scratch trial loop, since deleted, positions pushed through `sch_move_symbol`,
the quality gate choosing between it and the other trials) is withdrawn: a structural placer yields one layout, and the
server's per-call reload is a third of a build. The ladder gets onto a sheet through a direct path with one author:

1. Roles (WP1) and layout (WP2) on the intent and the symbol geometries read from the libraries.
2. The sheet is written in-process with `kicad-sch-api` (MIT; the library MCP Pro's own authoring services sit on): every
   symbol placed in one batch with its orientation, one document held in memory through the build, one save.
3. Routing through our own libavoid (`scripts/libavoid_route.mjs`, the router the engine already uses); power
   terminals, net names on wires, block captions and field placement run as they do today (they are functions over the
   scene, not server calls).
4. Verification by KiCad itself, as now: `kicad-cli` ERC and a netlist export compared with the intent
   (`backend/kicad/netlist.py`); render with `kicad-cli`.
5. A sandbox build mode `ladder` so `bench` tables and pictures come from the same tooling as the engine's.

No MCP server, no HTTP client, no guarded/verified profile split on this path. The existing MCP Pro path stays untouched
as the shipped product until the direct path reaches parity on the four demos; then the trial loop and the server can
go. Record `ladder-roles.json` and `ladder-layout.json` in the artifact root.

Verify with the sandbox once the mode exists: `python scripts/ci/sandbox/sandbox.py run --label ladder-1 --mode ladder
--jobs 4 --timeout 3600 --fixtures demo_buck,demo_lm317,demo_opamp,demo_ampac`, then `python
scripts/ci/sandbox/sandbox.py bench ladder-1 --against <previous run>` (bench.md and PNGs under the run's `bench/`).
The direct path logs per-stage timing (write, route, terminals and text, verify, render; wall time per circuit) in the same shape as the engine's `timing.json` (`KICAD_AI_TIMING=1`), and the bench table shows it beside the engine's run on the same machine (rec-1: buck 407 s, LM317 651 s, op-amp 1105 s, amplifier 2197 s with the trial loop).

Acceptance: all four demos build with ERC clean and the netlist matching the intent; timing is reported; corners per connection at or below
the previous run and no new crossings on the LM317 and the amplifier; the PNGs beside KiCad's drawings go to the user
for a blind verdict, which is the final acceptance.

## 7. Review checklist

- No circuit-specific logic; every rule is stated in the docstring in plain terms and has a test.
- Determinism: sorted iteration, no decisions from set order, shuffled-input tests present.
- Geometry: pin tips on row lines, bodies beyond rows, no overlaps; a failing assertion names the parts.
- Judged by the harness counts (through 0, overlaps 0) and the pictures; the greedy model's total is not the criterion.
- The direct build is unchanged for the circuits it already builds (run the four demos before and after and compare
  `mcu_smoke.kicad_sch` with UUIDs stripped and the children of the root and of every placed symbol sorted: the order
  KiCad's library serialises items and pins in differs from run to run even for the same input).

## 8. How to hand the work in

- One pull request per work package into `engine/straight-over-compact`, from branches `ladder/wp1-roles`,
  `ladder/wp2-layout`, `ladder/wp3-harness` and, after the first three are approved, `ladder/wp4-integration`, each
  based on the current tip of `engine/straight-over-compact`. Work in a separate clone or `git worktree` per branch;
  the reviewer's checkout runs sandbox jobs and must not be edited.
- The PR body carries the acceptance artifacts from the work package, so the review does not have to regenerate
  them: WP1 the Roles table for the four demos and every ambiguity resolved; WP2 the toy-ladder SVG; WP3 the
  engine-versus-ladder PNGs per circuit and the harness counts; WP4 the bench table and PNGs of `ladder-1`. Also the
  test counts (`python -m unittest discover -s tests/unit -p "test_*.py"` and `python scripts/ci/sandbox/sandbox.py
  selftest`). Tests are `unittest` modules under `tests/unit/` so CI runs them.
- Review: give the reviewer the PR number. The reviewer reads the diff (`gh pr diff`), checks the branch out into a
  worktree, runs the suite and the harness, and posts the review on the PR with findings against section 7; approve
  or request changes happens there and the author iterates on the same PR. WP2 and WP3 are reviewed with WP1 present,
  so when all three land together WP1 goes first.
- Merge order: WP1, WP2, WP3 into `engine/straight-over-compact` once approved; WP4 is written against that.

### WP5: the legaliser (outsourced against section 4b)

- Branch `ladder/wp5-legaliser`, one PR into `engine/straight-over-compact`. Deliverable: `backend/schematic/ladder_solve.py`
  with `legalise(plan, symbols, nets, *, gap_mm=2.54, pitch_mm=7.62) -> dict[str, Placement]` exactly as section 4b
  states (CP-SAT via `ortools`, one worker, a deterministic budget and a wall-clock safety net, a status string,
  `LadderInfeasible` naming the parts and the constraint group), plus `unittest` tests under `tests/unit/` on toy plans
  (a chain with hangs, a stacked pair, a rail-pin slot, an infeasible plan) and a determinism test (two solves compare).
- Inputs are the committed fixtures `tests/fixtures/ladder/<name>.json` (four demos, ten corpus circuits), loaded with
  `backend/schematic/ladder_fixture.py::load(path)`: no sandbox, recordings, corpus clones or KiCad are needed.
- Acceptance is measured, not described: `python -m backend.schematic.ladder_check tests/fixtures/ladder/<name>.json
  <placement.json>` prints the placement model's counts (through, overlaps, clearance, short, bends, crossings, wire,
  extent) for the solver's placement beside the fixture's heuristic baseline and a legality verdict. The PR body carries
  that output for all fourteen fixtures. Required: on the four demos through 0, crossings 0, short 0 and wire no longer
  than the baseline; on the corpus, at least as many fixtures legal as the baseline and no count worse on any; every
  infeasible fixture reported with its constraint and parts (that is a finding about the plan, not a failure).
- No circuit-specific logic, no tuning per fixture; constants are the two parameters. The reviewer runs the check
  command and the harness (`KICAD_AI_LADDER_PLACEMENT=<file>`), then builds the placements on real sheets.

