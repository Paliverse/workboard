"""Version reporting, update checks, and channel-aware upgrades."""
from __future__ import annotations


def register(add) -> None:
    """Register this module's subcommands; implemented by the setup slice."""
