"""Domain primitives shared across layers (strategies, risk, execution).

Lives below all of those so importing it never creates an upside-down
dependency. Only enums / tiny dataclasses with no upstream imports
belong here.
"""
from .direction import Direction

__all__ = ["Direction"]
