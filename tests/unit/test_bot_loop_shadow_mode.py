"""Tests for BotLoop SHADOW_MODE intercept (Phase 10).

The intercept lives in :py:meth:`BotLoop._evaluate_and_execute` AFTER
``risk_guard.allow_entry`` and BEFORE the executor call. These tests
pin:

- shadow_mode=False (default) → executor.open_from_signal called
  normally; no SHADOW_TRADE alert.
- shadow_mode=True + signal approved → executor NOT called;
  SHADOW_TRADE alert emitted with [SHADOW] marker, ghost emoji,
  INFO/TRADE classification.
- shadow_mode=True + signal rejected by risk → no SHADOW_TRADE
  alert (rejection logged normally; the intercept is downstream of
  the risk gate).
- shadow_mode=True + market snapshot None → no SHADOW_TRADE alert
  (intercept is downstream of the market-snapshot gate too).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.loop import BotLoop
from bot.types import BotState
from day_type import DayType
from common import Direction
from strategies.signal import Signal


_NOW = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _signal(
    pair: str = "GBPUSD",
    direction: Direction = Direction.BULLISH,
    suggested_entry_price: float = 1.30000,
    suggested_sl_price: float = 1.29850,
) -> Signal:
    return Signal(
        pair=pair,
        direction=direction,
        day_type=DayType.NORMAL,
        strategy_name="ema_pullback",
        suggested_entry_price=suggested_entry_price,
        suggested_sl_price=suggested_sl_price,
        suggested_tp_price=None,
        confidence_score=0.85,
        source_candle_ts=_NOW,
        invalid_after_candle_ts=_NOW + timedelta(minutes=5),
        debug={},
    )


# Re-use the bot_loop test fixtures via a thin wrapper. We import
# them here rather than duplicating.
from tests.unit.test_bot_loop import (  # type: ignore[import-not-found]
    _RecordingAlerter,
    _build,
)


def _build_with_shadow(monkeypatch, *, shadow_mode: bool, alerter=None):
    """Build a BotLoop with explicit shadow_mode + optional alerter.

    Reuses _build() from test_bot_loop to get the underlying fakes,
    then re-constructs the bot with the shadow flag set."""
    bot, pieces = _build(monkeypatch)
    pairs = ("GBPUSD",)
    if alerter is None:
        alerter = _RecordingAlerter()
    bot = BotLoop(
        feed_manager=pieces["feed"],
        ig_client=pieces["ig"],
        executor=pieces["executor"],
        risk_guard=pieces["risk"],
        position_manager=pieces["positions"],
        pairs=tuple(pairs),
        pair_to_epic={p: f"CS.D.{p}.TODAY.IP" for p in pairs},
        clock=lambda: _NOW,
        alerter=alerter,  # type: ignore[arg-type]
        shadow_mode=shadow_mode,
    )
    alerter._state_getter = lambda: bot.state
    pieces["alerter"] = alerter
    return bot, pieces


# ---------------------------------------------------------------------------
# shadow_mode=False (default behaviour)
# ---------------------------------------------------------------------------


def test_shadow_mode_default_is_false(monkeypatch) -> None:
    """The default is safe: shadow_mode flag exists, defaults False."""
    bot, _ = _build(monkeypatch)
    assert bot._shadow_mode is False


def test_shadow_mode_false_calls_executor_normally(monkeypatch) -> None:
    """With shadow_mode=False, an approved signal flows to the
    executor as in pre-Phase-10 behaviour."""
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=False)
    bot.start()
    bot.mark_ready()
    bot._evaluate_and_execute(_signal())
    # Executor recorded the call.
    assert len(pieces["executor"].opened) == 1
    assert pieces["executor"].opened[0].pair == "GBPUSD"
    # No SHADOW_TRADE alert in non-shadow mode.
    shadows = [a for a in pieces["alerter"].sent if a.event_subtype == "SHADOW_TRADE"]
    assert shadows == []


# ---------------------------------------------------------------------------
# shadow_mode=True
# ---------------------------------------------------------------------------


def test_shadow_mode_true_intercepts_executor_call(monkeypatch) -> None:
    """With shadow_mode=True, an approved signal does NOT reach the
    executor — replaced by a SHADOW_TRADE alert."""
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    bot._evaluate_and_execute(_signal())
    # Executor was NOT called.
    assert pieces["executor"].opened == []
    # SHADOW_TRADE alert fired.
    shadows = [a for a in pieces["alerter"].sent if a.event_subtype == "SHADOW_TRADE"]
    assert len(shadows) == 1
    a = shadows[0]
    from alerts import AlertCategory, AlertSeverity
    assert a.severity is AlertSeverity.INFO
    assert a.category is AlertCategory.TRADE
    assert a.pair == "GBPUSD"
    # Body has the ghost-emoji prefix and the [SHADOW] marker.
    assert "\U0001f47b" in a.full_text  # 👻
    assert "[SHADOW]" in a.full_text
    assert "[mode=shadow]" in a.full_text
    assert "BUY" in a.full_text
    assert "1.30000" in a.full_text
    # Debug payload has the structured shadow context.
    assert a.debug["mode"] == "shadow"
    assert a.debug["direction"] == "BUY"
    assert a.debug["planned_entry"] == 1.30000


def test_shadow_mode_short_signal_renders_sell(monkeypatch) -> None:
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    bot._evaluate_and_execute(_signal(direction=Direction.BEARISH, suggested_sl_price=1.30150))
    shadows = [a for a in pieces["alerter"].sent if a.event_subtype == "SHADOW_TRADE"]
    assert "SELL" in shadows[0].full_text


def test_shadow_mode_does_not_increment_inflight_counter(monkeypatch) -> None:
    """The intercept is BEFORE _with_inflight_tracked — no broker
    round-trip means no inflight counter contribution that would
    gratuitously hold up shutdown drain."""
    bot, _ = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    bot._evaluate_and_execute(_signal())
    assert bot.inflight_count == 0


def test_shadow_mode_risk_rejection_does_not_alert(monkeypatch) -> None:
    """When risk_guard.allow_entry rejects, no SHADOW_TRADE fires —
    the intercept is downstream of the risk gate, so a rejected
    signal has nothing to shadow."""
    from tests.unit.test_bot_loop import _Decision  # type: ignore[import-not-found]
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    pieces["risk"].allow_result = _Decision(
        allow=False, rule="daily_dd", reason="capped", debug={},
    )
    bot._evaluate_and_execute(_signal())
    # No alert; executor not called.
    assert pieces["alerter"].sent == []
    assert pieces["executor"].opened == []


def test_shadow_mode_market_snapshot_none_does_not_alert(monkeypatch) -> None:
    """When _build_market_snapshot returns None (e.g. broker info
    fetch failed), the executor would be skipped in non-shadow mode
    too — shadow mode preserves the same gating."""
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    monkeypatch.setattr(bot, "_build_market_snapshot", lambda pair: None)
    bot._evaluate_and_execute(_signal())
    assert pieces["alerter"].sent == []
    assert pieces["executor"].opened == []


def test_shadow_mode_logs_warning_at_construction(monkeypatch, caplog) -> None:
    """A shadow_mode=True boot logs a WARNING so journalctl shows
    the deployment mode even if the operator missed the STARTUP
    Telegram alert."""
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.loop"):
        _build_with_shadow(monkeypatch, shadow_mode=True)
    warnings = [
        r.message for r in caplog.records
        if "SHADOW_MODE" in r.message and r.levelno == logging.WARNING
    ]
    assert any("no trades will be opened" in w for w in warnings)


def test_shadow_mode_false_does_not_log_shadow_warning(monkeypatch, caplog) -> None:
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.loop"):
        _build_with_shadow(monkeypatch, shadow_mode=False)
    shadow_warnings = [
        r.message for r in caplog.records
        if "SHADOW_MODE" in r.message and r.levelno == logging.WARNING
    ]
    assert shadow_warnings == []


# ---------------------------------------------------------------------------
# Other alert paths still work in shadow mode (intercept is narrow)
# ---------------------------------------------------------------------------


def test_shadow_mode_feed_stale_alert_still_fires(monkeypatch) -> None:
    """SHADOW_MODE doesn't affect feed-state transitions or their
    alerts. Only the broker-call leg of the trade pipeline is
    intercepted."""
    from feed.types import FeedEvent, FeedEventKind
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    pieces["alerter"].sent.clear()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    feed_stale = [a for a in pieces["alerter"].sent if a.event_subtype == "FEED_STALE"]
    assert len(feed_stale) == 1


# ---------------------------------------------------------------------------
# H1 layer 2 (Phase 10 Session-3 review): defense-in-depth gates
# ---------------------------------------------------------------------------


def _seed_position(pieces) -> str:
    """Insert a position into the fake PositionManager and return its
    deal_id. Used by the layer-2 defense tests where the bot is
    constructed directly (bypassing layer 1 in bot.main)."""
    from execution.types import ExecutionPosition
    from common import Direction
    pos = ExecutionPosition(
        deal_id="DEAL_GUARD",
        deal_reference="REF",
        pair="GBPUSD",
        direction=Direction.BULLISH,
        day_type_at_entry=DayType.NORMAL,
        strategy_name="trend_break",
        size_units=1.0,
        entry_price=1.30050,
        initial_sl_price=1.29900,
        current_sl_price=1.29900,
        suggested_tp_price=1.30450,
        entry_time_utc=_NOW,
        signal_source_candle_ts=_NOW,
        be_moved=False,
        trail_active=False,
        sl_history=(),
    )
    pieces["positions"].upsert(pos)
    return pos.deal_id


def test_apply_amend_skipped_in_shadow_mode_with_warning_alert(monkeypatch) -> None:
    """H1 layer 2: shadow_mode=True + apply_amend would fire →
    SHADOW_GUARD_BLOCKED warning alert + skip the broker call.

    Reaching this state means layer 1 (bot.main startup guard) was
    bypassed. The layer-2 gate is the safety net.
    """
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    deal_id = _seed_position(pieces)
    pieces["alerter"].sent.clear()
    # Force evaluate_sl_amend to suggest a move so apply_amend would
    # fire if the gate wasn't in place.
    from execution.types import AmendOrder
    monkeypatch.setattr(
        "bot.loop.evaluate_sl_amend",
        lambda position, df_m5, current_price: AmendOrder(
            deal_id=deal_id, new_sl_price=1.30000, reason="trail_active",
        ),
    )
    # Feed a BAR_CLOSE so _run_sl_evaluation runs.
    pieces["feed"].fire(_bar_close("GBPUSD"))
    # Executor.apply_amend was NOT called.
    assert pieces["executor"].amended == []
    # SHADOW_GUARD_BLOCKED alert fired.
    blocked = [a for a in pieces["alerter"].sent
               if a.event_subtype == "SHADOW_GUARD_BLOCKED"]
    assert len(blocked) == 1
    a = blocked[0]
    from alerts import AlertCategory, AlertSeverity
    assert a.severity is AlertSeverity.WARNING
    assert a.category is AlertCategory.SYSTEM
    assert a.pair == "GBPUSD"
    assert "apply_amend" in a.full_text
    assert deal_id in a.full_text
    assert a.debug["operation"] == "apply_amend"


def test_force_close_skipped_in_shadow_mode_with_warning_alert(monkeypatch) -> None:
    """H1 layer 2: shadow_mode=True + force-close would fire →
    SHADOW_GUARD_BLOCKED warning alert + skip the broker call. NO
    TRADE_CLOSED alert (the position is still real at the broker —
    we just refused to act)."""
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=True)
    bot.start()
    bot.mark_ready()
    deal_id = _seed_position(pieces)
    pieces["alerter"].sent.clear()
    from risk.types import ForceCloseOrder
    bot._execute_force_close(ForceCloseOrder(
        position_id=deal_id, pair="GBPUSD", reason="EOD_FLATTEN",
    ))
    # Broker close_position was NOT called.
    assert pieces["ig"].close_calls == []
    # NO TRADE_CLOSED alert.
    closed = [a for a in pieces["alerter"].sent
              if a.event_subtype == "TRADE_CLOSED"]
    assert closed == []
    # NO deal-log entry (we didn't actually close).
    assert deal_id not in bot._recent_closes
    # SHADOW_GUARD_BLOCKED alert fired.
    blocked = [a for a in pieces["alerter"].sent
               if a.event_subtype == "SHADOW_GUARD_BLOCKED"]
    assert len(blocked) == 1
    assert "force_close" in blocked[0].full_text
    assert deal_id in blocked[0].full_text


def test_force_close_normal_mode_with_position_still_works(monkeypatch) -> None:
    """Negative case: shadow_mode=False + force-close → real broker
    call + TRADE_CLOSED alert + deal-log entry. Pins that the layer-2
    gate doesn't accidentally trigger in non-shadow mode."""
    bot, pieces = _build_with_shadow(monkeypatch, shadow_mode=False)
    bot.start()
    bot.mark_ready()
    deal_id = _seed_position(pieces)
    pieces["alerter"].sent.clear()
    from risk.types import ForceCloseOrder
    bot._execute_force_close(ForceCloseOrder(
        position_id=deal_id, pair="GBPUSD", reason="EOD_FLATTEN",
    ))
    # Real broker call.
    assert len(pieces["ig"].close_calls) == 1
    # TRADE_CLOSED alert (NOT SHADOW_GUARD_BLOCKED).
    closed = [a for a in pieces["alerter"].sent
              if a.event_subtype == "TRADE_CLOSED"]
    assert len(closed) == 1
    blocked = [a for a in pieces["alerter"].sent
               if a.event_subtype == "SHADOW_GUARD_BLOCKED"]
    assert blocked == []


def _bar_close(pair: str):
    """Local helper — re-build a minimal BAR_CLOSE event for
    triggering _run_sl_evaluation. Imports inline to avoid polluting
    the module-level imports."""
    from feed.types import Candle, FeedEvent, FeedEventKind
    return FeedEvent(
        kind=FeedEventKind.BAR_CLOSE,
        pair=pair,
        candle=Candle(
            pair=pair,
            close_time=_NOW,
            open=1.3000, high=1.3010, low=1.2990, close=1.3005,
            volume=200.0, source="LS_NATIVE_5M",
        ),
        timestamp=_NOW,
        debug={},
    )
