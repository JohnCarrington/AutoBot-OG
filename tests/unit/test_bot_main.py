"""Tests for bot.main — the entrypoint composition.

We don't exercise the full _build_runtime (that would hit the real IG
SDK and Lightstreamer factory). Instead we test the helpers that have
no IO dependency: config loading and token extraction.
"""
from __future__ import annotations

import pytest

from bot.main import _extract_tokens, _load_config


def test_load_config_defaults_to_pair_config_pairs(monkeypatch) -> None:
    monkeypatch.delenv("BOT_PAIRS", raising=False)
    cfg = _load_config()
    # From config.pair_config.PAIRS — locked v1 list.
    assert "GBPUSD" in cfg.pairs
    assert all(epic.startswith("CS.D.") for epic in cfg.pair_to_epic.values())


def test_load_config_respects_bot_pairs_env_var(monkeypatch) -> None:
    monkeypatch.setenv("BOT_PAIRS", "GBPUSD,EURUSD")
    cfg = _load_config()
    assert cfg.pairs == ("GBPUSD", "EURUSD")
    assert cfg.pair_to_epic == {
        "GBPUSD": "CS.D.GBPUSD.TODAY.IP",
        "EURUSD": "CS.D.EURUSD.TODAY.IP",
    }


def test_load_config_strips_whitespace_and_upper_cases_pairs(monkeypatch) -> None:
    monkeypatch.setenv("BOT_PAIRS", " gbpusd , eurusd ,")
    cfg = _load_config()
    assert cfg.pairs == ("GBPUSD", "EURUSD")


def test_extract_tokens_from_session_headers() -> None:
    class _S:
        class _Service:
            class session:
                headers = {"CST": "cst-tok", "X-SECURITY-TOKEN": "xst-tok"}
        service = _Service()
    cst, xst = _extract_tokens(_S())
    assert cst == "cst-tok"
    assert xst == "xst-tok"


def test_extract_tokens_case_insensitive_keys() -> None:
    class _S:
        class _Service:
            class session:
                headers = {"cst": "C", "x-security-token": "X"}
        service = _Service()
    cst, xst = _extract_tokens(_S())
    assert (cst, xst) == ("C", "X")


def test_extract_tokens_raises_on_missing_headers() -> None:
    class _S:
        class _Service:
            session = None
        service = _Service()
    with pytest.raises(RuntimeError, match="no session headers"):
        _extract_tokens(_S())


def test_extract_tokens_raises_on_missing_cst() -> None:
    class _S:
        class _Service:
            class session:
                headers = {"X-SECURITY-TOKEN": "X"}
        service = _Service()
    with pytest.raises(RuntimeError, match="CST"):
        _extract_tokens(_S())
