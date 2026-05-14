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

from feed.ig_rest import (
    AllowanceExceeded,
    AmendRequest,
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
        """
        self._positions = position_manager
        self._client = client
        self._resolve_epic = epic_resolver
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._sleep = sleep or time.sleep

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
            return TradeResult(
                success=False,
                deal_id=confirmation.deal_id,
                deal_reference=confirmation.deal_reference,
                rejection_reason=(
                    f"broker_rejected:status={confirmation.status},"
                    f"deal_status={confirmation.deal_status},"
                    f"reason={confirmation.reason}"
                ),
            )

        # 4. Build & register the ExecutionPosition.
        position = _build_position(
            order=order,
            confirmation=confirmation,
            now_utc=self._clock(),
        )
        self._positions.upsert(position)
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
        self._positions.upsert(updated)
        return AmendResult(
            success=True,
            deal_id=amend.deal_id,
            new_sl_price=amend.new_sl_price,
            reason=amend.reason,
            broker_status=confirmation.status,
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
