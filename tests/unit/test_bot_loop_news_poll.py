"""Tests for step 4.5 — calendar polling wired into BAR_CLOSE pipeline.

Pre-step-4.5 recon: ``poll_for_actual`` was never called from ``bot/``,
so the news-calendar cache was always empty in production.
``classify_day_type`` therefore always fail-closed to ``BIG_NEWS_DAY``
and ``is_blackout`` always fail-closed to a "cache-stale" block. This
step wires the poll into ``_handle_bar_close`` so the cache refreshes
on every BAR_CLOSE before any consumer reads it.

These tests pin the three behaviours that matter for ops:
  1. The poll runs on every BAR_CLOSE, BEFORE the day-type classifier.
  2. A poll exception does NOT crash the bar — the handler continues
     and ``classify_day_type`` still runs (fail-closing on a stale cache
     is the existing, correct behaviour).
  3. The bot loop calls ``poll_for_actual`` unconditionally — throttling
     is the polling function's own responsibility (verified in
     ``test_news_calendar_calendar.py``).
"""
from __future__ import annotations

from tests.unit.test_bot_loop import _bar_close, _build  # type: ignore[import-not-found]


def test_poll_for_actual_called_on_bar_close(monkeypatch) -> None:
    """A BAR_CLOSE event triggers a poll_for_actual call."""
    bot, pieces = _build(monkeypatch)
    import bot.loop as loop_mod

    poll_calls: list[None] = []
    monkeypatch.setattr(
        loop_mod, "poll_for_actual", lambda: poll_calls.append(None),
    )

    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))

    assert len(poll_calls) == 1


def test_poll_for_actual_called_before_classify_day_type(monkeypatch) -> None:
    """Poll must precede classify_day_type so the cache is fresh."""
    bot, pieces = _build(monkeypatch)
    import bot.loop as loop_mod

    call_order: list[str] = []
    monkeypatch.setattr(
        loop_mod, "poll_for_actual", lambda: call_order.append("poll"),
    )

    real_classify = loop_mod.classify_day_type

    def _spy(**kwargs):
        call_order.append("classify")
        return real_classify(**kwargs)

    monkeypatch.setattr(loop_mod, "classify_day_type", _spy)

    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))

    assert "poll" in call_order
    assert "classify" in call_order
    assert call_order.index("poll") < call_order.index("classify"), (
        f"poll_for_actual must run before classify_day_type; got {call_order}"
    )


def test_poll_for_actual_failure_does_not_crash_bar(monkeypatch) -> None:
    """A poll exception is swallowed; classify_day_type still runs."""
    bot, pieces = _build(monkeypatch)
    import bot.loop as loop_mod

    def _boom() -> None:
        raise RuntimeError("simulated Finnhub network failure")

    classify_calls: list[None] = []
    real_classify = loop_mod.classify_day_type

    def _spy(**kwargs):
        classify_calls.append(None)
        return real_classify(**kwargs)

    monkeypatch.setattr(loop_mod, "poll_for_actual", _boom)
    monkeypatch.setattr(loop_mod, "classify_day_type", _spy)

    bot.start()
    bot.mark_ready()
    # Must not raise — bar handler swallows the poll error.
    pieces["feed"].fire(_bar_close("GBPUSD"))

    # classify_day_type ran despite the poll failure — fail-closed
    # behaviour applies on a stale cache, which is the existing
    # correct posture.
    assert classify_calls, (
        "classify_day_type must still run after a poll failure"
    )


def test_poll_called_unconditionally_throttle_lives_in_poll_fn(monkeypatch) -> None:
    """Loop calls poll_for_actual every BAR_CLOSE — throttle is the poll's job.

    The min_interval throttle inside ``poll_for_actual`` (covered by
    ``test_news_calendar_calendar.py``) decides whether each call
    actually fetches. The loop's contract is just "call it every bar."
    """
    bot, pieces = _build(monkeypatch)
    import bot.loop as loop_mod

    poll_calls: list[None] = []
    monkeypatch.setattr(
        loop_mod, "poll_for_actual", lambda: poll_calls.append(None),
    )

    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))
    pieces["feed"].fire(_bar_close("GBPUSD", offset_min=1))
    pieces["feed"].fire(_bar_close("GBPUSD", offset_min=2))

    assert len(poll_calls) == 3
