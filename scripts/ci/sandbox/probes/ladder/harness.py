"""The ladder placer beside the engine's, offline: same as ``sandbox.py ladder`` (see docs/specs/ladder-placer.md, WP3)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import sandbox  # noqa: E402

if __name__ == "__main__":
    sys.exit(sandbox.main(["ladder", "--run", "auto-st3", *sys.argv[1:]]))
