You are picking up the KiCad schematic engine in C:\Users\Trey\Documents\schematic-engine (GitHub
BigBadLoser/schematic-engine, branch main). Start by reading docs/HANDOFF.md, then sections 4b and 4c of
docs/specs/ladder-placer.md. The handoff lists the owner's working rules; follow them exactly: crossings are priced,
never banned; no rule may name a circuit or part; every change comes with the 14-fixture table, a picture for sheet
changes and a plain statement of what got worse; the four demos must not get worse.

Use the sandbox Python for everything: scripts\ci\sandbox\.work\venv\Scripts\python.exe with PYTHONPATH=. from the
repository root. Run the unit suite first to confirm the tree is green.

Work as an orchestrator: give implementation items to agents working in git worktrees under .worktrees/, review their
tables and pictures, merge to main yourself and push (you have standing permission for this repository). Resume an
existing agent with a new item rather than starting a fresh one where you can.

Begin with open item 1 in the handoff (field-text overlaps: the proposed continuation rule for the field solve, tested
by rebuilding sheets in the sandbox). Show me the before/after overlap counts and pictures before merging.
