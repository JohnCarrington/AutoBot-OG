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
        """Return ``True`` if the *currently committed* regime is executable.

        A regime is live iff ``current_regime`` is anything other than
        ``TRANSITION``. An in-flight ``pending_regime`` does **not**
        suppress liveness — strategies should keep trading the committed
        regime until the pending one is confirmed by M5 and promoted.

        A fresh engine (current=TRANSITION, pending=None) is not live; an
        engine staging its very first transition (current=TRANSITION,
        pending=TREND awaiting M5) is also not live because nothing is
        committed yet.
        """
        return self.current_regime != RegimeLabel.TRANSITION

    def get_state(self) -> RegimeState:
        """Return a serialisable snapshot of the engine's current state."""
        return to_dict(self)

    # --- H1 event ------------------------------------------------------------

    def process_h1_close(
        self,
        h1_row: pd.Series,
        prev_h1_row: Optional[pd.Series] = None,
    ) -> None:
        """Consume a single H1 close and update internal state.

        The branch order matters and is contractual:

        1. NaN-indicator guard (C2): if the classifier reports
           ``insufficient_indicator_data``, the engine refuses to mutate
           any committed or pending state — a missing indicator value
           must never demote a real regime.
        2. VOLATILE state machine: stay-or-cooldown.
        3. Normal hysteresis, then a three-way disambiguation:
           - ``final == current``  → no transition; clear any pending,
             counter goes to 0 (nothing to confirm).
           - ``final == pending``  → re-emit of the same in-flight
             transition; refresh pending metadata, **counter preserved**
             (this is the C1 fix).
           - otherwise              → fresh transition staged; counter
             reset to 0. VOLATILE commits immediately.
        """
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

        # --- C2: NaN-indicator guard -----------------------------------------
        # A missing slope or BB-width on a single bar (gap, vendor outage,
        # warmup) must not demote the committed regime. Surface the no-op
        # via ``reason`` for diagnostics, then bail.
        if naive_reason == "insufficient_indicator_data":
            self.reason = naive_reason
            return

        # --- H1: slope sign-flip routes through VOLATILE ---------------------
        # A direct +/- flip on a committed TREND is more likely a whipsaw,
        # news shock or mis-printed bar than a clean regime change. Rather
        # than letting the bot fire a counter-trend signal on the very next
        # M5, demote the naive emission to VOLATILE — the existing
        # ``VOLATILE_EXIT_QUIET_H1_BARS`` cooldown then governs how (and
        # when) the new direction is eventually accepted via normal
        # hysteresis + M5 confirmation.
        if (
            self.current_regime == RegimeLabel.TREND
            and naive_label == RegimeLabel.TREND
            and self.current_direction is not None
            and naive_dir is not None
            and naive_dir != self.current_direction
        ):
            naive_label = RegimeLabel.VOLATILE
            naive_dir = None
            naive_reason = "slope_sign_flip"
            naive_conf = Confidence.LOW
            self.debug["naive_reason"] = naive_reason
            self.debug["naive_regime"] = naive_label.value
            self.debug["naive_direction"] = None

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
                # N1 fix: preserve any non-VOLATILE pending staged by a
                # prior fall-through. M5 must be allowed to confirm it
                # across multiple H1 windows — wiping it on every
                # cooldown bar made the recovery path effectively
                # impossible on interleaved data. Only wipe a VOLATILE
                # pending (defensive — VOLATILE pending normally
                # auto-commits inside process_h1_close, so this should
                # not arise in practice).
                if self.pending_regime == RegimeLabel.VOLATILE:
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

        matches_current = (
            final_label == self.current_regime
            and final_dir == self.current_direction
        )
        if matches_current:
            # No regime change — refresh confidence/reason and clear any
            # in-flight pending transition (the H1 has changed its mind).
            self.current_confidence = naive_conf
            self.reason = naive_reason
            self.pending_regime = None
            self.pending_direction = None
            self.m5_confirmation_count = 0
            return

        matches_pending = (
            self.pending_regime is not None
            and final_label == self.pending_regime
            and final_dir == self.pending_direction
        )
        if matches_pending:
            # C1 fix: same pending re-emitted by H1. Preserve the M5
            # confirmation counter — agreeing M5 bars accumulated under
            # the previous H1 print are still valid evidence.
            self.pending_confidence = naive_conf
            self.pending_reason = naive_reason
            self.reason = naive_reason
            return

        # Fresh transition: stage as pending, reset counter. VOLATILE
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

        Specific overrides applied in order:

        - Explicit ``VOLATILE`` naive emission always wins (this includes
          the H1 sign-flip case: the upstream override in
          ``process_h1_close`` converts a TREND→opposite-TREND naive into
          a VOLATILE one with reason ``"slope_sign_flip"`` before
          hysteresis sees it).
        - Sticky TREND bullish / bearish hold while ``|slope|`` is still
          beyond ``SLOPE_FLAT_BAND`` *in the same sign*.
        - **H3 fix**: the sticky RANGE branch defers to a TREND naive
          emission that carries direction — a structure-backed or
          strong-slope TREND breakout breaks the RANGE lock immediately
          even when ``bb_width`` is still inside the hysteresis band.
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

        # Sticky RANGE exit: only leave once width exceeds the upper band,
        # except that a structure-backed or strong-slope TREND breakout
        # (naive == TREND with a non-None direction) immediately wins —
        # the classifier already gated those signals through structure
        # priority or |slope| > SLOPE_TREND_ENTRY, so the BB-width sticky
        # check should not override them (H3).
        if self.current_regime == RegimeLabel.RANGE:
            structure_or_strong_trend_breakout = (
                naive_label == RegimeLabel.TREND and naive_dir is not None
            )
            if (
                not structure_or_strong_trend_breakout
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

        # H5: refuse anything that isn't TREND or RANGE (VOLATILE is
        # short-circuited above and TRANSITION/anything-else must NOT
        # auto-confirm through the no-op gate).
        return False

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
