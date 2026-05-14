"""Price-structure detection: 5-bar fractal swings and structure-state queries.

A 5-bar fractal swing high at index ``i`` is a bar whose high is strictly
greater than the highs of its two left and two right neighbours. The mirror
condition on ``low`` defines a swing low. Because the test peeks at bars
``i + 1`` and ``i + 2``, a swing at ``i`` carries an inherent 2-bar
confirmation lag — the labels produced here are *static* (computed over the
whole DataFrame); callers in a live-bar setting must enforce the lag.

Public API:
    add_fractal_swings(df)
        Append swing/marker/state columns to a copy of an OHLC DataFrame.
    get_structure_state(df, lookback_bars=10)
        Summarise the most recent swing state at the tail of a DataFrame
        that has already been processed by ``add_fractal_swings``.
"""
from .fractals import add_fractal_swings
from .state import get_structure_state

__all__ = ["add_fractal_swings", "get_structure_state"]
