"""Tests for alerts.telegram_client — TelegramClient HTTP shim."""
from __future__ import annotations

import logging
from typing import Any

import pytest

from alerts.telegram_client import TelegramClient


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _fake_post_factory(status_code: int):
    calls: list = []

    def fake_post(url, *, data=None, timeout=None):
        calls.append({"url": url, "data": data, "timeout": timeout})
        return _FakeResponse(status_code)

    fake_post.calls = calls  # type: ignore[attr-defined]
    return fake_post


def test_send_success_returns_true() -> None:
    post = _fake_post_factory(200)
    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=post)
    assert client.send("hello") is True
    assert len(post.calls) == 1  # type: ignore[attr-defined]
    call = post.calls[0]  # type: ignore[attr-defined]
    assert "TOK/sendMessage" in call["url"]
    assert call["data"] == {"chat_id": "CHAT", "text": "hello"}


def test_send_4xx_returns_false_and_logs_warning(caplog) -> None:
    post = _fake_post_factory(400)
    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=post)
    with caplog.at_level(logging.WARNING, logger="alerts.telegram_client"):
        assert client.send("bad") is False
    assert any("400" in r.getMessage() for r in caplog.records)


def test_send_5xx_returns_false() -> None:
    post = _fake_post_factory(503)
    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=post)
    assert client.send("test") is False


def test_send_timeout_returns_false(caplog) -> None:
    def boom(*a, **kw):
        raise TimeoutError("upstream timed out")

    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=boom)
    with caplog.at_level(logging.WARNING, logger="alerts.telegram_client"):
        assert client.send("hi") is False
    assert any("TimeoutError" in r.getMessage() for r in caplog.records)


def test_send_network_error_returns_false() -> None:
    def boom(*a, **kw):
        raise ConnectionError("no route to host")

    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=boom)
    assert client.send("hi") is False


def test_send_never_raises_for_any_exception() -> None:
    """The contract: alerts are observability, never propagate up."""
    class _Weird(Exception):
        pass

    def boom(*a, **kw):
        raise _Weird("unknown failure")

    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=boom)
    # If this raises, the test fails — that's the assertion.
    assert client.send("test") is False


def test_send_passes_timeout_to_post() -> None:
    post = _fake_post_factory(200)
    client = TelegramClient(
        bot_token="TOK", chat_id="CHAT", timeout_sec=2.5, post_fn=post,
    )
    client.send("hello")
    assert post.calls[0]["timeout"] == 2.5  # type: ignore[attr-defined]


def test_send_truncates_long_text_in_failure_log(caplog) -> None:
    """Verify failure logs don't dump multi-KB alert bodies."""
    def boom(*a, **kw):
        raise ConnectionError("err")

    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=boom)
    long_text = "x" * 1000
    with caplog.at_level(logging.WARNING, logger="alerts.telegram_client"):
        client.send(long_text)
    # The log should contain a truncated form, not the full 1000 chars.
    for r in caplog.records:
        msg = r.getMessage()
        # Some token of truncation should appear; full payload shouldn't.
        assert "x" * 1000 not in msg


def test_send_response_without_status_code_returns_false() -> None:
    """Defensive: a malformed response without status_code attr fails closed."""
    class _MalformedResponse:
        pass

    def post(*a, **kw):
        return _MalformedResponse()

    client = TelegramClient(bot_token="TOK", chat_id="CHAT", post_fn=post)
    assert client.send("test") is False
