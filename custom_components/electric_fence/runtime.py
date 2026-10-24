"""Runtime data for Electric Fence."""

from dataclasses import dataclass

from .settings import Settings


@dataclass
class Runtime:
    """Per-entry runtime state (extended in later tasks)."""

    settings: Settings
