"""Tests for execution.executor.Executor.

The :py:class:`IGClient` collaborator is stubbed with an in-memory
fake that records calls and returns canned :py:class:`DealConfirmation`
records. No network access; no ``trading_ig`` library calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from execution.executor import Executor
from execution.position_manager import PositionManager
from execution.state.positions_state import PositionsState
from execution.types import AmendOrder
from feed.ig_rest.client import AllowanceExceeded
from feed.ig_rest.types import (
    AmendRequest,
    BrokerPosition,
    CloseRequest,
    DealConfirmation,
    OrderRequest,
)
from regime.labels import Direction, RegimeLabel
from strategies.signal import Signal


_TS = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fake IGClient
# ---------------------------------------------------------------------------


class _FakeIGClient:
    """Records every call and returns canned confirmations."""

    def __init__(self) -> None:
        self.open_calls: list[OrderRequest] = []
        self.amend_calls: list[AmendRequest] = []
        self.close_calls: list[CloseRequest] = []
        self.open_returns: list[DealConfirmation] = []
        self.amend_returns: list[DealConfirmation] = []
        self.open_exceptions: list[BaseException] = []
        self.amend_exceptions: list[BaseException] = []

    # --- Configuration --------------------------------------------------

    def queue_open(self, confirmation: DealConfirmation) -> None:
        self.open_returns.append(confirmation)

    def queue_amend(self, confirmation: DealConfirmation) -> None:
        self.amend_returns.append(confirmation)

    def queue_open_exception(self, exc: BaseException) -> None:
        self.open_exceptions.append(exc)

    def queue_amend_exception(self, exc: BaseException) -> None:
        self.amend_exceptions.append(exc)

    # --- IGClient surface (subset) -------------------------------------

    def open_position(self, order: OrderRequest) -> DealConfirmation:
        self.open_calls.append(order)
        if self.open_exceptions:
            raise self.open_exceptions.pop(0)
        if not self.open_returns:
            raise AssertionError("no queued open confirmation")
        return self.open_returns.pop(0)

    def amend_position(self, amend: AmendRequest) -> DealConfirmation:
        self.amend_calls.append(amend)
        if self.amend_exceptions:
            raise self.amend_exceptions.pop(0)
        if not self.amend_returns:
            raise AssertionError("no queued amend confirmation")
        return self.amend_returns.pop(0)

    def close_position(self, close: CloseRequest) -> DealConfirmation:
        self.close_calls.append(close)
        raise AssertionError("not exercised in these tests")

    def fetch_open_positions(self) -> list[BrokerPosition]:  # pragma: no cover
        return []

    def fetch_open_position_by_deal_id(
        self, deal_id: str
    ) -> Optional[BrokerPosition]:  # pragma: no cover
        return None

    def fetch_deal_confirmation(
        self, deal_reference: str
    ) -> DealConfirmation:  # pragma: no cover
        raise AssertionError("not exercised in these tests")


def _ig_signal(**overrides) -> Signal:
    defaults = dict(
        pair="GBPUSD",
        direction=Direction.BULLISH,
        regime=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        suggested_entry_price=1.30000,
        suggested_sl_price=1.29850,
        suggested_tp_price=None,
        confidence_score=0.85,
        source_candle_ts=_TS,
        invalid_after_candle_ts=_TS,
        debug={},
    )
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def _accept(
    *,
    deal_id: str = "D1",
    deal_reference: str = "REF1",
    level: float = 1.30000,
    stop_level: float | None = 1.29850,
) -> DealConfirmation:
    return DealConfirmation(
        deal_reference=deal_reference,
        deal_id=deal_id,
        status="ACCEPTED",
        deal_status="OPEN",
        epic="CS.D.GBPUSD.TODAY.IP",
        direction="BUY",
        size=1.0,
        level=level,
        stop_level=stop_level,
        date=_TS,
        raw={},
    )


def _rejected(reason: str = "VOLATILITY_TOO_HIGH") -> DealConfirmation:
    return DealConfirmation(
        deal_reference="REF_REJ",
        deal_id=None,
        status="REJECTED",
        deal_status="REJECTED",
        reason=reason,
        raw={},
    )


def _make_executor(
    tmp_path: Path,
    *,
    sleep_calls: list | None = None,
    fake: Optional[_FakeIGClient] = None,
    clock_value: Optional[datetime] = None,
) -> tuple[Executor, _FakeIGClient, PositionManager]:
    mgr = PositionManager(PositionsState(path=tmp_path / "p.json"))
    fake = fake or _FakeIGClient()

    def _sleep(s: float) -> None:
        if sleep_calls is not None:
            sleep_calls.append(s)

    def _clock() -> datetime:
        return clock_value or _TS

    exec_ = Executor(
        position_manager=mgr,
        client=fake,  # type: ignore[arg-type]
        epic_resolver=lambda pair: f"CS.D.{pair}.TODAY.IP",
        clock=_clock,
        sleep=_sleep,
    )
    return exec_, fake, mgr


# --- open_from_signal ------------------------------------------------------


def test_open_from_signal_happy_path(tmp_path: Path) -> None:
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(_accept())
    result = executor.open_from_signal(_ig_signal())
    assert result.success is True
    assert result.deal_id == "D1"
    assert mgr.get("D1") is not None
    assert len(fake.open_calls) == 1
    submitted = fake.open_calls[0]
    assert submitted.direction == "BUY"
    assert submitted.stop_level == pytest.approx(1.29850)


def test_open_from_signal_short_translates_to_sell(tmp_path: Path) -> None:
    executor, fake, _ = _make_executor(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(
        _ig_signal(direction=Direction.BEARISH, suggested_sl_price=1.30150)
    )
    assert fake.open_calls[0].direction == "SELL"


def test_open_from_signal_rejection_returns_failure(tmp_path: Path) -> None:
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(_rejected("MARKET_OFFLINE"))
    result = executor.open_from_signal(_ig_signal())
    assert result.success is False
    assert "broker_rejected" in (result.rejection_reason or "")
    assert len(mgr) == 0


def test_open_from_signal_allowance_exceeded_returns_failure(tmp_path: Path) -> None:
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open_exception(AllowanceExceeded(60.0))
    result = executor.open_from_signal(_ig_signal())
    assert result.success is False
    assert "allowance_exceeded" in (result.rejection_reason or "")
    assert len(mgr) == 0


def test_open_from_signal_broker_exception_returns_failure(tmp_path: Path) -> None:
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open_exception(RuntimeError("connection reset"))
    result = executor.open_from_signal(_ig_signal())
    assert result.success is False
    assert "broker_error" in (result.rejection_reason or "")


# --- Idempotency ----------------------------------------------------------


def test_duplicate_signal_silently_reuses_position(tmp_path: Path) -> None:
    executor, fake, _ = _make_executor(tmp_path)
    fake.queue_open(_accept())
    first = executor.open_from_signal(_ig_signal())
    second = executor.open_from_signal(_ig_signal())  # same source_ts
    assert first.success and second.success
    assert second.deal_id == first.deal_id
    assert second.rejection_reason == "duplicate_signal_silently_reused"
    # Only one broker call made.
    assert len(fake.open_calls) == 1


# --- apply_amend ----------------------------------------------------------


def test_apply_amend_be_move_flips_flags(tmp_path: Path) -> None:
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())

    fake.queue_amend(_accept(deal_id="D1", stop_level=1.30010))
    result = executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is True
    pos = mgr.get("D1")
    assert pos is not None
    assert pos.be_moved is True
    assert pos.trail_active is True
    assert pos.current_sl_price == 1.30010
    assert len(pos.sl_history) == 1


def test_apply_amend_trail_does_not_re_flip_flags(tmp_path: Path) -> None:
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())

    # First, BE move.
    fake.queue_amend(_accept())
    executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    # Then a trail amend.
    fake.queue_amend(_accept())
    executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30050, reason="trail_swing_primary")
    )
    pos = mgr.get("D1")
    assert pos is not None
    assert pos.current_sl_price == 1.30050
    assert pos.be_moved is True  # unchanged
    assert pos.trail_active is True
    assert len(pos.sl_history) == 2


def test_apply_amend_unknown_position_fails_fast(tmp_path: Path) -> None:
    executor, _, _ = _make_executor(tmp_path)
    result = executor.apply_amend(
        AmendOrder(deal_id="UNKNOWN", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is False
    assert "unknown_position" in result.reason


def test_apply_amend_retries_once_on_failure(tmp_path: Path) -> None:
    sleep_calls: list = []
    executor, fake, mgr = _make_executor(tmp_path, sleep_calls=sleep_calls)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())

    fake.queue_amend_exception(RuntimeError("transient"))
    fake.queue_amend(_accept())  # second attempt succeeds
    result = executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is True
    assert len(sleep_calls) == 1


def test_apply_amend_double_failure_returns_failure(tmp_path: Path) -> None:
    sleep_calls: list = []
    executor, fake, mgr = _make_executor(tmp_path, sleep_calls=sleep_calls)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())

    fake.queue_amend_exception(RuntimeError("first"))
    fake.queue_amend_exception(RuntimeError("second"))
    result = executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is False
    # Local state untouched on amend failure.
    pos = mgr.get("D1")
    assert pos is not None and pos.current_sl_price == 1.29850
    assert pos.be_moved is False


def test_apply_amend_rejected_status_returns_failure(tmp_path: Path) -> None:
    sleep_calls: list = []
    executor, fake, mgr = _make_executor(tmp_path, sleep_calls=sleep_calls)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())

    fake.queue_amend(_rejected("MARKET_OFFLINE"))
    fake.queue_amend(_rejected("MARKET_OFFLINE"))  # retry also rejected
    result = executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is False
    assert "broker_rejected" in result.reason
