"""Tests for the ``alerts`` package init wiring.

Currently covers N1 from the Phase 9 adversarial review — the
``logging.Filter`` installed on the ``alerts.*`` loggers to scrub
``/bot{token}/`` URL segments from any log record before it reaches a
handler. This is belt-and-braces over the explicit scrubbing inside
``TelegramClient.send``: if a future caller forgets to pre-scrub,
the filter catches the leak.
"""
from __future__ import annotations

import logging

import requests

import alerts
from alerts import _SCRUBBED_LOGGER_NAMES, _TokenScrubFilter


def _scrubbed_logger_filter_count(name: str) -> int:
    """How many _TokenScrubFilter instances are on a given logger."""
    return sum(
        1 for f in logging.getLogger(name).filters
        if isinstance(f, _TokenScrubFilter)
    )


def test_filter_installed_on_each_alerts_logger() -> None:
    """N1: every alerts.* logger named in _SCRUBBED_LOGGER_NAMES has
    exactly one _TokenScrubFilter installed."""
    for name in _SCRUBBED_LOGGER_NAMES:
        assert _scrubbed_logger_filter_count(name) == 1, (
            f"logger {name!r} should have exactly one token-scrub filter"
        )


def test_filter_install_is_idempotent() -> None:
    """Calling the installer again must not double-install."""
    alerts._install_token_scrub_filter()
    alerts._install_token_scrub_filter()
    for name in _SCRUBBED_LOGGER_NAMES:
        # Still exactly one filter, never two.
        assert _scrubbed_logger_filter_count(name) == 1


def test_filter_redacts_token_in_log_message(caplog) -> None:
    """N1 behaviour: a record whose message contains ``/bot<token>/``
    has the token replaced with ``<redacted>`` before reaching the
    handler."""
    log = logging.getLogger("alerts.telegram_client")
    with caplog.at_level(logging.WARNING, logger="alerts.telegram_client"):
        log.warning(
            "ConnectionError on https://api.telegram.org/bot1234:ABCDEF/sendMessage"
        )
    relevant = [r for r in caplog.records if "ConnectionError" in r.getMessage()]
    assert len(relevant) == 1
    msg = relevant[0].getMessage()
    assert "1234:ABCDEF" not in msg
    assert "/bot<redacted>/" in msg


def test_filter_handles_args_style_log_calls(caplog) -> None:
    """The filter pre-formats record.getMessage() and clears args
    so a token embedded in a ``%s`` arg is also scrubbed."""
    log = logging.getLogger("alerts.alerter")
    with caplog.at_level(logging.WARNING, logger="alerts.alerter"):
        log.warning(
            "Telegram delivery failed: %s",
            "POST /bot999:SECRET/sendMessage returned 500",
        )
    relevant = [
        r for r in caplog.records if "delivery failed" in r.getMessage()
    ]
    assert len(relevant) == 1
    msg = relevant[0].getMessage()
    assert "999:SECRET" not in msg
    assert "/bot<redacted>/" in msg


def test_filter_leaves_non_token_messages_untouched(caplog) -> None:
    """A record that doesn't contain a /bot{token}/ pattern passes
    through unchanged — the filter must not eat or mangle normal
    log output."""
    log = logging.getLogger("alerts.coalescer")
    with caplog.at_level(logging.WARNING, logger="alerts.coalescer"):
        log.warning("pending_count=3 elapsed=42s")
    relevant = [
        r for r in caplog.records if "pending_count" in r.getMessage()
    ]
    assert len(relevant) == 1
    assert relevant[0].getMessage() == "pending_count=3 elapsed=42s"


def test_filter_does_not_drop_records(caplog) -> None:
    """The filter must always return True — no record dropping."""
    log = logging.getLogger("alerts.formatter")
    f = _TokenScrubFilter()
    record = log.makeRecord(
        name="alerts.formatter",
        level=logging.INFO,
        fn="x", lno=0, msg="hello", args=(), exc_info=None,
    )
    assert f.filter(record) is True


def test_filter_not_installed_on_root_logger() -> None:
    """Scoping is alerts.* only — installing on root would touch
    every record in the process. Sanity-check that we haven't
    accidentally widened the blast radius."""
    root_filters = [
        f for f in logging.getLogger().filters
        if isinstance(f, _TokenScrubFilter)
    ]
    assert root_filters == []


def test_token_scrubbed_from_exception_traceback(caplog) -> None:
    """H1 (Session-3 re-review): logger.exception paths must also
    be scrubbed. The original N1 implementation only touched
    record.getMessage(), leaving the traceback (rendered lazily
    into record.exc_text) unredacted. This test pins that a
    requests.exceptions.ConnectionError carrying a token in the
    URL does not leak that token via the traceback string."""
    log = logging.getLogger("alerts.alerter")
    try:
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded with url: "
            "/bot12345:LEAK_VIA_TRACEBACK/sendMessage"
        )
    except requests.exceptions.ConnectionError:
        with caplog.at_level(logging.ERROR, logger="alerts.alerter"):
            log.exception("Network call failed")

    assert caplog.records, "expected at least one captured log record"
    for record in caplog.records:
        full_text = record.getMessage() + " " + (record.exc_text or "")
        assert "LEAK_VIA_TRACEBACK" not in full_text, (
            f"Token leaked in record: msg={record.getMessage()!r} "
            f"exc_text={record.exc_text!r}"
        )
        if record.exc_text:
            assert "<redacted>" in record.exc_text
