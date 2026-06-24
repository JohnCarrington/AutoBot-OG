"""Integration tests for risk.guard.RiskGuard (2c rewrite).

2c (B-2/B-3): the guard no longer holds a RegimeEngine reference.
The EOD overnight-hold carve-out is plumbed in at call-time via a
``structure_state_for_pair`` callable.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from day_type import DayType
from common import Direction

from risk.constants import (
    CONSECUTIVE_LOSS_THRESHOLD,
    SPREAD_ABS_CAP_PIPS,
)
from risk.guard import RiskGuard
from risk.news_calendar.calendar import BlackoutResult
from risk.rules import news_blackout as news_blackout_module
from risk.state.circuit_breaker_state import (
    CircuitBreakerState,
    current_session_date_ny,
)
from risk.types import (
    AccountState,
    CandidateTrade,
    MarketSnapshot,
    OpenPosition,
)
from structure_engine import StructureState


# --- Helpers ----------------------------------------------------------------


def _now() -> datetime:
    return datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)


def _stub_no_news(monkeypatch) -> None:
    monkeypatch.setattr(
        news_blackout_module,
        "is_blackout",
        lambda currency, query_time, **_: BlackoutResult(
            is_blocked=False,
            reason="no event in window",
            event_summary=None,
            confidence="low",
        ),
    )


def _stub_news_block(currency: str, monkeypatch) -> None:
    def fake(c, query_time, **_):
        if c == currency:
            return BlackoutResult(
                is_blocked=True,
                reason="high-impact event in window",
                event_summary=f"{currency} CPI @ {query_time.isoformat()}",
                confidence="high",
            )
        return BlackoutResult(
            is_blocked=False,
            reason="no event in window",
            event_summary=None,
            confidence="low",
        )

    monkeypatch.setattr(news_blackout_module, "is_blackout", fake)


def _candidate(
    pair: str = "GBPUSD",
    day_type: DayType = DayType.NORMAL,
    direction: Direction = Direction.BULLISH,
    strategy_name: str = "bb_bounce",
) -> CandidateTrade:
    return CandidateTrade(
        pair=pair,
        intended_direction=direction,
        intended_day_type=day_type,
        planned_entry_price=1.30,
        strategy_name=strategy_name,
    )


def _account(realized: float = 0.0) -> AccountState:
    return AccountState(
        balance=10_000.0, currency="GBP", realized_pnl_today_r=realized
    )


def _market(spread: float = 0.5, atr: float = 20.0) -> MarketSnapshot:
    return MarketSnapshot(current_spread_pips=spread, atr_m5_pips=atr)


def _pos(
    *,
    pid: str = "p1",
    pair: str = "GBPUSD",
    direction: Direction = Direction.BULLISH,
    pnl_r: float = 0.0,
    strategy_name: str = "ema_pullback",
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        pair=pair,
        direction=direction,
        day_type_at_entry=DayType.NORMAL,
        strategy_name=strategy_name,
        entry_price=1.30,
        current_price=1.31,
        entry_time_utc=_now(),
        current_pnl_r=pnl_r,
    )


def _structure(htf_bias: str = "BULLISH") -> StructureState:
    return StructureState(
        pair="GBPUSD",
        timestamp=_now().isoformat(),
        is_valid=True,
        htf_bias=htf_bias,  # type: ignore[arg-type]
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="UNKNOWN",
        confidence=0.5,
        reason="stub",
        levels=[],
        debug={},
    )


def _make_guard(tmp_path: Path) -> RiskGuard:
    state = CircuitBreakerState(path=tmp_path / "cb.json")
    return RiskGuard(state=state)


# --- Happy path ------------------------------------------------------------


def test_allow_entry_passes_all_gates(tmp_path: Path, monkeypatch) -> None:
    _stub_no_news(monkeypatch)
    guard = _make_guard(tmp_path)
    decision = guard.allow_entry(
        candidate=_candidate(day_type=DayType.NORMAL),
        positions=[],
        account=_account(),
        market=_market(),
        now_utc=_now(),
    )
    assert decision.allow is True
    assert decision.rule == "risk_guard"
    pipeline = [step["rule"] for step in decision.debug["pipeline"]]
    assert pipeline == [
        "circuit_breakers",
        "position_caps",
        "news_blackout",
        "spread_filter",
        "eod_enforcement",
    ]


# --- Rule ordering / short-circuit -----------------------------------------


def test_circuit_breakers_short_circuit_blocks_other_rules(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_no_news(monkeypatch)
    guard = _make_guard(tmp_path)
    # Pre-arm a daily DD cooldown to force rejection at step 1.
    guard.state.daily_dd_session_date = current_session_date_ny(_now())
    guard.state.daily_dd_cooldown_until_utc = _now() + timedelta(hours=2)

    decision = guard.allow_entry(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        market=_market(spread=99.0),
        now_utc=_now(),
    )
    assert decision.allow is False
    assert decision.rule == "circuit_breakers"
    rules_executed = [step["rule"] for step in decision.debug["pipeline"]]
    assert rules_executed == ["circuit_breakers"]


def test_position_caps_block_before_news_and_spread(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_news_block("GBP", monkeypatch)
    guard = _make_guard(tmp_path)
    # Same-pair existing position → per-pair cap trips at the same time
    # the same-strategy cap would also trip. Per-pair fires first.
    decision = guard.allow_entry(
        candidate=_candidate(pair="GBPUSD"),
        positions=[_pos(pid="existing", pair="GBPUSD")],
        account=_account(),
        market=_market(spread=99.0),
        now_utc=_now(),
    )
    assert decision.allow is False
    assert decision.rule == "position_caps"
    rules_executed = [step["rule"] for step in decision.debug["pipeline"]]
    assert rules_executed == ["circuit_breakers", "position_caps"]


def test_news_blackout_blocks_before_spread(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_news_block("GBP", monkeypatch)
    guard = _make_guard(tmp_path)
    decision = guard.allow_entry(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        market=_market(spread=99.0),
        now_utc=_now(),
    )
    assert decision.allow is False
    assert decision.rule == "news_blackout"
    rules_executed = [step["rule"] for step in decision.debug["pipeline"]]
    assert "spread_filter" not in rules_executed


def test_spread_filter_blocks_when_only_remaining_check(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_no_news(monkeypatch)
    guard = _make_guard(tmp_path)
    decision = guard.allow_entry(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        market=_market(spread=SPREAD_ABS_CAP_PIPS + 1.0),
        now_utc=_now(),
    )
    assert decision.allow is False
    assert decision.rule == "spread_filter"


def test_eod_suppression_runs_last(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_no_news(monkeypatch)
    guard = _make_guard(tmp_path)
    # 20:45 UTC = 16:45 EDT, 15 min to NY close on a Wednesday → rejected.
    near_close = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    decision = guard.allow_entry(
        candidate=_candidate(day_type=DayType.NORMAL),
        positions=[],
        account=_account(),
        market=_market(),
        now_utc=near_close,
    )
    assert decision.allow is False
    assert decision.rule == "eod_enforcement"


# --- State persistence ----------------------------------------------------


def test_circuit_breaker_state_persists_after_dirty_run(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_no_news(monkeypatch)
    path = tmp_path / "cb.json"
    guard = RiskGuard(state_path=path)
    # Triggering a daily DD writes state to disk.
    guard.allow_entry(
        candidate=_candidate(),
        positions=[_pos(pnl_r=-3.5)],
        account=_account(realized=0.0),
        market=_market(),
        now_utc=_now(),
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["daily_dd_cooldown_until_utc"] is not None


def test_state_not_persisted_when_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    """If no rule marks state dirty, the JSON file is not touched."""
    _stub_no_news(monkeypatch)
    path = tmp_path / "cb.json"
    baseline = CircuitBreakerState(path=path)
    baseline.daily_dd_session_date = current_session_date_ny(_now())
    baseline.save()
    mtime_before = path.stat().st_mtime_ns
    guard = RiskGuard(state_path=path)
    guard.allow_entry(
        candidate=_candidate(day_type=DayType.NORMAL),
        positions=[],
        account=_account(),
        market=_market(),
        now_utc=_now(),
    )
    mtime_after = path.stat().st_mtime_ns
    assert mtime_before == mtime_after


# --- record_trade_outcome --------------------------------------------------


def test_record_trade_outcome_increments_streak_and_persists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cb.json"
    guard = RiskGuard(state_path=path)
    guard.record_trade_outcome(pnl_r=-1.0, closed_at_utc=_now())
    assert guard.state.loss_streak == 1
    data = json.loads(path.read_text())
    assert data["loss_streak"] == 1


def test_record_trade_outcome_arms_consecutive_loss_cooldown(
    tmp_path: Path,
) -> None:
    guard = RiskGuard(state_path=tmp_path / "cb.json")
    for _ in range(CONSECUTIVE_LOSS_THRESHOLD):
        guard.record_trade_outcome(pnl_r=-1.0, closed_at_utc=_now())
    assert guard.state.consecutive_loss_cooldown_until_utc is not None


# --- positions_to_force_close (B-1 / B-3) ---------------------------------


def test_force_close_consults_structure_state_for_pair(tmp_path: Path) -> None:
    """B-1/B-3/2d: the callable provides htf_bias per pair. A BULLISH
    position whose htf_bias is still BULLISH survives the Wed NY close
    regardless of pnl level; a BULLISH position whose htf_bias has
    gone BEARISH closes."""
    guard = RiskGuard(state_path=tmp_path / "cb.json")
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    bias_map = {"GBPUSD": "BULLISH", "EURUSD": "BEARISH"}
    orders = guard.positions_to_force_close(
        positions=[
            _pos(pid="held_high", pair="GBPUSD", pnl_r=2.0,
                 direction=Direction.BULLISH),
            _pos(pid="held_low", pair="GBPUSD", pnl_r=0.2,
                 direction=Direction.BULLISH),
            _pos(pid="flipped", pair="EURUSD", pnl_r=2.0,
                 direction=Direction.BULLISH),
        ],
        now_utc=now,
        structure_state_for_pair=lambda p: _structure(htf_bias=bias_map[p]),
    )
    pids = sorted(o.position_id for o in orders)
    assert pids == ["flipped"]


def test_force_close_when_structure_lookup_returns_none(
    tmp_path: Path,
) -> None:
    """B-1: if structure isn't available for a pair, the EOD rule
    fail-closes — position force-closes."""
    guard = RiskGuard(state_path=tmp_path / "cb.json")
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = guard.positions_to_force_close(
        positions=[_pos(pnl_r=2.0)],
        now_utc=now,
        structure_state_for_pair=lambda _p: None,
    )
    assert len(orders) == 1
    assert "structure_unavailable" in orders[0].reason


def test_force_close_returns_empty_before_close(tmp_path: Path) -> None:
    guard = RiskGuard(state_path=tmp_path / "cb.json")
    orders = guard.positions_to_force_close(
        positions=[_pos()],
        now_utc=_now(),  # 14:00 UTC, before close
        structure_state_for_pair=lambda _p: _structure(),
    )
    assert orders == []


# --- Decision shape -------------------------------------------------------


def test_decision_debug_contains_pipeline_trace(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_no_news(monkeypatch)
    guard = _make_guard(tmp_path)
    decision = guard.allow_entry(
        candidate=_candidate(day_type=DayType.NORMAL),
        positions=[],
        account=_account(),
        market=_market(),
        now_utc=_now(),
    )
    assert "pipeline" in decision.debug
    assert decision.debug["candidate_pair"] == "GBPUSD"
    assert decision.debug["candidate_day_type"] == "NORMAL"
    assert decision.debug["candidate_strategy"] == "bb_bounce"
    for entry in decision.debug["pipeline"]:
        assert set(entry.keys()) == {"rule", "allow", "reason"}
