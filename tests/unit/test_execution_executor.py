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
        self.close_returns: list[DealConfirmation] = []
        self.open_exceptions: list[BaseException] = []
        self.amend_exceptions: list[BaseException] = []
        self.close_exceptions: list[BaseException] = []

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

    # --- Close (used by M2 emergency-close path) -----------------------

    def queue_close(self, confirmation: DealConfirmation) -> None:
        self.close_returns.append(confirmation)

    def queue_close_exception(self, exc: BaseException) -> None:
        self.close_exceptions.append(exc)

    def close_position(self, close: CloseRequest) -> DealConfirmation:
        self.close_calls.append(close)
        if self.close_exceptions:
            raise self.close_exceptions.pop(0)
        if self.close_returns:
            return self.close_returns.pop(0)
        # Default: a synthetic ACCEPTED close confirm.
        return DealConfirmation(
            deal_reference="REF_CLOSE",
            deal_id=close.deal_id,
            status="ACCEPTED",
            deal_status="ACCEPTED",
            raw={},
        )

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
        deal_status="ACCEPTED",
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
    assert "dealStatus=REJECTED" in (result.rejection_reason or "")
    assert len(mgr) == 0


def test_open_from_signal_real_ig_rejected_shape_does_not_register_position(
    tmp_path: Path,
) -> None:
    """C1 regression (review 2026-05-14): a real-shape IG REJECTED
    confirmation (``dealStatus="REJECTED"``, no ``status`` field) must
    flow through the parser → executor pipeline as ``success=False``
    and leave ``PositionManager`` empty. Pre-fix, the parser silently
    classified this as ACCEPTED, registering a phantom position and
    consuming the idempotency key.
    """
    from feed.ig_rest.positions import _parse_deal_confirmation

    real_ig_rejected_payload = {
        "dealReference": "REF_REJ",
        "dealStatus": "REJECTED",
        "reason": "MARKET_OFFLINE",
        # NOTE: no "status" field — that's what real IG sends for rejects.
    }
    parsed = _parse_deal_confirmation(real_ig_rejected_payload)
    assert parsed.status == "REJECTED"  # parser-level sanity

    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(parsed)
    sig = _ig_signal()
    result = executor.open_from_signal(sig)

    assert result.success is False
    assert len(mgr) == 0
    # And the idempotency key was NOT consumed — a retry of the same
    # signal must be allowed to fire (e.g. when the strategy emits the
    # same setup on the next polling cycle if market reopens).
    assert mgr.by_signal_source(
        sig.pair, sig.strategy_name, sig.source_candle_ts
    ) is None


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


def test_open_from_signal_persist_failure_emergency_closes_and_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """M2 regression (review 2026-05-14): when persistence fails after
    the broker has accepted the open, the executor must (a) log
    CRITICAL, (b) attempt an emergency close on the broker, (c)
    re-raise the original exception so the caller crashes loudly
    rather than continuing with desynced local state.
    """
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(_accept(deal_id="DEAL_PERSIST_FAIL"))

    def _boom(_pos) -> None:
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(mgr, "upsert", _boom)

    with pytest.raises(OSError, match="ENOSPC"):
        executor.open_from_signal(_ig_signal())

    # Emergency close was attempted with the original direction
    # translated to BUY (long position → emergency close uses BUY,
    # which the IG wrapper will flip to SELL inside close_position).
    assert len(fake.close_calls) == 1
    close_req = fake.close_calls[0]
    assert close_req.deal_id == "DEAL_PERSIST_FAIL"
    assert close_req.position_direction == "BUY"
    assert close_req.size == 1.0


