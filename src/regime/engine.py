"""Stateful regime engine: H1 hysteresis + M5 validation.

The engine wraps the pure ``classify_h1`` function with three pieces of
state-machine logic:

1. **Asymmetric hysteresis on slope and BB-width thresholds.** The
   classifier uses *entry* thresholds (e.g. ``|slope| > 0.35`` to enter
   TREND); the engine refuses to *leave* TREND while ``|slope|`` is still
   above the looser *exit* threshold (``0.15``). The same pattern applies
   to RANGE (entry width ``< 1.8``, exit width ``> 2.5``).

2. **M5 validation gate.** A regime emitted by H1 is held in
   ``pending_regime`` and does not become "live" until three consecutive
   M5 closes agree with it. Any disagreeing M5 close resets the counter.

3. **VOLATILE state machine.** VOLATILE bypasses the M5 gate (volatility
   is by definition unstable; sweep strategies act on opportunity, not on
   confirmed regime). Exiting VOLATILE requires three consecutive H1
   closes whose naive classification is *not* VOLATILE; the engine then
   re-applies hysteresis from VOLATILE's standpoint to pick the next
   regime.

M5 validation rules (looser than H1; sign-only for slope):

- TREND bullish: ``ema_slope_norm_50_10 > 0`` AND ``close > ema_50``.
- TREND bearish: ``ema_slope_norm_50_10 < 0`` AND ``close < ema_50``.
- RANGE: ``bb_lower_20_2 <= close <= bb_upper_20_2`` AND
  ``bb_width_norm_20_2 < 2.5``.
- VOLATILE: no gate — see point 3 above.

All hysteresis and confirmation thresholds live as class-level constants so
they can be tuned without touching the algorithm.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import pandas as pd

from .classifier import (
    BB_WIDTH_RANGE_EXIT,
    SLOPE_FLAT_BAND,
    classify_h1,
)
from .labels import Confidence, Direction, RegimeLabel
from .state import RegimeState, to_dict

# --- Tunables -----------------------------------------------------------------

# Number of agreeing M5 closes required to confirm a pending non-VOLATILE
# regime.
M5_CONFIRMATIONS_REQUIRED = 3

# Number of consecutive non-VOLATILE H1 classifications required before the
# engine will *leave* VOLATILE.
VOLATILE_EXIT_QUIET_H1_BARS = 3


class RegimeEngine:
    """In-memory regime state machine. Single-instance, single-pair."""

    # --- Construction --------------------------------------------------------

    def __init__(self) -> None:
        self.current_regime: RegimeLabel = RegimeLabel.TRANSITION
        self.current_direction: Optional[Direction] = None
        self.current_confidence: Confidence = Confidence.LOW

        self.pending_regime: Optional[RegimeLabel] = None
        self.pending_direction: Optional[Direction] = None
        self.pending_confidence: Confidence = Confidence.LOW
        self.pending_reason: str = ""

        self.m5_confirmation_count: int = 0
        self.last_regime_change_time: Any = None
        self.reason: str = "initial"
        self.debug: dict[str, Any] = {}

        # VOLATILE exit counter: consecutive non-VOLATILE naive H1 closes
        # observed while the committed regime is VOLATILE.
        self._volatile_quiet_count: int = 0

    # --- Public query API ----------------------------------------------------

    def is_live(self) -> bool:
        """Return ``True`` if the current regime is executable.

        A regime is live when either:
        - it is ``VOLATILE`` (no M5 gate by design), or
        - there is no pending transition awaiting M5 confirmation.

        A fresh engine (current=TRANSITION, no pending) is *not* live; the
        regime engine has nothing meaningful to emit until at least one H1
        close has been processed.
        """
        if self.current_regime == RegimeLabel.TRANSITION and self.pending_regime is None:
            return False
        if self.current_regime == RegimeLabel.VOLATILE:
            return True
        return self.pending_regime is None

    def get_state(self) -> RegimeState:
        """Return a serialisable snapshot of the engine's current state."""
        return to_dict(self)

    # --- H1 event ------------------------------------------------------------

    def process_h1_close(
        self,
        h1_row: pd.Series,
        prev_h1_row: Optional[pd.Series] = None,
    ) -> None:
        """Consume a single H1 close and update internal state."""
        naive_label, naive_dir, naive_conf, naive_reason = classify_h1(
            h1_row, prev_h1_row
        )

        slope = _safe_float(h1_row.get("ema_slope_norm_50_10"))
        bb_width = _safe_float(h1_row.get("bb_width_norm_20_2"))
        macd_hist = _safe_float(h1_row.get("macd_hist_12_26_9"))
        self.debug = {
            "slope_norm": slope,
            "bb_width_norm": bb_width,
            "macd_hist": macd_hist,
            "naive_regime": naive_label.value,
            "naive_direction": naive_dir.value if naive_dir is not None else None,
            "naive_reason": naive_reason,
            "structural_pattern": str(
                h1_row.get("structural_pattern", "INSUFFICIENT_DATA")
            ),
        }

        timestamp = h1_row.name

        # --- VOLATILE state-machine override ---------------------------------
        # While VOLATILE is the committed regime, we don't transition out
        # until the naive classification has been non-VOLATILE for
        # VOLATILE_EXIT_QUIET_H1_BARS bars in a row. This lets sweep
        # strategies trade through the volatility rather than getting
        # whipsawed by an immediate downgrade.
        if self.current_regime == RegimeLabel.VOLATILE:
            if naive_label == RegimeLabel.VOLATILE:
                self._volatile_quiet_count = 0
                # Stay VOLATILE; refresh confidence/reason for the new bar.
                self.current_confidence = naive_conf
                self.reason = naive_reason
                # Re-inherit direction if naive offered one (e.g. a new
                # volatility-expansion bar inside a trending bias).
                if naive_dir is not None:
                    self.current_direction = naive_dir
                self.pending_regime = None
                self.pending_direction = None
                self.m5_confirmation_count = 0
                return
            self._volatile_quiet_count += 1
            if self._volatile_quiet_count < VOLATILE_EXIT_QUIET_H1_BARS:
                # Still cooling down; hold VOLATILE.
                self.reason = "volatile_cooldown"
                self.pending_regime = None
                self.pending_direction = None
                self.m5_confirmation_count = 0
                return
            # Quiet period satisfied — fall through to normal hysteresis,
            # which will treat the next regime as a fresh entry.
            self._volatile_quiet_count = 0

        # --- Normal hysteresis ----------------------------------------------
        final_label, final_dir = self._apply_hysteresis(
            naive_label, naive_dir, slope, bb_width
        )

        same = (
            final_label == self.current_regime
            and final_dir == self.current_direction
        )
        if same:
            # No regime change — just refresh confidence/reason and clear
            # any in-flight pending transition.
            self.current_confidence = naive_conf
            self.reason = naive_reason
            self.pending_regime = None
            self.pending_direction = None
            self.m5_confirmation_count = 0
            return

        # Regime change: stage as pending, await M5 confirmation. VOLATILE
        # commits immediately (no M5 gate).
        self.pending_regime = final_label
        self.pending_direction = final_dir
        self.pending_confidence = naive_conf
        self.pending_reason = naive_reason
        self.m5_confirmation_count = 0
        self.reason = naive_reason

        if final_label == RegimeLabel.VOLATILE:
            self._commit_pending(timestamp)

    # --- M5 event ------------------------------------------------------------

    def process_m5_close(self, m5_row: pd.Series) -> None:
        """Consume a single M5 close and advance the confirmation counter."""
        if self.pending_regime is None:
            return  # nothing to validate

        # VOLATILE has no M5 gate — but pending VOLATILE is committed at
        # the moment of process_h1_close, so this branch should not fire
        # in practice. Guarded for defensiveness.
        if self.pending_regime == RegimeLabel.VOLATILE:
            self._commit_pending(m5_row.name)
            return

        if self._m5_validates(m5_row, self.pending_regime, self.pending_direction):
            self.m5_confirmation_count += 1
            if self.m5_confirmation_count >= M5_CONFIRMATIONS_REQUIRED:
                self._commit_pending(m5_row.name)
        else:
            self.m5_confirmation_count = 0

    # --- Internals -----------------------------------------------------------

    def _apply_hysteresis(
        self,
        naive_label: RegimeLabel,
        naive_dir: Optional[Direction],
        slope: float,
        bb_width: float,
    ) -> tuple[RegimeLabel, Optional[Direction]]:
        """Override the naive classification with sticky exit thresholds.

        Only delays *exits* — entry is decided entirely by the classifier's
        entry thresholds. Structure-driven and VOLATILE transitions are
        not subject to hysteresis: explicit events take priority over
        threshold smoothing.
        """
        # Explicit VOLATILE entry always wins (volatility is a real event,
        # not a borderline reading).
        if naive_label == RegimeLabel.VOLATILE:
            return naive_label, naive_dir

        # If naive matches current, no hysteresis needed.
        if (
            naive_label == self.current_regime
            and naive_dir == self.current_direction
        ):
            return naive_label, naive_dir

        # Sticky TREND bullish exit.
        if (
            self.current_regime == RegimeLabel.TREND
            and self.current_direction == Direction.BULLISH
            and not math.isnan(slope)
            and slope > SLOPE_FLAT_BAND
        ):
            return RegimeLabel.TREND, Direction.BULLISH

        # Sticky TREND bearish exit.
        if (
            self.current_regime == RegimeLabel.TREND
            and self.current_direction == Direction.BEARISH
            and not math.isnan(slope)
            and slope < -SLOPE_FLAT_BAND
        ):
            return RegimeLabel.TREND, Direction.BEARISH

        # Sticky RANGE exit: only leave once width exceeds the upper band.
        if (
            self.current_regime == RegimeLabel.RANGE
            and not math.isnan(bb_width)
            and bb_width <= BB_WIDTH_RANGE_EXIT
        ):
            return RegimeLabel.RANGE, None

        return naive_label, naive_dir

    def _m5_validates(
        self,
        m5_row: pd.Series,
        pending_label: RegimeLabel,
        pending_dir: Optional[Direction],
    ) -> bool:
        """Return ``True`` if this M5 close agrees with the pending regime."""
        if pending_label == RegimeLabel.TREND:
            slope = _safe_float(m5_row.get("ema_slope_norm_50_10"))
            ema = _safe_float(m5_row.get("ema_50"))
            close = _safe_float(m5_row.get("close"))
            if math.isnan(slope) or math.isnan(ema) or math.isnan(close):
                return False
            if pending_dir == Direction.BULLISH:
                return slope > 0 and close > ema
            if pending_dir == Direction.BEARISH:
                return slope < 0 and close < ema
            return False

        if pending_label == RegimeLabel.RANGE:
            close = _safe_float(m5_row.get("close"))
            upper = _safe_float(m5_row.get("bb_upper_20_2"))
            lower = _safe_float(m5_row.get("bb_lower_20_2"))
            width = _safe_float(m5_row.get("bb_width_norm_20_2"))
            if any(math.isnan(v) for v in (close, upper, lower, width)):
                return False
            return lower <= close <= upper and width < BB_WIDTH_RANGE_EXIT

        # TRANSITION isn't a regime callers should ever validate into,
        # but treat any M5 close as agreement (no-op).
        return True

    def _commit_pending(self, timestamp: Any) -> None:
        """Promote ``pending_*`` to ``current_*`` and clear the pending slot."""
        if self.pending_regime is None:
            return
        self.current_regime = self.pending_regime
        self.current_direction = self.pending_direction
        self.current_confidence = self.pending_confidence
        self.reason = self.pending_reason or "committed"
        self.last_regime_change_time = timestamp
        self.pending_regime = None
        self.pending_direction = None
        self.m5_confirmation_count = 0
        if self.current_regime == RegimeLabel.VOLATILE:
            self._volatile_quiet_count = 0


# --- Helpers ----------------------------------------------------------------


def _safe_float(value: Any) -> float:
    """Convert a Series cell to ``float``, returning NaN for None / non-numeric."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")
