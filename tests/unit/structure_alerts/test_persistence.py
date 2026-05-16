"""Tests for structure_alerts.persistence.append_event_to_jsonl.

Best-effort jsonl writer — basic write paths plus the OSError
swallow-and-warn isolation invariant.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from alerts import AlertSeverity
from structure_alerts.persistence import append_event_to_jsonl, _to_payload
from structure_alerts.types import AlertEvent, AlertEventKind


_TS = datetime(2026, 5, 16, 9, 5, tzinfo=timezone.utc)


def _event(**overrides) -> AlertEvent:
    defaults = dict(
        kind=AlertEventKind.HTF_BIAS_CHANGE,
        pair="GBPUSD",
        severity=AlertSeverity.WARNING,
        timestamp=_TS,
        dedupe_key="GBPUSD_HTF_BIAS_BEARISH",
        full_text="HTF bias BULLISH -> BEARISH",
        short_text="HTF BULLISH -> BEARISH",
        debug={"prev_htf_bias": "BULLISH", "curr_htf_bias": "BEARISH"},
    )
    defaults.update(overrides)
    return AlertEvent(**defaults)


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


def test_payload_renders_enums_as_values() -> None:
    payload = _to_payload(_event())
    assert payload["kind"] == "HTF_BIAS_CHANGE"
    assert payload["severity"] == "WARNING"


def test_payload_renders_timestamp_as_iso_string() -> None:
    payload = _to_payload(_event())
    assert payload["timestamp"] == _TS.isoformat()


def test_payload_field_order_starts_with_timestamp() -> None:
    # Field order is human-grep-friendly for incident triage:
    # timestamp first, then kind/pair/severity, then dedupe_key, then
    # bodies, then debug.
    payload = _to_payload(_event())
    keys = list(payload.keys())
    assert keys[0] == "timestamp"
    assert keys[1] == "kind"
    assert keys[2] == "pair"
    assert keys[3] == "severity"
    assert keys[4] == "dedupe_key"


def test_payload_preserves_debug_dict() -> None:
    payload = _to_payload(_event(debug={"x": 1, "y": "z"}))
    assert payload["debug"] == {"x": 1, "y": "z"}


# ---------------------------------------------------------------------------
# Basic write paths
# ---------------------------------------------------------------------------


def test_appends_single_event_as_one_line(tmp_path) -> None:
    path = tmp_path / "structure_alerts.jsonl"
    append_event_to_jsonl(_event(), path)
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "HTF_BIAS_CHANGE"
    assert rec["pair"] == "GBPUSD"
    assert rec["dedupe_key"] == "GBPUSD_HTF_BIAS_BEARISH"


def test_appends_multiple_events_in_order(tmp_path) -> None:
    path = tmp_path / "structure_alerts.jsonl"
    e1 = _event(dedupe_key="K1", full_text="first")
    e2 = _event(dedupe_key="K2", full_text="second")
    e3 = _event(dedupe_key="K3", full_text="third")
    for evt in (e1, e2, e3):
        append_event_to_jsonl(evt, path)
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["dedupe_key"] == "K1"
    assert json.loads(lines[1])["dedupe_key"] == "K2"
    assert json.loads(lines[2])["dedupe_key"] == "K3"


def test_creates_missing_parent_directories(tmp_path) -> None:
    # Nested parent path does not exist yet — append should create it.
    path = tmp_path / "deeply" / "nested" / "structure_alerts.jsonl"
    assert not path.parent.exists()
    append_event_to_jsonl(_event(), path)
    assert path.exists()
    assert path.parent.is_dir()


def test_accepts_string_path(tmp_path) -> None:
    path_str = str(tmp_path / "structure_alerts.jsonl")
    append_event_to_jsonl(_event(), path_str)
    assert Path(path_str).exists()


# ---------------------------------------------------------------------------
# Serialisation edge cases
# ---------------------------------------------------------------------------


def test_non_json_native_debug_values_survive_via_default_str(tmp_path) -> None:
    # default=str in json.dumps lets non-trivial values round-trip
    # through str() rather than crashing the writer.
    weird_value = AlertEventKind.NEW_MAJOR_LEVEL  # enum lands as str
    path = tmp_path / "structure_alerts.jsonl"
    append_event_to_jsonl(
        _event(debug={"x": weird_value, "ts_native": _TS}),
        path,
    )
    rec = json.loads(path.read_text().splitlines()[0])
    # The enum lands as its Python str() form; the datetime as its
    # isoformat() (via str(datetime)).
    assert "NEW_MAJOR_LEVEL" in rec["debug"]["x"]
    assert "2026-05-16" in rec["debug"]["ts_native"]


# ---------------------------------------------------------------------------
# OSError isolation — caller never sees a raise
# ---------------------------------------------------------------------------


def test_os_error_when_parent_is_a_regular_file_is_swallowed(
    tmp_path, caplog,
) -> None:
    # Create a FILE where the writer expects a parent DIRECTORY. The
    # mkdir(parents=True, exist_ok=True) call raises FileExistsError
    # (a subclass of OSError) because the existing parent is not a
    # directory.
    parent_file = tmp_path / "this_is_a_file"
    parent_file.write_text("not a directory")
    path = parent_file / "structure_alerts.jsonl"

    # Must not raise.
    with caplog.at_level("WARNING", logger="structure_alerts.persistence"):
        append_event_to_jsonl(_event(), path)

    # WARNING was logged with the expected prefix.
    matching = [
        r for r in caplog.records
        if "structure_alerts: failed to write" in r.getMessage()
    ]
    assert len(matching) == 1


def test_open_raises_os_error_is_swallowed(tmp_path, monkeypatch, caplog) -> None:
    # Defensive: even if mkdir succeeds, a permissions race that
    # makes open() raise OSError must be swallowed.
    path = tmp_path / "structure_alerts.jsonl"

    def _raising_open(self, *args, **kwargs):  # type: ignore[no-redef]
        raise PermissionError("simulated")

    monkeypatch.setattr(Path, "open", _raising_open)
    with caplog.at_level("WARNING", logger="structure_alerts.persistence"):
        append_event_to_jsonl(_event(), path)
    matching = [
        r for r in caplog.records
        if "structure_alerts: failed to write" in r.getMessage()
    ]
    assert len(matching) == 1
