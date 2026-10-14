# MCP Pro 3.37.0 live read verification

Date: 2026-10-03

## Environment verified

- KiCad MCP Pro 3.37.0, Streamable HTTP at `http://127.0.0.1:3334/mcp`
- KiCad CLI 10.0.4
- Project: `maulSaber-vside`
- PCB open in KiCad for live IPC reads

## Protocol observation

MCP initialization succeeded using protocol `2025-11-25`. The default profile
published the project, PCB read, schematic, library, validation, and DFM
categories. The server instructions identify this project setup workflow:

1. `kicad_get_version`
2. `kicad_set_project`
3. `project_get_next_action`

## Project-context requirement

`kicad_set_project` rejected relative `pcb_file` and `sch_file` values after
resolving them against an existing workspace root. Explicit absolute file paths
were accepted. Our adapter must therefore translate its project-relative KiCad
references to absolute paths only at the MCP boundary; persistent app data
remains project-relative.

## Verified reads

`pcb_get_board_summary` succeeded with live KiCad IPC:

- source: `live-gui`
- 4 footprints, 4 nets, 6 tracks, 4 vias, 1 zone

`sch_get_symbols` succeeded from the selected schematic and returned four
symbols: S1, S2, J1, and `#PWR01`, including positions and footprints.

## Read-only boundary

No design-file mutation, ERC, DRC, export, or write-tier tool was invoked.
`kicad_set_project` changed only MCP Pro's active project context.
