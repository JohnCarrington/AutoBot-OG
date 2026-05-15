"""Executor — translate approved :py:class:`Signal` → broker open + SL amend.

The class composes :py:class:`PositionManager` and :py:class:`IGClient`:

- :py:meth:`open_from_signal` — submit a market order for the signal,
  parse the confirmation, register an :py:class:`ExecutionPosition`
  in the manager, return a :py:class:`TradeResult`.
- :py:meth:`apply_amend` — translate an :py:class:`AmendOrder`
  (from :py:func:`execution.sl_management.evaluate_sl_amend`) into an
  IG :py:class:`AmendRequest`, parse the confirmation, update the
  managed position.

Idempotency: :py:meth:`open_from_signal` short-circuits if the
manager already has an open position with the same
``(pair, strategy_name, source_candle_ts)`` triple — returns the
existing position wrapped in a success :py:class:`TradeResult`.

The executor does not retry; the caller (Phase 7 bot loop) decides
whether a failure is fatal or recoverable.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from alerts import Alert, AlertCategory, AlertSeverity, TelegramAlerter
from feed.ig_rest import (
    AllowanceExceeded,
    AmendRequest,
    CloseRequest,
    DealConfirmation,
    IGClient,
    OrderRequest,
)
from regime.labels import Direction
from strategies.signal import Signal

from .constants import (
    EXECUTION_AMEND_RETRY_COUNT,
    EXECUTION_AMEND_RETRY_DELAY_S,
    EXECUTION_DEFAULT_SIZE_UNITS,
)
from .position_manager import PositionManager
from .types import (
    AmendOrder,
    AmendResult,
    ExecutionPosition,
    TradeOrder,
    TradeResult,
)


logger = logging.getLogger(__name__)


class Executor:
    """Broker-side trade open + SL amend orchestrator."""

    def __init__(
        self,
        *,
        position_manager: PositionManager,
        client: IGClient,
        epic_resolver: Callable[[str], str],
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        alerter: Optional[TelegramAlerter] = None,
    ) -> None:
        """Construct.

        Parameters
        ----------
        position_manager
            Owner of the persisted position state.
        client
            :py:class:`IGClient` used for every broker call.
        epic_resolver
            ``pair -> epic`` callable. In v1 GBPUSD is the only pair;
            Phase 7 wires a real lookup. Injected here so tests can
            supply a deterministic mapping.
        clock
            Optional wall-clock override (returns tz-aware UTC). Tests
            inject ``lambda: <fixed datetime>``.
        sleep
            Optional sleep override (defaults to :py:func:`time.sleep`).
            Tests supply a no-op or recording stub.
        alerter
            Optional :class:`alerts.TelegramAlerter`. When wired,
            ``open_from_signal`` emits ``TRADE_OPENED`` (INFO) on
            broker-confirmed open and ``apply_amend`` emits
            ``AMEND_FAILED`` (WARNING) on broker rejection. ``None``
            (the default) keeps the executor a quiet no-op for tests
            and pre-Phase-9 callers.
        """
        self._positions = position_manager
        self._client = client
        self._resolve_epic = epic_resolver
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._sleep = sleep or time.sleep
        self._alerter = alerter

    # --- Open from signal ---------------------------------------------------

    def open_from_signal(self, signal: Signal) -> TradeResult:
        """Submit a market order to open a position for ``signal``.

        Returns a :py:class:`TradeResult` describing the outcome.
        Idempotent against duplicate signals from the same source bar.
        """
        # 1. Idempotency check.
        existing = self._positions.by_signal_source(
            signal.pair,
            signal.strategy_name,
            signal.source_candle_ts,
        )
        if existing is not None:
            return TradeResult(
                success=True,
                deal_id=existing.deal_id,
                deal_reference=existing.deal_reference,
                opened_position=existing,
                rejection_reason="duplicate_signal_silently_reused",
            )

        # 2. Translate Signal → TradeOrder → OrderRequest.
        epic = self._resolve_epic(signal.pair)
        order = TradeOrder(
            pair=signal.pair,
            epic=epic,
            direction=signal.direction,
            size_units=EXECUTION_DEFAULT_SIZE_UNITS,
            entry_price=signal.suggested_entry_price,
            sl_price=signal.suggested_sl_price,
            tp_price=signal.suggested_tp_price,
            regime_at_entry=signal.regime,
            strategy_name=signal.strategy_name,
            signal_source_candle_ts=signal.source_candle_ts,
        )
        request = OrderRequest(
            epic=epic,
            direction="BUY" if order.direction == Direction.BULLISH else "SELL",
            size=order.size_units,
            stop_level=order.sl_price,
            limit_level=order.tp_price,
        )

        # 3. Submit and parse the confirmation.
        try:
            confirmation = self._client.open_position(request)
        except AllowanceExceeded as exc:
            logger.warning(
                "Executor.open_from_signal allowance-exceeded for %s/%s "
                "(sleep=%.1fs); not opening.",
                signal.pair,
                signal.strategy_name,
                exc.recommended_sleep_seconds,
            )
            return TradeResult(
                success=False,
                deal_id=None,
                deal_reference="",
                rejection_reason=f"allowance_exceeded:{exc.recommended_sleep_seconds:.0f}s",
            )
        except Exception as exc:  # noqa: BLE001 — broker errors are caller-facing
            logger.exception(
                "Executor.open_from_signal broker error for %s/%s: %s",
                signal.pair,
                signal.strategy_name,
                exc,
            )
            return TradeResult(
                success=False,
                deal_id=None,
                deal_reference="",
                rejection_reason=f"broker_error:{exc}",
            )

        if confirmation.status != "ACCEPTED" or not confirmation.deal_id:
            # Post-C1: ``confirmation.status`` is already derived from
            # IG's canonical ``dealStatus`` field, so this string need
            # not surface the lifecycle field as well — it was
            # actively misleading in logs ("status=ACCEPTED,
            # deal_status=REJECTED" for real rejections).
            return TradeResult(
                success=False,
                deal_id=confirmation.deal_id,
                deal_reference=confirmation.deal_reference,
                rejection_reason=(
                    f"broker_rejected:dealStatus={confirmation.deal_status},"
                    f"reason={confirmation.reason}"
                ),
            )

        # 4. Build & register the ExecutionPosition.
        position = _build_position(
            order=order,
            confirmation=confirmation,
            now_utc=self._clock(),
        )
        # M2 (review 2026-05-14): if the persistence call fails (disk
        # full, permission denied, ENOSPC mid-fsync), the broker has
        # an open position while our local state knows nothing about
        # it. The pre-fix code let the OSError propagate while
        # silently leaving the position open at IG; on the next bot
        # restart the position would surface as BROKER_ORPHAN
        # (ALERT, operator action). Fail loudly instead: log CRITICAL,
        # attempt an emergency close on the broker, and re-raise the
        # original exception so the caller (Phase 7 loop) can crash
        # rather than continue with desynced state.
        try:
            self._positions.upsert(position)
        except Exception as upsert_exc:
            logger.critical(
                "Persist failed after broker accepted open: "
                "deal_id=%s, pair=%s, exception=%r. Attempting "
                "emergency close on broker. Operator must verify "
                "no orphan position remains.",
                confirmation.deal_id,
                signal.pair,
                upsert_exc,
                exc_info=True,
            )
            try:
                self._client.close_position(
                    CloseRequest(
                        deal_id=confirmation.deal_id or "",
                        epic=epic,
                        position_direction=(
                            "BUY"
                            if order.direction == Direction.BULLISH
                            else "SELL"
                        ),
                        size=order.size_units,
                    )
                )
                logger.critical(
                    "Emergency close submitted for deal_id=%s after "
                    "persist failure.",
                    confirmation.deal_id,
                )
            except Exception as close_exc:  # noqa: BLE001
                logger.critical(
                    "Emergency close ALSO FAILED for deal_id=%s "
                    "after persist failure: %r. MANUAL INTERVENTION "
                    "REQUIRED — broker may have an unmanaged open "
                    "position.",
                    confirmation.deal_id,
                    close_exc,
                    exc_info=True,
                )
            # Re-raise the *original* persistence error so the caller
            # sees the root cause; the emergency-close outcome is on
            # the log.
            raise
        self._emit_trade_opened(order=order, position=position)
        return TradeResult(
            success=True,
            deal_id=position.deal_id,
            deal_reference=position.deal_reference,
            opened_position=position,
        )

    # --- Amend --------------------------------------------------------------

    def apply_amend(self, amend: AmendOrder) -> AmendResult:
        """Forward an :py:class:`AmendOrder` to the broker.

        Retries once after :data:`EXECUTION_AMEND_RETRY_DELAY_S` on
        any broker exception. Updates the managed position on
        success; leaves local state untouched on failure (the next
        reconciliation pass will resolve drift against broker truth).
        """
        position = self._positions.get(amend.deal_id)
        if position is None:
            return AmendResult(
                success=False,
                deal_id=amend.deal_id,
                new_sl_price=amend.new_sl_price,
                reason=f"unknown_position:{amend.deal_id}",
            )

        request = AmendRequest(
            deal_id=amend.deal_id,
            stop_level=amend.new_sl_price,
            limit_level=position.suggested_tp_price,
        )

        last_error: Optional[str] = None
        for attempt in range(EXECUTION_AMEND_RETRY_COUNT + 1):
            if attempt > 0:
                self._sleep(EXECUTION_AMEND_RETRY_DELAY_S)
            try:
                confirmation = self._client.amend_position(request)
            except AllowanceExceeded as exc:
                last_error = f"allowance_exceeded:{exc.recommended_sleep_seconds:.0f}s"
                logger.warning(
                    "Executor.apply_amend allowance-exceeded attempt %d "
                    "for deal=%s: %s",
                    attempt + 1,
                    amend.deal_id,
                    last_error,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = f"broker_error:{exc}"
                logger.exception(
                    "Executor.apply_amend broker error attempt %d for deal=%s",
                    attempt + 1,
                    amend.deal_id,
                )
                continue

            if confirmation.status != "ACCEPTED":
                last_error = (
                    f"broker_rejected:{confirmation.deal_status},"
                    f"{confirmation.reason}"
                )
                continue
            break
        else:
            self._emit_amend_failed(
                position=position, amend=amend, reason=last_error,
            )
            return AmendResult(
                success=False,
                deal_id=amend.deal_id,
                new_sl_price=amend.new_sl_price,
                reason=last_error or "unknown_failure",
            )

        is_be_move = amend.reason == "be_move_at_1r"
        updated = position.with_sl_amend(
            new_sl_price=amend.new_sl_price,
            at_utc=self._clock(),
            reason=amend.reason,
            deal_id_or_reference=confirmation.deal_id or confirmation.deal_reference,
            be_moved=True if is_be_move else None,
            trail_active=True if is_be_move else None,
        )
        # H1 (Session-3 commit-2b review): mirror the M2 pattern from
        # open_from_signal — if local persistence fails AFTER the
        # broker has accepted the amend, the broker holds the new SL
        # and our local state still has the old one. Reconciliation's
        # "broker is authoritative on SL" rule would silently mask the
        # divergence (next pass writes broker_sl back into local state
        # without an alert, because SL_DRIFT_LARGE doesn't translate
        # to an alert). Fail loudly: log CRITICAL, fire a CRITICAL
        # AMEND_PERSIST_FAILED alert (bypasses coalescing), and
        # re-raise the original persistence error so the BotLoop
        # caller (_run_sl_evaluation) logs at exception level and
        # continues — the bot does NOT crash automatically, but the
        # operator now has a CRITICAL Telegram alert pointing them
        # at the divergence. (Repeated failures may eventually trip
        # the 5-strike event-failure counter and trigger
        # FAILURE_THRESHOLD_TRIPPED, but a single occurrence does
        # not.) Emergency action: unlike open_from_signal we do NOT
        # try to revert the amend automatically — a revert call is
        # itself a broker round-trip that can fail, and a
        # failed-revert loop is worse than a loud alert. Operator
        # reconciles manually.
        try:
            self._positions.upsert(updated)
        except Exception as upsert_exc:
            logger.critical(
                "Amend persisted at broker but local upsert FAILED — "
                "STATE DIVERGED: deal_id=%s, broker_new_sl=%s, "
                "local_old_sl=%s, exception=%r. CRITICAL alert "
                "dispatched; re-raising for caller to handle.",
                position.deal_id,
                amend.new_sl_price,
                position.current_sl_price,
                upsert_exc,
                exc_info=True,
            )
            self._emit_amend_persist_failed(
                position=position, amend=amend, exc_summary=str(upsert_exc),
            )
            raise
        return AmendResult(
            success=True,
            deal_id=amend.deal_id,
            new_sl_price=amend.new_sl_price,
            reason=amend.reason,
            broker_status=confirmation.status,
        )


    # ------------------------------------------------------------------
    # Alerter helpers
    # ------------------------------------------------------------------

    def _emit_trade_opened(
        self, *, order: TradeOrder, position: ExecutionPosition,
    ) -> None:
        if self._alerter is None:
            return
        side = "BUY" if order.direction == Direction.BULLISH else "SELL"
        full = (
            f"{order.pair} {side} @ {position.entry_price:.5f} "
            f"SL={position.current_sl_price:.5f} "
            f"strategy={order.strategy_name}"
        )
        short = f"{side} @ {position.entry_price:.5f}"
        try:
            self._alerter.send(
                Alert(
                    category=AlertCategory.TRADE,
                    event_subtype="TRADE_OPENED",
                    severity=AlertSeverity.INFO,
                    pair=order.pair,
                    full_text=full,
                    short_text=short,
                    timestamp=self._clock(),
                    debug={
                        "deal_id": position.deal_id,
                        "strategy": order.strategy_name,
                        "regime": str(order.regime_at_entry),
                    },
                )
            )
        except Exception:
            logger.exception("alerter.send raised for TRADE_OPENED")

    def _emit_amend_failed(
        self,
        *,
        position: ExecutionPosition,
        amend: AmendOrder,
        reason: Optional[str],
    ) -> None:
        if self._alerter is None:
            return
        full = (
            f"SL amend failed for {position.pair} deal={amend.deal_id} "
            f"new_sl={amend.new_sl_price:.5f} reason={reason or 'unknown'}"
        )
        short = f"deal={amend.deal_id}: {reason or 'unknown'}"
        try:
            self._alerter.send(
                Alert(
                    category=AlertCategory.TRADE,
                    event_subtype="AMEND_FAILED",
                    severity=AlertSeverity.WARNING,
                    pair=position.pair,
                    full_text=full,
                    short_text=short,
                    timestamp=self._clock(),
                    debug={
                        "deal_id": amend.deal_id,
                        "new_sl": amend.new_sl_price,
                        "reason": reason,
                        "amend_reason": amend.reason,
                    },
                )
            )
        except Exception:
            logger.exception("alerter.send raised for AMEND_FAILED")

    def _emit_amend_persist_failed(
        self,
        *,
        position: ExecutionPosition,
        amend: AmendOrder,
        exc_summary: str,
    ) -> None:
        """CRITICAL alert when broker accepts amend but local upsert fails.

        H1 (Session-3 commit-2b review): the broker now holds the
        updated SL and our local state still has the old one. Future
        reconciliation passes silently overwrite local with broker's
        value — without an alert, the operator never learns that a
        persistence failure happened. CRITICAL severity bypasses
        coalescing so the alert ships immediately, even if the bot
        crashes in the next instruction (the alerter has its own
        three-layer isolation; the call returns before the raise).
        """
        if self._alerter is None:
            return
        full = (
            f"\U0001f6a8 AMEND PERSIST FAILED\n"
            f"{position.pair} deal={position.deal_id}\n"
            f"Broker SL: {amend.new_sl_price:.5f} | "
            f"Local SL: {position.current_sl_price:.5f}\n"
            f"STATE DIVERGED — manual reconciliation required\n"
            f"Error: {exc_summary}"
        )
        short = (
            f"{position.pair} STATE DIVERGED deal={position.deal_id}"
        )
        try:
            self._alerter.send(
                Alert(
                    category=AlertCategory.TRADE,
                    event_subtype="AMEND_PERSIST_FAILED",
                    severity=AlertSeverity.CRITICAL,
                    pair=position.pair,
                    full_text=full,
                    short_text=short,
                    timestamp=self._clock(),
                    debug={
                        "deal_id": position.deal_id,
                        "broker_new_sl": amend.new_sl_price,
                        "local_old_sl": position.current_sl_price,
                        "exception": exc_summary,
                    },
                )
            )
        except Exception:
            logger.exception(
                "alerter.send raised for AMEND_PERSIST_FAILED — "
                "STATE DIVERGENCE is unalerted; operator must check "
                "logs for the persist-failure CRITICAL line"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_position(
    *,
    order: TradeOrder,
    confirmation: DealConfirmation,
    now_utc: datetime,
) -> ExecutionPosition:
    entry_price = (
        confirmation.level
        if confirmation.level is not None
        else order.entry_price
    )
    return ExecutionPosition(
        deal_id=confirmation.deal_id or "",
        deal_reference=confirmation.deal_reference,
        pair=order.pair,
        direction=order.direction,
        regime_at_entry=order.regime_at_entry,
        strategy_name=order.strategy_name,
        size_units=order.size_units,
        entry_price=float(entry_price),
        initial_sl_price=order.sl_price,
        current_sl_price=order.sl_price,
        suggested_tp_price=order.tp_price,
        entry_time_utc=confirmation.date or now_utc,
        signal_source_candle_ts=order.signal_source_candle_ts,
        be_moved=False,
        trail_active=False,
        sl_history=(),
    )


__all__ = ["Executor"]
