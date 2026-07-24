"""Live facade context for extracted application services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AppContext:
    """Resolve app-level services from the compatibility facade at call time."""

    def __init__(self, values: Mapping[str, Any]):
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
