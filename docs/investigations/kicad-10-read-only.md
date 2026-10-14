# KiCad 10 / MCP Pro investigation

Date: 2026-10-03

## Upstream surface reviewed

The current canonical MCP Pro repository is `oaslananka/kicad-mcp-pro`. Its
published documentation describes stdio and Streamable HTTP transports and a
bounded default profile containing read-only review tools. Its server metadata
advertises live schematic/PCB read capabilities and says KiCad CLI 10 is needed
for file-backed DRC, ERC, and exports.

## Local verification

This development environment does not have `kicad`, `kicad-cli`, Cargo, or
MCP Pro installed. Therefore no local schematic/PCB read or KiCad 10 E2E claim
is made. `backend/kicad/capabilities.py` captures this as unverified rather
than advertising support.

## Implementation decision

Use a replaceable `KiCadService` seam and a read-only MCP Pro adapter. The
adapter's request method is intentionally isolated so actual MCP tool names and
payloads can be bound after a compatible local MCP Pro server is installed and
interrogated. File discovery and hashing work independently of KiCad.

