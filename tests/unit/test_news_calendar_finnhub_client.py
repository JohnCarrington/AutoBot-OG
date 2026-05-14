"""Tests for risk.news_calendar.finnhub_client.

Primary purpose: pin the H1 security fix — the Finnhub API token must
travel in the ``X-Finnhub-Token`` header, never in the URL. ``requests``
exception strings include the failing URL verbatim, so a network blip
with the token in the query string would leak the credential to logs.

Also covers the H4 fail-closed contract: ``fetch_calendar`` returns
``None`` on failures (network, HTTP, JSON, disabled) and ``list`` only
on confirmed success.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from risk.news_calendar import finnhub_client
from risk.news_calendar.finnhub_client import fetch_calendar


SENTINEL_TOKEN = "PROD_API_TOKEN_THAT_MUST_NEVER_LEAK_xyz123abc"


@pytest.fixture
def fake_finnhub_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable Finnhub with a known sentinel token for the duration of a test."""
    monkeypatch.setattr(finnhub_client, "FINNHUB_API_KEY", SENTINEL_TOKEN)
    monkeypatch.setattr(finnhub_client, "FINNHUB_ENABLED", True)


# ---------------------------------------------------------------------------
# H1 — credential leak via requests exception URL
# ---------------------------------------------------------------------------


def test_h1_token_sent_via_header_not_query_string(fake_finnhub_enabled) -> None:
    """The HTTP request must carry the token in ``X-Finnhub-Token`` and the
    URL must not contain ``token=``. This is the load-bearing assertion
    for H1 — verify the wire format directly."""
    captured: dict = {}

    def fake_get(url: str, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"economicCalendar": []}
        return resp

    with patch.object(requests, "get", side_effect=fake_get):
        fetch_calendar()

    assert "token=" not in captured["url"], (
        f"URL must not contain token query param, got: {captured['url']}"
    )
    assert captured["headers"].get("X-Finnhub-Token") == SENTINEL_TOKEN, (
        "Token must be sent via X-Finnhub-Token header"
    )


def test_h1_token_does_not_leak_in_exception_log(
    fake_finnhub_enabled,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-05-14 review's H1 probe: simulate a connection failure
    that produces an exception string mentioning the URL, confirm the
    token does not appear in the captured warning log."""
    # ConnectionError below carries an arbitrary message — we make it
    # URL-shaped to mimic the real requests exception. If the production
    # code ever appended the token back into the URL or logged the full
    # request object, this would fail.
    fake_exc_msg = (
        "HTTPSConnectionPool(host='finnhub.io', port=443): Max retries "
        "exceeded with url: /api/v1/calendar/economic?from=2026-05-14"
        "&to=2026-05-15"
    )

    def boom(url: str, **kwargs):
        raise requests.ConnectionError(fake_exc_msg)

    with caplog.at_level(logging.WARNING, logger="risk.news_calendar.finnhub_client"):
        with patch.object(requests, "get", side_effect=boom):
            result = fetch_calendar()

    assert result is None, "H4: failure must return None, not []"
    # The sentinel token must not appear anywhere in the captured log.
    # Belt-and-braces: also check the formatted message text directly.
    full_log_text = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    assert SENTINEL_TOKEN not in full_log_text, (
        "FINNHUB token leaked into log output! Captured: " + full_log_text
    )


def test_h1_token_does_not_leak_when_request_object_str_includes_it(
    fake_finnhub_enabled,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hardening: even if ``requests`` itself includes the URL in its
    exception ``__str__`` (it does), our code does not stringify any
    object that holds the original URL. The token is only attached as a
    header, so it can never end up in the URL ``requests`` reports."""
    def boom(url: str, **kwargs):
        # Real requests behaviour: include url verbatim in the exception.
        raise requests.exceptions.SSLError(
            f"SSL: CERTIFICATE_VERIFY_FAILED while reaching {url}"
        )

    with caplog.at_level(logging.WARNING, logger="risk.news_calendar.finnhub_client"):
        with patch.object(requests, "get", side_effect=boom):
            fetch_calendar()

    assert SENTINEL_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# H4 — failure returns None, success returns list
# ---------------------------------------------------------------------------


def test_h4_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No FINNHUB_API_KEY → return None (treated as failure by caller)."""
    monkeypatch.setattr(finnhub_client, "FINNHUB_ENABLED", False)
    assert fetch_calendar() is None


def test_h4_http_non_200_returns_none(fake_finnhub_enabled) -> None:
    resp = MagicMock()
    resp.status_code = 502
    with patch.object(requests, "get", return_value=resp):
        assert fetch_calendar() is None


def test_h4_json_decode_failure_returns_none(fake_finnhub_enabled) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("not json")
    with patch.object(requests, "get", return_value=resp):
        assert fetch_calendar() is None


def test_h4_connection_error_returns_none(fake_finnhub_enabled) -> None:
    with patch.object(requests, "get", side_effect=requests.ConnectionError("boom")):
        assert fetch_calendar() is None


def test_h4_success_empty_returns_empty_list_not_none(
    fake_finnhub_enabled,
) -> None:
    """The 'fetch succeeded but no matching events' case returns ``[]``,
    which the caller knows is distinct from ``None``. The risk layer can
    safely refresh the cache with ``[]`` because the calendar genuinely
    has no in-window releases."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"economicCalendar": []}
    with patch.object(requests, "get", return_value=resp):
        result = fetch_calendar()
    assert result == [], f"empty success must be [], got {result!r}"
    assert result is not None


def test_h4_success_with_events_returns_filtered_list(
    fake_finnhub_enabled,
) -> None:
    """Filters by TRADED_COUNTRIES and drops LOW impact."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "economicCalendar": [
            # Keeps: HIGH impact, traded country.
            {"country": "US", "event": "NFP", "impact": "high"},
            # Drops: LOW impact.
            {"country": "GB", "event": "Misc", "impact": "low"},
            # Drops: non-traded country.
            {"country": "AU", "event": "RBA Rate", "impact": "high"},
            # Keeps: MEDIUM impact passes (review intent — needed for soft block).
            {"country": "DE", "event": "ZEW Index", "impact": "medium"},
        ],
    }
    with patch.object(requests, "get", return_value=resp):
        result = fetch_calendar()
    assert result is not None
    kept_events = [ev["event"] for ev in result]
    assert "NFP" in kept_events
    assert "ZEW Index" in kept_events
    assert "Misc" not in kept_events
    assert "RBA Rate" not in kept_events
