"""Strategy layer: day-type-gated pattern detectors.

Each strategy is a stateless function — given the last ~3 M5 bars,
the latest H1 bar, and the day_type / structure_state, it returns
``Optional[Signal]``. A non-``None`` return signals that a setup
completed at the most recent M5 close; ``None`` means no setup.

Use :py:func:`detect_all_setups` as the integration point: it
consults the day-type dispatch table and calls each mapped detector.

Public surface
--------------
- :py:class:`Signal`, :py:data:`StrategyName`
- :py:func:`detect_bb_bounce`, :py:func:`detect_ema_pullback`
- :py:func:`detect_all_setups`
- Session predicates: :py:func:`london_session`, :py:func:`ny_session`,
  :py:func:`london_ny_overlap`

See ``docs/v1_architecture.md`` §5 for the locked spec and §5.5 for
module structure / spec-ambiguity resolutions.
"""
from .bb_bounce import detect_bb_bounce
from .dispatcher import detect_all_setups
from .ema_pullback import detect_ema_pullback
from .sessions import london_ny_overlap, london_session, ny_session
from .signal import Signal, StrategyName, compute_invalid_after

__all__ = [
    "Signal",
    "StrategyName",
    "compute_invalid_after",
    "detect_all_setups",
    "detect_bb_bounce",
    "detect_ema_pullback",
    "london_ny_overlap",
    "london_session",
    "ny_session",
]
