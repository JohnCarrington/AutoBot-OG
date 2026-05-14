"""Tests for feed.ig_rest.auth.load_ig_credentials + create_ig_service."""
from __future__ import annotations

import pytest

from feed.ig_rest.auth import (
    IGCredentials,
    IGSession,
    create_ig_service,
    load_ig_credentials,
)


_REQUIRED = ("IG_USERNAME", "IG_PASSWORD", "IG_API_KEY", "IG_ACC_TYPE")


def _set_full_env(monkeypatch, **overrides) -> None:
    defaults = {
        "IG_USERNAME": "u",
        "IG_PASSWORD": "p",
        "IG_API_KEY": "k",
        "IG_ACC_TYPE": "DEMO",
    }
    defaults.update(overrides)
    for name, val in defaults.items():
        monkeypatch.setenv(name, val)
    # Clear optional ones unless overridden.
    for opt in ("IG_ACCOUNT_ID", "IG_PRODUCT_TYPE"):
        if opt not in overrides:
            monkeypatch.delenv(opt, raising=False)


def test_load_credentials_happy_path(monkeypatch) -> None:
    _set_full_env(monkeypatch)
    creds = load_ig_credentials()
    assert creds.username == "u"
    assert creds.acc_type == "DEMO"
    assert creds.account_id is None
    assert creds.product_type is None


def test_load_credentials_uppercases_acc_type(monkeypatch) -> None:
    _set_full_env(monkeypatch, IG_ACC_TYPE="live")
    creds = load_ig_credentials()
    assert creds.acc_type == "LIVE"


def test_load_credentials_carries_optional_fields(monkeypatch) -> None:
    _set_full_env(monkeypatch, IG_ACCOUNT_ID="ABC123", IG_PRODUCT_TYPE="spreadbet")
    creds = load_ig_credentials()
    assert creds.account_id == "ABC123"
    assert creds.product_type == "SPREADBET"


@pytest.mark.parametrize("missing", _REQUIRED)
def test_load_credentials_missing_required_raises(monkeypatch, missing) -> None:
    _set_full_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(RuntimeError, match="Missing"):
        load_ig_credentials()


def test_load_credentials_invalid_acc_type_raises(monkeypatch) -> None:
    _set_full_env(monkeypatch, IG_ACC_TYPE="paper")
    with pytest.raises(RuntimeError, match="IG_ACC_TYPE"):
        load_ig_credentials()


def test_load_credentials_invalid_product_type_raises(monkeypatch) -> None:
    _set_full_env(monkeypatch, IG_PRODUCT_TYPE="exotic")
    with pytest.raises(RuntimeError, match="IG_PRODUCT_TYPE"):
        load_ig_credentials()


# --- create_ig_service: factory injection ----------------------------------


class _FakeService:
    """Minimal stub matching _IGServiceLike."""

    def __init__(self) -> None:
        self.ACC_NUMBER = None
        self.create_session_calls = 0

    def create_session(self, session=None, encryption=False, version: str = "2") -> None:
        self.create_session_calls += 1

    def switch_account(self, account_id: str, default_account) -> None:
        pass


def test_create_ig_service_uses_injected_factory(monkeypatch) -> None:
    _set_full_env(monkeypatch, IG_ACCOUNT_ID="X1")
    seen: dict = {}

    def factory(creds: IGCredentials):
        seen["creds"] = creds
        return _FakeService()

    sess = create_ig_service(service_factory=factory)
    assert isinstance(sess, IGSession)
    assert seen["creds"].username == "u"
    assert sess.account_id == "X1"
    assert sess.service.create_session_calls == 1
