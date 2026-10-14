from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .models import ProjectMetadata


class KiCadService(ABC):
    """Domain boundary for all KiCad reads in this application."""

    @abstractmethod
    def inspect_project(self, root: Path) -> ProjectMetadata:
        raise NotImplementedError

    @abstractmethod
    def read_project(self, root: Path) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def capability_registry(self) -> list[dict[str, object]]:
        raise NotImplementedError
