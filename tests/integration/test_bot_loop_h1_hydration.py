"""Phase B Commit 2 — integration tests for the H1 hydration dispatcher.

These tests exercise :py:meth:`BotLoop._derive_and_enrich_h1` with both
the H1-buffer path (flag-on) and the legacy M5-resample path (flag-off,
``buffer_for_h1`` returns ``None``). They confirm:

1. Flag-on, buffer warmed → classifier-relevant H1 indicators
   (``ema_slope_norm_50_10``, ``bb_width_norm_20_2``) are NON-NaN on
   the first BAR_CLOSE.
2. Flag-off, no H1 buffer → those same indicators are NaN at first
   BAR_CLOSE (proves the fix is real, not noise).
3. Buffer-fed and resample-fed paths produce equivalent H1 indicator
   columns once both are warmed — the parity safety net.

Construction reuses the well-tested fakes from
``tests/unit/test_bot_loop.py`` with one local extension: a
``buffer_for_h1`` accessor on the fake feed.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.loop import BotLoop, _synthesise_h1_from_m5_tail
from feed.rolling_buffer import RollingBuffer
from feed.types import Candle

# Reuse the fake fixtures from the main BotLoop suite — same pattern
# as test_bot_loop_structure_alerts.py.
from tests.unit.test_bot_loop import (
    _FakeBuffer,
    _FakeExecutor,
    _FakeFeed,
    _FakeIGClient,
    _FakePositionManager,
    _FakeRiskGuard,
)


_NOW = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)
_PAIR = "GBPUSD"


# ---------------------------------------------------------------------------
# Local helpers — trending M5 / H1 series for indicator-warmup tests.
# ---------------------------------------------------------------------------


def _trending_m5(open_time: datetime, idx: int) -> Candle:
    """Generate a deterministic up-trending M5 bar.

    Used to seed both the M5 buffer and (via aggregation) the H1
    buffer with a series that produces a non-NaN, positive
    ``ema_slope_norm_50_10`` — i.e. the regime classifier would
    label this as TREND/BULLISH if it saw the full H1 series.
    """
    price = 1.30000 + 0.0010 * idx
    return Candle(
        pair=_PAIR,
        close_time=open_time + timedelta(minutes=5),
        open=price,
        high=price + 0.00050,
        low=price - 0.00050,
        close=price + 0.00020,
        volume=100.0 + (idx % 7),
        source="LS_NATIVE_5M",
    )


def _trending_h1(close_time: datetime, idx: int) -> Candle:
    """Single trending H1 bar — used to seed the H1 buffer directly."""
    price = 1.30000 + 0.0050 * idx
    return Candle(
        pair=_PAIR,
        close_time=close_time,
        open=price,
        high=price + 0.00250,
        low=price - 0.00250,
        close=price + 0.00100,
        volume=1500.0 + (idx % 13),
        source="REST",
    )


def _build_h1_buffer(n_bars: int = 72) -> RollingBuffer:
    """Seed an H1 RollingBuffer with ``n_bars`` trending H1 candles.

    Closes anchored at hour boundaries ending one hour ago so the
    next live M5 BAR_CLOSE produces an in-progress synthesised bar
    rather than colliding with the seeded tail.
    """
    buf = RollingBuffer(_PAIR, capacity=max(n_bars, 60))
    base_close = _NOW.replace(minute=0, second=0, microsecond=0)
    bars = [
        _trending_h1(base_close - timedelta(hours=(n_bars - i)), idx=i)
        for i in range(n_bars)
    ]
    buf.bulk_append(bars)
    return buf


def _build_m5_dataframe(n_bars: int = 100) -> pd.DataFrame:
    """Seed an M5 DataFrame so the legacy resample path has SOME input.

    Anchors to ``_NOW`` so the last M5 close is at _NOW. The shape
    matches what ``_build_m5_dataframe`` in BotLoop returns.
    """
    rows = []
    closes = []
    for i in range(n_bars):
        open_t = _NOW - timedelta(minutes=5 * (n_bars - i))
        bar = _trending_m5(open_t, idx=i)
        rows.append({
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        })
        closes.append(bar.close_time)
    return pd.DataFrame(
        rows, index=pd.DatetimeIndex(closes, name="close_time", tz="UTC"),
    )


class _FakeFeedWithH1(_FakeFeed):
    """Test feed exposing both ``buffer_for`` and ``buffer_for_h1``.

    Production :class:`feed.feed_manager.FeedManager` exposes both
    accessors post-Commit-1; the bare ``_FakeFeed`` in
    ``test_bot_loop.py`` returns ``None`` for ``buffer_for_h1`` (so
    the 1157-baseline tests stay on the legacy path). This subclass
    lets each H1 integration test inject the H1 buffer it needs.
    """

    def __init__(self) -> None:
        super().__init__()
        self._h1_buffers: dict[str, object] = {}

    def set_h1_buffer(self, pair: str, buf) -> None:
        self._h1_buffers[pair] = buf

    def buffer_for_h1(self, pair: str):
        return self._h1_buffers.get(pair)


def _build_bot(
    monkeypatch,
    *,
    h1_buffer: object = None,
    pairs: tuple[str, ...] = (_PAIR,),
) -> BotLoop:
    """Minimal BotLoop with one pair and the H1-aware fake feed."""
    feed = _FakeFeedWithH1()
    # Seed the M5 buffer with enough trending bars so the legacy
    # resample path produces some H1 (and the indicator stack has
    # enough M5 history for its own warm-up).
    seed_m5 = [
        _trending_m5(_NOW - timedelta(minutes=5 * (100 - i)), idx=i)
        for i in range(100)
    ]
    for pair in pairs:
        feed.add_buffer(pair, seed_m5)
        if h1_buffer is not None:
            feed.set_h1_buffer(pair, h1_buffer)

    ig = _FakeIGClient()
    executor = _FakeExecutor()
    pm = _FakePositionManager()
    rg = _FakeRiskGuard()

    import bot.loop as loop_mod
    monkeypatch.setattr(
        loop_mod, "fetch_market_info",
        lambda session, epic: type(
            "MI", (), {"bid": 1.30000, "offer": 1.30020},
        )(),
    )

    return BotLoop(
        feed_manager=feed,        # type: ignore[arg-type]
        ig_client=ig,             # type: ignore[arg-type]
        executor=executor,        # type: ignore[arg-type]
        risk_guard=rg,            # type: ignore[arg-type]
        position_manager=pm,      # type: ignore[arg-type]
        pairs=tuple(pairs),
        pair_to_epic={p: f"CS.D.{p}.TODAY.IP" for p in pairs},
        clock=lambda: _NOW,
    )


# ---------------------------------------------------------------------------
# Test 1 — flag-on: H1 buffer warmed, classifier-relevant indicators non-NaN
# ---------------------------------------------------------------------------


def test_flag_on_classifier_indicators_non_nan_on_first_bar(monkeypatch) -> None:
    """With a warmed H1 buffer (>= FEED_H1_MIN_USABLE_BARS), the
    dispatcher returns an H1 frame whose ``ema_slope_norm_50_10`` and
    ``bb_width_norm_20_2`` columns are NON-NaN — i.e. the regime
    classifier would NOT return ``insufficient_indicator_data``.
    """
    h1_buf = _build_h1_buffer(n_bars=72)
    bot = _build_bot(monkeypatch, h1_buffer=h1_buf)

    df_m5 = _build_m5_dataframe(n_bars=100)
    # Mid-hour M5 close to exercise the trim branch (most realistic case).
    mid_hour = _NOW.replace(minute=35, second=0, microsecond=0)
    df_h1 = bot._derive_and_enrich_h1(  # type: ignore[attr-defined]
        df_m5, m5_close_time=mid_hour, pair=_PAIR,
    )

    # Trim removes the latest bar → 71 rows from a 72-bar buffer.
    assert len(df_h1) == 71
    # The two classifier inputs:
    last = df_h1.iloc[-1]
    assert not math.isnan(last["ema_slope_norm_50_10"]), (
        "flag-on path must produce a warmed ema_slope — the H1 buffer "
        "has 71 bars after trim which is >> the 60-bar classifier minimum"
    )
    assert not math.isnan(last["bb_width_norm_20_2"])


# ---------------------------------------------------------------------------
# Test 2 — flag-off: no H1 buffer → resample-derived frame is under-warmed
# ---------------------------------------------------------------------------


def test_flag_off_classifier_indicators_nan_on_first_bar(monkeypatch) -> None:
    """Without an H1 buffer the dispatcher falls back to the legacy
    M5 resample. With 100 M5 bars (~8.3 H1 bars), the resampled H1
    series is too short for the EMA-50 to warm up and the slope/
    bb_width columns come back NaN — the exact failure mode Phase B
    is fixing.
    """
    bot = _build_bot(monkeypatch, h1_buffer=None)
    df_m5 = _build_m5_dataframe(n_bars=100)
    mid_hour = _NOW.replace(minute=35, second=0, microsecond=0)

    df_h1 = bot._derive_and_enrich_h1(  # type: ignore[attr-defined]
        df_m5, m5_close_time=mid_hour, pair=_PAIR,
    )

    # Resampled H1 is short (≤ 9 rows from 100 M5 = ~8.3 H1).
    assert len(df_h1) <= 9
    if not df_h1.empty:
        last = df_h1.iloc[-1]
        # EMA-50 needs 50 H1 bars to warm — under-warmed series → NaN.
        assert math.isnan(last["ema_slope_norm_50_10"]), (
            "flag-off path with <50 H1 bars MUST produce NaN slope "
            "(this is the bug we're fixing)"
        )


# ---------------------------------------------------------------------------
# Test 3 — parity: buffer-fed and resample-fed paths agree once both are warmed
# ---------------------------------------------------------------------------


def test_h1_buffer_vs_resample_parity_after_warmup(monkeypatch) -> None:
    """When the H1 buffer was populated by ``_synthesise_h1_from_m5_tail``
    from the same M5 history that the resample path consumes, the two
    dispatchers produce equivalent OHLC for the trailing window.

    This is the safety net: the new path must not drift from the
    legacy one. Indicator columns can differ in the early warmup
    bars (different histories produce different EMA seed values),
    but the OHLC columns are pure aggregations and must match
    exactly on the bars that overlap.
    """
    # Build a long M5 history — 200 hours = 2400 M5 bars.
    long_m5: list[Candle] = []
    n_m5 = 200 * 12
    for i in range(n_m5):
        open_t = _NOW - timedelta(minutes=5 * (n_m5 - i))
        long_m5.append(_trending_m5(open_t, idx=i))
    m5_buf = RollingBuffer(_PAIR, capacity=n_m5)
    m5_buf.bulk_append(long_m5)

    # Populate the H1 buffer by replaying _synthesise_h1_from_m5_tail
    # for every fully-formed hour the M5 history contains.
    h1_buf = RollingBuffer(_PAIR, capacity=200)
    first_hour = (long_m5[0].close_time - timedelta(minutes=5)).replace(
        minute=0, second=0, microsecond=0,
    )
    # The last fully-formed hour is the one ending at _NOW (M5 close
    # at _NOW had open at _NOW - 5min, which floors to _NOW - 1h).
    last_hour_start = _NOW - timedelta(hours=1)
    cur = first_hour
    while cur <= last_hour_start:
        synth = _synthesise_h1_from_m5_tail(m5_buf, cur)
        if synth is not None:
            h1_buf.push(synth)
        cur += timedelta(hours=1)

    bot = _build_bot(monkeypatch, h1_buffer=h1_buf)
    # Override the M5 buffer with the long one so the resample path
    # has matching input.
    bot._feed._buffers[_PAIR] = _FakeBuffer(long_m5)  # type: ignore[attr-defined]

    df_m5 = bot._build_m5_dataframe(_PAIR)  # type: ignore[attr-defined]
    on_boundary = _NOW  # minute == 0 → no trim, both paths include the latest
    assert on_boundary.minute == 0

    # Flag-on path
    df_buffer_fed = bot._derive_and_enrich_h1(  # type: ignore[attr-defined]
        df_m5, m5_close_time=on_boundary, pair=_PAIR,
    )
    # Flag-off path (pair=None routes to legacy)
    df_resample_fed = bot._derive_and_enrich_h1(  # type: ignore[attr-defined]
        df_m5, m5_close_time=on_boundary, pair=None,
    )

    # Trailing 50 H1 bars overlap both frames; OHLC must match.
    tail_buffer = df_buffer_fed.tail(50)
    tail_resample = df_resample_fed.tail(50)
    # check_freq=False: the resample path produces an index with
    # freq='h' attached; the buffer-fed path does not (RollingBuffer
    # just emits a plain DatetimeIndex of close times). Both indexes
    # are identical *values* — only the cached freq attribute
    # differs. Strategies consume the index as timestamps, not as a
    # freq-aware range, so this is cosmetic.
    # Compare index timestamps directly (skip the freq attribute).
    assert list(tail_buffer.index) == list(tail_resample.index)
    for col in ("open", "high", "low", "close", "volume"):
        pd.testing.assert_series_equal(
            tail_buffer[col], tail_resample[col],
            check_exact=False, atol=1e-9, rtol=1e-9,
            check_freq=False,
        )
    # Indicators have to match too once both paths are warmed.
    for col in ("ema_slope_norm_50_10", "bb_width_norm_20_2"):
        # Skip leading NaNs which may differ if the two histories
        # have different lengths — compare only rows where both are
        # populated.
        both_valid = (
            tail_buffer[col].notna() & tail_resample[col].notna()
        )
        assert both_valid.any(), f"expected at least some valid {col} rows"
        pd.testing.assert_series_equal(
            tail_buffer.loc[both_valid, col],
            tail_resample.loc[both_valid, col],
            check_exact=False, atol=1e-6, rtol=1e-6,
            check_freq=False,
        )
