"""Tests for feed.lightstreamer.parsers — payload → Candle conversion.

The fixture below (``PROBE_PAYLOAD``) is the *verbatim* payload IG
delivered against ``CS.D.GBPUSD.TODAY.IP`` on the demo endpoint, copied
straight out of ``scripts/probe_lightstreamer_output_2026-05-15.json``.
Prices are in raw IG spreadbet **points** (no decimal scaling) — see
the parser comment block for the convention. An earlier version of
this fixture used scaled-decimal values (``"1.33392"`` instead of
``"13339.2"``) and hid C2 from adversarial review.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from feed.lightstreamer.parsers import (
    PartialPayloadError,
    is_bar_close,
    parse_chart_payload,
)

PROBE_JSON_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts" / "probe_lightstreamer_output_2026-05-15.json"
)


# Verbatim from the probe JSON. UTM 1778837400000 = 2026-05-15 09:30:00
# UTC (bar open). BID/OFR are raw IG spreadbet points: 13339.2 (raw
# points) corresponds to ~1.33392 in decimal price-space, but everything
# downstream of the parser operates on raw points without rescaling.
PROBE_PAYLOAD = {
    "UTM": "1778837400000",
    "LTV": "252",
    "CONS_TICK_COUNT": "252",
    "CONS_END": "0",
    "BID_OPEN": "13339.2",
    "BID_HIGH": "13342.2",
    "BID_LOW": "13338.4",
    "BID_CLOSE": "13340.8",
    "OFR_OPEN": "13340.1",
    "OFR_HIGH": "13343.1",
    "OFR_LOW": "13339.3",
    "OFR_CLOSE": "13341.7",
}


def test_parse_full_payload_yields_mid_ohlc() -> None:
    candle = parse_chart_payload("GBPUSD", PROBE_PAYLOAD)
    assert candle.pair == "GBPUSD"
    # UTM 1778837400000 = 2026-05-15 09:30:00 UTC (bar open) →
    # close_time = open + 5min = 09:35:00 UTC.
    assert candle.close_time == datetime(2026, 5, 15, 9, 35, tzinfo=timezone.utc)
    # Mids = (bid+ofr)/2 in RAW POINTS — no scaling applied.
    assert candle.open == pytest.approx((13339.2 + 13340.1) / 2)
    assert candle.high == pytest.approx((13342.2 + 13343.1) / 2)
    assert candle.low == pytest.approx((13338.4 + 13339.3) / 2)
    assert candle.close == pytest.approx((13340.8 + 13341.7) / 2)
    # Sanity check the raw-points range for GBPUSD: production rolling
    # CSV shows values like 13523.65, never 1.3523. If this assertion
    # ever flips back to decimal we want a loud test failure.
    assert 1000 < candle.open < 100000, (
        f"GBPUSD raw-points mid out of expected range: {candle.open}"
    )
    assert candle.volume == 252.0
    assert candle.source == "LS_NATIVE_5M"


def test_parse_verbatim_probe_json_from_disk() -> None:
    """Round-trip the probe's recorded sample through the live parser.

    This test exists specifically to catch the failure mode adversarial
    review flagged as C2: if the parser ever scales prices or drops
    fields silently, the candle's open will leave the raw-points range
    (~1000-100000 for GBPUSD spreadbet) and this assertion will fire.
    """
    raw = json.loads(PROBE_JSON_PATH.read_text())
    sample = None
    for attempt in raw.get("attempts", []):
        if attempt.get("name") == "CHART:5MINUTE":
            sample = attempt.get("sample_payload")
            break
    assert sample is not None, (
        f"CHART:5MINUTE attempt not found in {PROBE_JSON_PATH}"
    )
    candle = parse_chart_payload("GBPUSD", sample)
    assert candle.pair == "GBPUSD"
    # Raw points, not scaled decimal — see parser docstring.
    assert 1000 < candle.open < 100000
    assert 1000 < candle.close < 100000
    # CONS_END=0 on probe payload → not closed.
    assert is_bar_close(sample) is False


def test_parse_missing_utm_raises_partial() -> None:
    payload = dict(PROBE_PAYLOAD)
    payload["UTM"] = None
    with pytest.raises(PartialPayloadError):
        parse_chart_payload("GBPUSD", payload)


def test_parse_missing_one_side_of_ohlc_raises_partial() -> None:
    payload = dict(PROBE_PAYLOAD)
    payload["BID_HIGH"] = None
    with pytest.raises(PartialPayloadError):
        parse_chart_payload("GBPUSD", payload)


def test_parse_missing_volume_defaults_to_zero() -> None:
    payload = dict(PROBE_PAYLOAD)
    payload.pop("CONS_TICK_COUNT")
    payload.pop("LTV")
    candle = parse_chart_payload("GBPUSD", payload)
    assert candle.volume == 0.0


def test_parse_uses_ltv_when_cons_tick_count_missing() -> None:
    payload = dict(PROBE_PAYLOAD)
    payload.pop("CONS_TICK_COUNT")
    candle = parse_chart_payload("GBPUSD", payload)
    assert candle.volume == 252.0


def test_parse_handles_nan_as_missing() -> None:
    payload = dict(PROBE_PAYLOAD)
    payload["BID_OPEN"] = "nan"
    with pytest.raises(PartialPayloadError):
        parse_chart_payload("GBPUSD", payload)


def test_is_bar_close_recognises_one() -> None:
    assert is_bar_close({"CONS_END": "1"}) is True
    assert is_bar_close({"CONS_END": 1}) is True
    assert is_bar_close({"CONS_END": "1.0"}) is True


def test_is_bar_close_false_on_missing_or_zero() -> None:
    assert is_bar_close({}) is False
    assert is_bar_close({"CONS_END": None}) is False
    assert is_bar_close({"CONS_END": "0"}) is False
    assert is_bar_close({"CONS_END": "garbage"}) is False