def test_open_from_signal_persist_failure_close_also_fails_still_raises_original(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """M2: even when the emergency-close call ALSO fails, the original
    persistence error propagates (the emergency-close outcome is on
    the log only). Both failures must be logged at CRITICAL.
    """
    import logging

    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(_accept(deal_id="DEAL_DOUBLE_FAIL"))
    fake.queue_close_exception(RuntimeError("connection refused"))

    def _boom(_pos) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mgr, "upsert", _boom)

    with caplog.at_level(logging.CRITICAL, logger="execution.executor"):
        with pytest.raises(OSError, match="disk full"):
            executor.open_from_signal(_ig_signal())

    # Both criticals logged.
    messages = [r.message for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any("Persist failed" in m for m in messages)
    assert any("Emergency close ALSO FAILED" in m for m in messages)


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


# ---------------------------------------------------------------------------
# Phase 9 commit 2b — Executor alerter wiring
# ---------------------------------------------------------------------------


class _RecordingAlerter:
    """Test alerter that just records sent alerts."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, alert) -> None:
        self.sent.append(alert)

    def tick(self) -> None:
        pass

    def close(self) -> None:
        pass


def _make_executor_with_alerter(
    tmp_path: Path,
) -> tuple[Executor, _FakeIGClient, PositionManager, _RecordingAlerter]:
    mgr = PositionManager(PositionsState(path=tmp_path / "p.json"))
    fake = _FakeIGClient()
    alerter = _RecordingAlerter()

    exec_ = Executor(
        position_manager=mgr,
        client=fake,  # type: ignore[arg-type]
        epic_resolver=lambda pair: f"CS.D.{pair}.TODAY.IP",
        clock=lambda: _TS,
        sleep=lambda s: None,
        alerter=alerter,  # type: ignore[arg-type]
    )
    return exec_, fake, mgr, alerter


def test_open_from_signal_emits_trade_opened_on_success(tmp_path: Path) -> None:
    """TRADE_OPENED is INFO + TRADE category, fires only on success path."""
    executor, fake, _, alerter = _make_executor_with_alerter(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())
    assert len(alerter.sent) == 1
    alert = alerter.sent[0]
    from alerts import AlertCategory, AlertSeverity
    assert alert.event_subtype == "TRADE_OPENED"
    assert alert.severity is AlertSeverity.INFO
    assert alert.category is AlertCategory.TRADE
    assert alert.pair == "GBPUSD"
    assert "BUY" in alert.full_text
    assert alert.timestamp == _TS


def test_open_from_signal_no_alert_on_broker_rejection(tmp_path: Path) -> None:
    """Broker rejection: no TRADE_OPENED alert, returns failure result."""
    executor, fake, _, alerter = _make_executor_with_alerter(tmp_path)
    fake.queue_open(_rejected("REJECTED_BY_MARKET"))
    result = executor.open_from_signal(_ig_signal())
    assert result.success is False
    assert alerter.sent == []


def test_open_from_signal_no_alert_on_allowance_exceeded(tmp_path: Path) -> None:
    executor, fake, _, alerter = _make_executor_with_alerter(tmp_path)
    fake.queue_open_exception(AllowanceExceeded(recommended_sleep_seconds=60.0))
    result = executor.open_from_signal(_ig_signal())
    assert result.success is False
    assert alerter.sent == []


def test_open_from_signal_no_alert_on_duplicate_signal(tmp_path: Path) -> None:
    """Idempotency hit: same source bar → no second TRADE_OPENED. The
    duplicate-signal-silently-reused path must NOT re-alert (would spam
    the chat on retries / re-deliveries)."""
    executor, fake, _, alerter = _make_executor_with_alerter(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())
    assert len(alerter.sent) == 1
    # Re-deliver the same signal — idempotency reuses the existing position.
    executor.open_from_signal(_ig_signal())
    assert len(alerter.sent) == 1, "duplicate signal must not re-alert"


def test_apply_amend_emits_amend_failed_on_double_rejection(tmp_path: Path) -> None:
    """Both attempts rejected → AMEND_FAILED (WARNING)."""
    executor, fake, _, alerter = _make_executor_with_alerter(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())
    alerter.sent.clear()
    fake.queue_amend(_rejected("MARKET_OFFLINE"))
    fake.queue_amend(_rejected("MARKET_OFFLINE"))
    result = executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is False
    failed = [a for a in alerter.sent if a.event_subtype == "AMEND_FAILED"]
    assert len(failed) == 1
    from alerts import AlertCategory, AlertSeverity
    assert failed[0].severity is AlertSeverity.WARNING
    assert failed[0].category is AlertCategory.TRADE
    assert failed[0].pair == "GBPUSD"
    assert "D1" in failed[0].full_text


def test_apply_amend_no_alert_on_success(tmp_path: Path) -> None:
    """Successful amend (first try) should NOT emit AMEND_FAILED."""
    executor, fake, _, alerter = _make_executor_with_alerter(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())
    alerter.sent.clear()
    fake.queue_amend(_accept())
    result = executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is True
    failed = [a for a in alerter.sent if a.event_subtype == "AMEND_FAILED"]
    assert failed == []


def test_apply_amend_no_alert_when_unknown_position(tmp_path: Path) -> None:
    """Unknown deal_id is a fast-fail without broker contact — no
    AMEND_FAILED alert (it's a programmer error / stale deal_id, not
    a broker rejection the operator needs to know about via Telegram)."""
    executor, _, _, alerter = _make_executor_with_alerter(tmp_path)
    result = executor.apply_amend(
        AmendOrder(deal_id="NOPE", new_sl_price=1.0, reason="trail_active")
    )
    assert result.success is False
    assert alerter.sent == []


def test_executor_without_alerter_open_does_not_raise(tmp_path: Path) -> None:
    """Default constructor (no alerter) keeps existing tests' contract
    — no AttributeError on the trade-opened path."""
    executor, fake, _ = _make_executor(tmp_path)
    fake.queue_open(_accept())
    result = executor.open_from_signal(_ig_signal())
    assert result.success is True


def test_executor_without_alerter_amend_failure_does_not_raise(tmp_path: Path) -> None:
    executor, fake, _ = _make_executor(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())
    fake.queue_amend(_rejected("X"))
    fake.queue_amend(_rejected("X"))
    result = executor.apply_amend(
        AmendOrder(deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r")
    )
    assert result.success is False


def test_apply_amend_persist_failure_emits_critical_and_raises(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """H1 (Session-3 commit-2b review): broker accepts amend, local
    upsert fails (e.g. ENOSPC). The executor must:
    - log CRITICAL describing the divergence,
    - emit a CRITICAL ``AMEND_PERSIST_FAILED`` alert (bypasses
      coalescing — operator gets it immediately),
    - re-raise the original persistence error so the caller can
      shut the bot down rather than continue with desynced state.
    """
    import logging

    executor, fake, mgr, alerter = _make_executor_with_alerter(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())
    alerter.sent.clear()

    # Broker ACCEPTS the amend on first try.
    fake.queue_amend(_accept())

    # ...but the upsert that should follow blows up.
    original_upsert = mgr.upsert
    upsert_calls: list = []

    def _boom(pos):
        # First call (from open_from_signal) was already done; this
        # is the amend-time upsert we want to fail.
        upsert_calls.append(pos)
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(mgr, "upsert", _boom)

    with caplog.at_level(logging.CRITICAL, logger="execution.executor"):
        with pytest.raises(OSError, match="ENOSPC"):
            executor.apply_amend(
                AmendOrder(
                    deal_id="D1",
                    new_sl_price=1.30010,
                    reason="be_move_at_1r",
                )
            )

    # CRITICAL log entry describing the divergence.
    crit_msgs = [
        r.message for r in caplog.records if r.levelno == logging.CRITICAL
    ]
    assert any("STATE DIVERGED" in m for m in crit_msgs)
    assert any("D1" in m for m in crit_msgs)

    # CRITICAL alert dispatched (bypasses coalescer).
    from alerts import AlertCategory, AlertSeverity
    persist_alerts = [
        a for a in alerter.sent if a.event_subtype == "AMEND_PERSIST_FAILED"
    ]
    assert len(persist_alerts) == 1
    alert = persist_alerts[0]
    assert alert.severity is AlertSeverity.CRITICAL
    assert alert.category is AlertCategory.TRADE
    assert alert.pair == "GBPUSD"
    assert "STATE DIVERGED" in alert.full_text
    assert "D1" in alert.full_text
    assert alert.debug["broker_new_sl"] == 1.30010


def test_apply_amend_persist_failure_no_alerter_still_raises(
    tmp_path: Path, monkeypatch,
) -> None:
    """Without an alerter, the persist failure still re-raises so the
    caller can crash. The alert path is just skipped silently."""
    executor, fake, mgr = _make_executor(tmp_path)
    fake.queue_open(_accept())
    executor.open_from_signal(_ig_signal())
    fake.queue_amend(_accept())

    def _boom(pos):
        raise OSError("disk full")

    monkeypatch.setattr(mgr, "upsert", _boom)

    with pytest.raises(OSError, match="disk full"):
        executor.apply_amend(
            AmendOrder(
                deal_id="D1", new_sl_price=1.30010, reason="be_move_at_1r",
            )
        )
