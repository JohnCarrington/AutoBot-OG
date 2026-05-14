"""Regime classification: hierarchical H1 classifier + M5 validation gate.

This module is the *composition layer*: it consumes indicator columns
(from ``src.indicators``) and structure columns (from ``src.structure``)
and emits a regime label per H1 row, with a state machine that holds the
emitted label in ``pending_regime`` until three consecutive agreeing M5
closes promote it to ``current_regime``.

Hierarchical priority (highest first):
    Structure > EMA slope > BB width > MACD

Public surface:
    RegimeLabel, Direction, Confidence   — enums (regime/labels)
    classify_h1                           — pure-function H1 classifier
    add_structural_pattern_column         — H1 preprocessor (compound HH/HL/LH/LL)
    RegimeEngine                          — stateful state machine
    RegimeState                           — TypedDict snapshot of engine state
    apply_regime_to_candles               — batch H1+M5 replay annotator
"""
from .applier import apply_regime_to_candles
from .classifier import (
    add_structural_pattern_column,
    classify_h1,
    compute_structural_pattern,
)
from .engine import RegimeEngine
from .labels import Confidence, Direction, RegimeLabel
from .state import RegimeState, to_dict

__all__ = [
    "Confidence",
    "Direction",
    "RegimeEngine",
    "RegimeLabel",
    "RegimeState",
    "add_structural_pattern_column",
    "apply_regime_to_candles",
    "classify_h1",
    "compute_structural_pattern",
    "to_dict",
]
