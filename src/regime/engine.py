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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

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

# Bounded emission log: ~2000 events covers ~64 hours at H1+M5 cadence, which
# is plenty for the risk layer's 60-minute lookback queries (Phase 4).
EMISSION_LOG_MAXLEN = 2000


@dataclass(frozen=True)
class RegimeEmission:
    """Snapshot of engine state recorded at each ``process_*_close`` call.

    Captured at the *end* of the public method (i.e. *after* any state
    mutation), so consumers see the engine's view of the world that takes
    effect from this bar onward.

    Fields
    ------
    timestamp : Any
        The triggering bar's index (typically a ``pd.Timestamp``). Stored
        verbatim so test fixtures with integer indices still produce
        readable emissions; time-window filtering in
        :py:meth:`RegimeEngine.get_recent_emissions` only matches entries
        whose timestamp is a ``datetime``.
    kind : "H1" or "M5"
        Which public method produced this emission.
    regime, direction, is_live, reason
        Engine state values at the moment the emission was logged.
    committed : bool
        True iff ``current_regime`` *changed* during this event. Covers
        both H1-driven commits (VOLATILE auto-commit on stage; matches_
        current/pending transitions) and M5-driven commits (third
        consecutive agreeing M5 close promoting pending → current). The
        risk layer's regime-instability counter sums this flag across a
        60-minute window.
    was_m5_reset : bool
        Only meaningful for ``kind == "M5"``. True iff the M5
        confirmation counter went from ``>0`` to ``0`` on this close
        *and* the regime did NOT commit (i.e. an agreeing-streak was
        broken by a disagreeing M5, not promoted to current). The
        third-M5 promotion case also drops the counter to 0 but is
        recorded as ``committed=True, was_m5_reset=False`` — the risk
        layer's instability counter must count legitimate commits and
        actual resets separately. See review C1 (2026-05-14).
    """

    timestamp: Any
    kind: Literal["H1", "M5"]
    regime: RegimeLabel
    direction: Optional[Direction]
    is_live: bool
    reason: str
    committed: bool
    was_m5_reset: bool


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

        # Emission log (Phase 4): bounded deque of state snapshots, one per
        # ``process_h1_close`` / ``process_m5_close`` call. Consumed by the
        # risk layer via ``get_recent_emissions`` and
        # ``regime_live_at_last_h1_close``.
        self._emission_log: deque[RegimeEmission] = deque(
            maxlen=EMISSION_LOG_MAXLEN
        )
        # Snapshot of ``is_live()`` captured at the most recent H1 close,
        # used by the risk layer's regime-instability cooldown extension.
        self._last_h1_is_live: bool = False
        # Snapshot of ``current_regime`` at the most recent H1 close
        # (H1 fix). The instability cooldown extension wants "regime
        # held live AND non-VOLATILE for a full H1 close" — VOLATILE is
        # by definition the *unstable* state, so the extension must not
        # clear merely because VOLATILE is committed.
        self._last_h1_regime: RegimeLabel = RegimeLabel.TRANSITION

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

    def get_recent_emissions(
        self, window_minutes: int, now_utc: datetime
    ) -> list[RegimeEmission]:
        """Return logged emissions within ``window_minutes`` of ``now_utc``.

        Filters the internal emission log by timestamp. Emissions whose
        ``timestamp`` is not a ``datetime`` (test fixtures using integer
        indices) are skipped — production callers always pass real
        datetimes through ``apply_regime_to_candles``.

        Used by the risk layer's circuit-breaker rule to count regime
        commits and M5 resets in the trailing hour.

        Parameters
        ----------
        window_minutes : int
            Trailing window size in minutes (typically 60 for the v1
            instability check). Must be ``>= 1``.
        now_utc : datetime
            "Now" for the query. Naive datetimes are accepted but the
            caller is responsible for ensuring timezone consistency.

        Returns
        -------
        list[RegimeEmission]
            Chronologically ordered emissions (oldest first) whose
            ``timestamp`` is a ``datetime`` and within window.
        """
        if window_minutes < 1:
            raise ValueError(
                f"window_minutes must be >= 1, got {window_minutes}"
            )
        cutoff = now_utc - timedelta(minutes=window_minutes)
        out: list[RegimeEmission] = []
        for emission in self._emission_log:
            ts = emission.timestamp
            if not isinstance(ts, datetime):
                continue
            # Compare using consistent tz-awareness: if cutoff is tz-aware
            # and ts is naive (or vice-versa) the comparison would raise.
            # Skip mismatched entries defensively rather than blowing up.
            try:
                in_window = ts >= cutoff
            except TypeError:
                continue
            if in_window:
                out.append(emission)
        return out

    def regime_live_at_last_h1_close(self) -> bool:
        """Return True iff the last H1 close left a stable, live regime.

        Used by the risk layer's regime-instability cooldown extension:
        after the primary 1-hour cooldown elapses, entries remain
        blocked until at least one H1 close has produced ``is_live=True``
        AND the committed regime is non-VOLATILE.

        The dual condition matters because:

        - **H1 fix (review 2026-05-14):** VOLATILE is "live" under
          :py:meth:`is_live` (sweep strategies can act on it) — but the
          instability cooldown exists precisely because the regime
          was unstable, and VOLATILE is the textbook unstable state.
          Allowing VOLATILE to clear the extension means the cooldown
          ends mid-volatility, exactly what the breaker is supposed to
          prevent. The helper therefore requires
          ``_last_h1_regime in (TREND, RANGE)``.
        - **H2 (intentional, docs-only):** the snapshot is taken only
          inside :py:meth:`process_h1_close`. An M5-driven commit
          between two H1 closes is **not** reflected here — the spec
          (§6.9.3) reads "regime held live for a full H1 close", so
          waiting for the next H1 print is correct. The
          ``test_regime_live_at_last_h1_close_not_updated_by_m5_only``
          test pins this; an interim opportunity cost of up to one H1
          window is accepted by design.

        The snapshot is set to ``False`` on construction; it becomes
        meaningful after the first call to :py:meth:`process_h1_close`.
        """
        return self._last_h1_is_live and self._last_h1_regime in (
            RegimeLabel.TREND,
            RegimeLabel.RANGE,
        )

    # --- H1 event ------------------------------------------------------------

    def process_h1_close(
        self,
        h1_row: pd.Series,
        prev_h1_row: Optional[pd.Series] = None,
    ) -> None:
        """Consume a single H1 close and update internal state.

        Public wrapper that snapshots state, delegates to
        :py:meth:`_process_h1_close_inner` for the actual classification
        + state-machine work, then appends a :py:class:`RegimeEmission`
        to the emission log so the risk layer can introspect transitions
        and instability after the fact. Existing tests do not need to
        know the log exists — it is purely additive state.
        """
        pre_regime = self.current_regime
        timestamp = h1_row.name
        self._process_h1_close_inner(h1_row, prev_h1_row)
        committed = self.current_regime != pre_regime
        self._emission_log.append(
            RegimeEmission(
                timestamp=timestamp,
                kind="H1",
                regime=self.current_regime,
                direction=self.current_direction,
                is_live=self.is_live(),
                reason=self.reason,
                committed=committed,
                was_m5_reset=False,
            )
        )
        self._last_h1_is_live = self.is_live()
        self._last_h1_regime = self.current_regime

    def _process_h1_close_inner(
        self,
        h1_row: pd.Series,
        prev_h1_row: Optional[pd.Series] = None,
    ) -> None:
        """Classify + apply the H1 state machine. See ``process_h1_close``.

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
                # M3 fix: ``current_direction`` always reflects the
                # current bar's naive emission, even when ``None``. The
                # previous conditional preserved stale bias — e.g. a
                # ``volatility_expansion`` bar (carries BULLISH) followed
                # by a ``structure_conflict`` bar (carries None) used to
                # leave ``current_direction = BULLISH`` for the rest of
                # the VOLATILE state. Direction-less VOLATILE is valid.
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
        """Consume a single M5 close and advance the confirmation counter.

        Public wrapper that snapshots counter + regime, delegates to
        :py:meth:`_process_m5_close_inner`, then appends a
        :py:class:`RegimeEmission` capturing whether the counter just
        reset (``was_m5_reset``) and whether the regime committed via
        this close. Additive logging; the inner method is unchanged.
        """
        pre_count = self.m5_confirmation_count
        pre_regime = self.current_regime
        timestamp = m5_row.name
        self._process_m5_close_inner(m5_row)
        committed = self.current_regime != pre_regime
        # C1 fix: ``was_m5_reset`` means "a disagreeing M5 closed against
        # an in-flight pending" — counter dropped from >0 to 0 *without*
        # promoting to current. A successful commit also drops the
        # counter to 0 (via _commit_pending) but is not a reset; the risk
        # layer's instability counter would otherwise sum legitimate
        # commits into the M5-reset bucket and falsely trip the breaker.
        was_m5_reset = (
            pre_count > 0
            and self.m5_confirmation_count == 0
            and not committed
        )
        self._emission_log.append(
            RegimeEmission(
                timestamp=timestamp,
                kind="M5",
                regime=self.current_regime,
                direction=self.current_direction,
                is_live=self.is_live(),
                reason=self.reason,
                committed=committed,
                was_m5_reset=was_m5_reset,
            )
        )

    def _process_m5_close_inner(self, m5_row: pd.Series) -> None:
        """Advance the M5 confirmation counter. See ``process_m5_close``."""
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
