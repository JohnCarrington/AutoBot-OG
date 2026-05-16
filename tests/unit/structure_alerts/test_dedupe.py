"""Tests for structure_alerts.dedupe.DedupeCache.

The pathological cases pinned in the C-3 brief
(:py:func:`test_same_reaction_at_different_levels_both_fire_within_cooldown`
and
:py:func:`test_same_reaction_at_same_level_within_cooldown_blocks_second`)
codify the reaction-event dedupe-key invariant: keys MUST include
both the event subtype AND the quantised level price so different
levels of the same reaction type don't suppress each other.

The trigger layer (C-2) already constructs keys with the level price
embedded; these tests verify the end-to-end behaviour through the
dedupe gate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alerts import AlertSeverity
from structure_alerts.dedupe import DedupeCache


_T0 = datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pinned pathological cases (from the C-3 brief)
# ---------------------------------------------------------------------------


def test_same_reaction_at_different_levels_both_fire_within_cooldown() -> None:
    """Two CRITICAL support breaks on different levels within 30 min
    must both fire. Locked dedupe-key shape:
    ``GBPUSD_SUPPORT_ACCEPTANCE_{Q(price)}`` — different Q means
    different keys, so the cache tracks them independently.
    """
    cache = DedupeCache()
    assert cache.should_fire(
        "GBPUSD_SUPPORT_ACCEPTANCE_13340",
        AlertSeverity.CRITICAL,
        now=_T0,
    ) is True
    assert cache.should_fire(
        "GBPUSD_SUPPORT_ACCEPTANCE_13280",
        AlertSeverity.CRITICAL,
        now=_T0 + timedelta(minutes=1),
    ) is True
    # Both keys recorded.
    assert cache.size == 2


def test_same_reaction_at_same_level_within_cooldown_blocks_second() -> None:
    """Same CRITICAL key fires once, blocks at 29m59s, fires again at
    30m01s. Pins the 30-minute CRITICAL cooldown boundary against the
    locked dedupe-key shape ``GBPUSD_SUPPORT_ACCEPTANCE_13340``.
    """
    cache = DedupeCache()
    key = "GBPUSD_SUPPORT_ACCEPTANCE_13340"
    assert cache.should_fire(key, AlertSeverity.CRITICAL, now=_T0) is True
    assert cache.should_fire(
        key,
        AlertSeverity.CRITICAL,
        now=_T0 + timedelta(minutes=29, seconds=59),
    ) is False
    assert cache.should_fire(
        key,
        AlertSeverity.CRITICAL,
        now=_T0 + timedelta(minutes=30, seconds=1),
    ) is True


# ---------------------------------------------------------------------------
# Per-severity cooldown boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "severity,cooldown_seconds",
    [
        (AlertSeverity.INFO, 2 * 3600),
        (AlertSeverity.WARNING, 3600),
        (AlertSeverity.CRITICAL, 30 * 60),
    ],
)
def test_cooldown_boundary_per_severity(severity, cooldown_seconds) -> None:
    """For every severity: fire at t=0, block at cooldown-1s, fire at
    cooldown+1s. The boundary itself (exactly cooldown) is also
    fireable — :meth:`should_fire` uses ``elapsed < cooldown`` so
    equality is eligible.
    """
    cache = DedupeCache()
    key = f"K_{severity.value}"
    assert cache.should_fire(key, severity, now=_T0) is True
    assert cache.should_fire(
        key, severity, now=_T0 + timedelta(seconds=cooldown_seconds - 1),
    ) is False
    assert cache.should_fire(
        key, severity, now=_T0 + timedelta(seconds=cooldown_seconds),
    ) is True
    # The +cooldown call also recorded, so a +cooldown+1s call within
    # the new window blocks.
    assert cache.should_fire(
        key, severity, now=_T0 + timedelta(seconds=cooldown_seconds + 1),
    ) is False


def test_info_2h_boundary() -> None:
    """Explicit 2-hour INFO boundary — 1h59m blocked, 2h01m allowed."""
    cache = DedupeCache()
    key = "GBPUSD_NEW_LEVEL_SUPPORT_13020"
    assert cache.should_fire(key, AlertSeverity.INFO, now=_T0) is True
    assert cache.should_fire(
        key, AlertSeverity.INFO, now=_T0 + timedelta(hours=1, minutes=59),
    ) is False
    assert cache.should_fire(
        key, AlertSeverity.INFO, now=_T0 + timedelta(hours=2, minutes=1),
    ) is True


def test_warning_1h_boundary() -> None:
    """Explicit 1-hour WARNING boundary — 59m blocked, 61m allowed."""
    cache = DedupeCache()
    key = "GBPUSD_HTF_BIAS_BEARISH"
    assert cache.should_fire(key, AlertSeverity.WARNING, now=_T0) is True
    assert cache.should_fire(
        key, AlertSeverity.WARNING, now=_T0 + timedelta(minutes=59),
    ) is False
    assert cache.should_fire(
        key, AlertSeverity.WARNING, now=_T0 + timedelta(minutes=61),
    ) is True


# ---------------------------------------------------------------------------
# Standard suite
# ---------------------------------------------------------------------------


def test_empty_cache_any_key_fires() -> None:
    cache = DedupeCache()
    assert cache.should_fire("anything", AlertSeverity.INFO, now=_T0) is True
    assert cache.size == 1


def test_distinct_keys_do_not_interfere() -> None:
    cache = DedupeCache()
    keys = [
        ("GBPUSD_HTF_BIAS_BULLISH", AlertSeverity.WARNING),
        ("EURUSD_HTF_BIAS_BEARISH", AlertSeverity.WARNING),
        ("GBPUSD_MODE_TREND_CONTINUATION", AlertSeverity.WARNING),
        ("GBPUSD_NEW_LEVEL_SUPPORT_13020", AlertSeverity.INFO),
    ]
    for key, sev in keys:
        assert cache.should_fire(key, sev, now=_T0) is True
    assert cache.size == 4
    # Each blocks itself within window.
    for key, sev in keys:
        assert cache.should_fire(
            key, sev, now=_T0 + timedelta(seconds=30),
        ) is False


def test_clear_resets_cache_and_keys_refire() -> None:
    cache = DedupeCache()
    cache.should_fire("k1", AlertSeverity.WARNING, now=_T0)
    cache.should_fire("k2", AlertSeverity.INFO, now=_T0)
    assert cache.size == 2
    cache.clear()
    assert cache.size == 0
    # Both keys eligible again at t=0+30s.
    assert cache.should_fire(
        "k1", AlertSeverity.WARNING, now=_T0 + timedelta(seconds=30),
    ) is True
    assert cache.should_fire(
        "k2", AlertSeverity.INFO, now=_T0 + timedelta(seconds=30),
    ) is True


def test_blocked_attempt_does_not_reset_cooldown_clock() -> None:
    """A False return from should_fire MUST NOT update the timer —
    otherwise a key fired at t=0 with retries at t=15min (blocked)
    would push the next eligibility to t=30min from the retry, not
    from the original fire. Critical for the operator-paging contract.
    """
    cache = DedupeCache()
    key = "GBPUSD_SUPPORT_ACCEPTANCE_13340"
    cache.should_fire(key, AlertSeverity.CRITICAL, now=_T0)
    # Blocked at t=15min.
    cache.should_fire(
        key, AlertSeverity.CRITICAL, now=_T0 + timedelta(minutes=15),
    )
    # Still eligible at t=30m01s from the ORIGINAL fire (not from
    # the t=15m blocked attempt).
    assert cache.should_fire(
        key, AlertSeverity.CRITICAL, now=_T0 + timedelta(minutes=30, seconds=1),
    ) is True


def test_last_fired_read_only() -> None:
    cache = DedupeCache()
    assert cache.last_fired("never_seen") is None
    cache.should_fire("k", AlertSeverity.INFO, now=_T0)
    assert cache.last_fired("k") == _T0
    # last_fired does not mutate.
    assert cache.last_fired("k") == _T0


# ---------------------------------------------------------------------------
# Clock skew
# ---------------------------------------------------------------------------


def test_backwards_clock_jump_fires_rather_than_silently_blocks() -> None:
    """If NTP corrects a forward-jumped clock, a same-key event arriving
    with an EARLIER timestamp than the recorded one falls through to
    fire rather than being blocked until wall-clock catches up.
    Matches Phase 9 AlertCoalescer M2 from the Session-3 review.
    """
    cache = DedupeCache()
    key = "GBPUSD_HTF_BIAS_BEARISH"
    # Fired in the "future" (clock was wrong).
    cache.should_fire(
        key, AlertSeverity.WARNING, now=_T0 + timedelta(minutes=30),
    )
    # NTP corrects: now is earlier than the recorded fire time.
    assert cache.should_fire(
        key, AlertSeverity.WARNING, now=_T0,
    ) is True


# ---------------------------------------------------------------------------
# Severity routing
# ---------------------------------------------------------------------------


def test_same_key_different_severity_uses_severity_specific_cooldown() -> None:
    """Severity is read at each call (not bound at first fire) — so a
    pathological caller that fires the same key as INFO then later as
    CRITICAL would use CRITICAL's 30m cooldown for the comparison.

    In practice the trigger layer derives severity from kind via
    severity_for(kind), and dedupe keys embed kind-specific tokens —
    so this case shouldn't happen in production. The test pins the
    semantic anyway so a future refactor of the cooldown lookup
    doesn't silently change behaviour.
    """
    cache = DedupeCache()
    key = "weird_shared_key"
    cache.should_fire(key, AlertSeverity.INFO, now=_T0)
    # 31 minutes later — past CRITICAL's 30m, well inside INFO's 2h.
    # The next fire passes severity=CRITICAL; comparison uses 30m
    # cooldown, so it fires.
    assert cache.should_fire(
        key, AlertSeverity.CRITICAL, now=_T0 + timedelta(minutes=31),
    ) is True
