"""Probe IG Lightstreamer adapter formats for v1 native 5m candles.

Run on AutoBotV1 (or any host with the runtime deps below) where IG
credentials sit in a ``.env`` file in the working directory.

Runtime requirements:
- Python 3.10+
- ``python-dotenv``
- ``trading_ig`` (==0.0.16 on AutoBotV1)
- ``lightstreamer-client-python``

The script is **fully self-contained** — no imports from any AutoBot
repo. Authenticates with ``trading_ig.IGService`` directly, then drives
the raw ``lightstreamer.client`` API (we need exact control over the
subscription request to see which adapter format IG actually accepts).
Tries three adapters in priority order (CHART:5MINUTE, CHART:TICK,
MARKET:) on a single test epic, captures the first update payload, and
writes a dated JSON record alongside this script.

Output:
    scripts/probe_lightstreamer_output_<YYYY-MM-DD>.json

Privacy note:
    ``sample_payload`` only contains the market-data fields we explicitly
    requested (OHLC / BID / OFR / UTM etc.). No account-identifying
    fields are subscribed to, so nothing account-shaped lands in the
    output JSON.

This file is **probe-only** — Phase 7 implementation will live elsewhere.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from lightstreamer.client import (
    LightstreamerClient,
    Subscription,
    SubscriptionListener,
)
from trading_ig import IGService


# ---------------------------------------------------------------------------
# Credentials (loaded from .env in the working directory at import time)
# ---------------------------------------------------------------------------

load_dotenv()

IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")
IG_API_KEY = os.getenv("IG_API_KEY")
# Default DEMO so an unset env never accidentally hits the live endpoint.
IG_ACC_TYPE = (os.getenv("IG_ACC_TYPE") or "DEMO").upper()
IG_ACCOUNT_ID = (os.getenv("IG_ACCOUNT_ID") or "").strip() or None

if not all([IG_USERNAME, IG_PASSWORD, IG_API_KEY]):
    sys.exit("Missing IG_USERNAME, IG_PASSWORD, or IG_API_KEY in .env")
if IG_ACC_TYPE not in ("LIVE", "DEMO"):
    sys.exit(f"IG_ACC_TYPE must be 'LIVE' or 'DEMO', got {IG_ACC_TYPE!r}")


# ---------------------------------------------------------------------------
# Probe configuration
# ---------------------------------------------------------------------------

PROBE_DATE = "2026-05-15"
# GBPUSD TODAY.IP matches what production bots stream against on the
# SPREADBET account. EURUSD MINI.IP would be the CFD-demo equivalent
# but doesn't reflect the live production permission surface.
TEST_EPIC = "CS.D.GBPUSD.TODAY.IP"
PER_ATTEMPT_SECS = 15
TOTAL_BUDGET_SECS = 60
LS_CONNECT_TIMEOUT_SECS = 10

OUTPUT_PATH = (
    Path(__file__).resolve().parent
    / f"probe_lightstreamer_output_{PROBE_DATE}.json"
)

# Endpoint selection mirrors the legacy streamer (apd vs demo-apd) and
# is keyed off the same IG_ACC_TYPE env var that auth.py reads.
LS_ENDPOINT_BY_ACC = {
    "LIVE": "https://apd.marketdatasystems.com",
    "DEMO": "https://demo-apd.marketdatasystems.com",
}

# Three adapter formats to try, in the order we'd prefer them for v1:
#  1. CHART:{epic}:5MINUTE — gives us a native 5m bar with CONS_END.
#  2. CHART:{epic}:TICK    — raw ticks, useful as a fallback signal.
#  3. MARKET:{epic}        — what production bots use today; must work.
ATTEMPTS_SPEC: list[dict[str, Any]] = [
    {
        "name": "CHART:5MINUTE",
        "item": f"CHART:{TEST_EPIC}:5MINUTE",
        "fields": [
            "UTM", "LTV", "CONS_TICK_COUNT", "CONS_END",
            "BID_OPEN", "BID_HIGH", "BID_LOW", "BID_CLOSE",
            "OFR_OPEN", "OFR_HIGH", "OFR_LOW", "OFR_CLOSE",
        ],
    },
    {
        "name": "CHART:TICK",
        "item": f"CHART:{TEST_EPIC}:TICK",
        "fields": ["BID", "OFR", "LTV", "LTP", "UTM"],
    },
    {
        "name": "MARKET",
        "item": f"MARKET:{TEST_EPIC}",
        "fields": ["UPDATE_TIME", "BID", "OFFER", "MARKET_STATE"],
    },
]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("probe_lightstreamer")


# ---------------------------------------------------------------------------
# Per-attempt result container
# ---------------------------------------------------------------------------


@dataclass
class AttemptResult:
    name: str
    item: str
    fields: list[str]
    subscription_confirmed: bool = False
    error_code: Optional[int] = None
    error_message: Optional[str] = None
    sample_payload: Optional[dict[str, Any]] = None
    fields_received: Optional[list[str]] = None
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "item": self.item,
            "subscription_confirmed": self.subscription_confirmed,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "sample_payload": self.sample_payload,
            "fields_received": self.fields_received,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Lightstreamer SubscriptionListener — captures first confirm + first update
# ---------------------------------------------------------------------------


class _ProbeListener(SubscriptionListener):  # type: ignore[misc]
    """Single-shot listener: records the first subscription event of each
    kind (confirm / error / update) and signals the main thread via
    ``threading.Event`` flags. Subsequent updates are ignored — the probe
    only needs proof-of-life, not a full stream."""

    def __init__(self, fields: list[str]):
        self._fields = fields
        self._lock = threading.Lock()
        self.confirmed_event = threading.Event()
        self.update_event = threading.Event()
        self.error_event = threading.Event()
        self.error_code: Optional[int] = None
        self.error_message: Optional[str] = None
        self.sample_payload: Optional[dict[str, Any]] = None
        self.fields_received: Optional[list[str]] = None

    def onSubscription(self) -> None:
        self.confirmed_event.set()

    def onSubscriptionError(self, code, message) -> None:  # noqa: N802 — LS API
        with self._lock:
            try:
                self.error_code = int(code) if code is not None else None
            except (TypeError, ValueError):
                self.error_code = None
            self.error_message = str(message) if message else ""
        self.error_event.set()

    def onItemUpdate(self, item_update) -> None:  # noqa: N802 — LS API
        if self.update_event.is_set():
            return
        payload: dict[str, Any] = {}
        received: list[str] = []
        for f in self._fields:
            try:
                val = item_update.getValue(f)
            except Exception:
                val = None
            if val is not None:
                received.append(f)
            payload[f] = val
        with self._lock:
            self.sample_payload = payload
            self.fields_received = received
        self.update_event.set()


# ---------------------------------------------------------------------------
# IG session → LS connection
# ---------------------------------------------------------------------------


def _extract_tokens(ig: IGService) -> tuple[str, str]:
    """Pull CST + X-SECURITY-TOKEN from ``ig.session.headers``.

    This is the path verified working on AutoBotV1 with trading_ig
    0.0.16. Older trading_ig versions stash the tokens in different
    places (auth_data dicts, legacy attrs); we don't bother with those
    fallbacks because the probe target host pins 0.0.16.
    """
    sess = getattr(ig, "session", None)
    headers = getattr(sess, "headers", None) if sess is not None else None
    if not headers:
        raise RuntimeError(
            "ig.session.headers is empty or missing — IGService may not "
            "have completed login. Check create_session() call."
        )
    norm = {str(k).upper(): v for k, v in headers.items()}
    cst = norm.get("CST")
    xst = norm.get("X-SECURITY-TOKEN")
    if not cst or not xst:
        raise RuntimeError(
            "CST/X-SECURITY-TOKEN missing from ig.session.headers "
            f"(have keys: {sorted(norm.keys())})"
        )
    return cst, xst


def _connect_lightstreamer(endpoint: str, account_id: str, cst: str, xst: str):
    client = LightstreamerClient(endpoint, "DEFAULT")
    client.connectionDetails.setUser(account_id)
    client.connectionDetails.setPassword(f"CST-{cst}|XST-{xst}")
    client.connect()

    deadline = time.time() + LS_CONNECT_TIMEOUT_SECS
    last_status = ""
    while time.time() < deadline:
        last_status = client.getStatus()
        if "CONNECTED" in last_status.upper():
            logger.info("Lightstreamer connected (status=%s)", last_status)
            return client
        time.sleep(0.2)
    raise RuntimeError(
        f"LS failed to reach CONNECTED within {LS_CONNECT_TIMEOUT_SECS}s "
        f"(last status={last_status!r})"
    )


# ---------------------------------------------------------------------------
# Per-attempt probe
# ---------------------------------------------------------------------------


def _run_attempt(client, spec: dict[str, Any]) -> AttemptResult:
    result = AttemptResult(
        name=spec["name"], item=spec["item"], fields=list(spec["fields"])
    )

    listener = _ProbeListener(list(spec["fields"]))
    sub = Subscription(
        mode="MERGE", items=[spec["item"]], fields=list(spec["fields"])
    )
    sub.addListener(listener)
    client.subscribe(sub)
    logger.info(
        "→ subscribing %s (mode=MERGE, fields=%d)",
        spec["item"], len(spec["fields"]),
    )

    deadline = time.time() + PER_ATTEMPT_SECS
    while time.time() < deadline:
        if listener.error_event.is_set():
            result.error_code = listener.error_code
            result.error_message = listener.error_message
            break
        if listener.update_event.is_set():
            result.subscription_confirmed = True
            result.sample_payload = listener.sample_payload
            result.fields_received = listener.fields_received
            break
        time.sleep(0.2)
    else:
        # Timed out without an update. If we did at least see a subscription
        # confirm event, record that — useful for distinguishing "IG accepts
        # the format but no data flowing" from "IG rejected it outright".
        if listener.confirmed_event.is_set():
            result.subscription_confirmed = True
            result.note = (
                f"Subscription accepted by IG but no update received in "
                f"{PER_ATTEMPT_SECS}s — market may be closed or quiet."
            )

    try:
        client.unsubscribe(sub)
    except Exception as exc:
        logger.warning("unsubscribe failed (ignored): %s", exc)

    if result.subscription_confirmed and result.sample_payload:
        tag = "ok"
    elif result.subscription_confirmed:
        tag = "subscribed-no-data"
    elif result.error_code is not None:
        tag = f"err={result.error_code}"
    else:
        tag = "no-response"
    logger.info("← %s: %s", spec["name"], tag)
    return result


# ---------------------------------------------------------------------------
# Output / recommendation
# ---------------------------------------------------------------------------


def _pick_working(attempts: list[AttemptResult]) -> Optional[AttemptResult]:
    # Prefer an attempt that actually delivered data; fall back to one
    # that at least got past IG's subscription gate.
    for a in attempts:
        if a.subscription_confirmed and a.sample_payload:
            return a
    for a in attempts:
        if a.subscription_confirmed:
            return a
    return None


def _recommendation(working: Optional[AttemptResult]) -> str:
    if working is None:
        return (
            "No adapter accepted the test subscription. Check IG account "
            "permissions for streaming on this epic and confirm the LS "
            "endpoint matches IG_ACC_TYPE."
        )
    fields = working.fields_received or working.fields
    if working.sample_payload is None:
        return (
            f"Use {working.name} ({working.item}) — IG accepted the "
            f"subscription but no update arrived in the probe window. "
            f"Re-run during market hours to confirm a data sample."
        )
    return (
        f"Use {working.name} ({working.item}) with fields {fields} for "
        f"v1 native 5m candle subscription."
    )


def _write_output(
    acc_type: str,
    endpoint: str,
    results: list[AttemptResult],
    working: Optional[AttemptResult],
    fatal_error: Optional[str] = None,
) -> None:
    payload = {
        "probe_date": PROBE_DATE,
        "ig_acc_type": acc_type,
        "ig_host": endpoint,
        "test_epic": TEST_EPIC,
        "attempts": [r.to_dict() for r in results],
        "working_adapter": working.name if working else None,
        "recommendation": _recommendation(working),
        "fatal_error": fatal_error,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, default=str))


def _print_summary(
    results: list[AttemptResult],
    working: Optional[AttemptResult],
    fatal_error: Optional[str] = None,
) -> None:
    print()
    print("=" * 60)
    print("LIGHTSTREAMER PROBE SUMMARY")
    print("=" * 60)
    if fatal_error:
        print(f"  FATAL: {fatal_error}")
        print()
    for r in results:
        if r.subscription_confirmed and r.sample_payload:
            tag = "OK (got update)"
        elif r.subscription_confirmed:
            tag = "SUBSCRIBED (no update in window)"
        elif r.error_code is not None:
            tag = f"ERROR {r.error_code}: {r.error_message or ''}"
        elif r.note:
            tag = f"SKIPPED ({r.note})"
        else:
            tag = "NO RESPONSE"
        print(f"  {r.name:14s}  {tag}")
    print()
    print(_recommendation(working))
    print(f"Output written to: {OUTPUT_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    endpoint = LS_ENDPOINT_BY_ACC.get(IG_ACC_TYPE)
    if endpoint is None:
        raise RuntimeError(
            f"No LS endpoint mapping for IG_ACC_TYPE={IG_ACC_TYPE!r}"
        )
    logger.info(
        "IG account type: %s | LS endpoint: %s | test epic: %s",
        IG_ACC_TYPE, endpoint, TEST_EPIC,
    )

    # Pre-seed `results` with all specs so the JSON record is meaningful
    # even if we crash before / during the subscription loop. Each entry
    # is replaced in-place once its attempt actually runs.
    results: list[AttemptResult] = [
        AttemptResult(
            name=s["name"], item=s["item"], fields=list(s["fields"]),
            note="not attempted — exited before this probe ran",
        )
        for s in ATTEMPTS_SPEC
    ]
    fatal_error: Optional[str] = None
    client = None

    try:
        # trading_ig wants lower-case acc_type ("demo"/"live"); see
        # legacy auth.py which calls .lower() before constructing.
        ig = IGService(
            IG_USERNAME,
            IG_PASSWORD,
            IG_API_KEY,
            IG_ACC_TYPE.lower(),
            acc_number=IG_ACCOUNT_ID,
        )
        ig.create_session()
        logger.info("IG session created (acc_type=%s)", IG_ACC_TYPE)

        cst, xst = _extract_tokens(ig)
        account_id = IG_ACCOUNT_ID or getattr(ig, "ACC_NUMBER", None)
        if not account_id:
            raise RuntimeError(
                "No account_id available for LS authentication — set "
                "IG_ACCOUNT_ID in .env or ensure IGService.ACC_NUMBER is "
                "populated after create_session()."
            )

        started = time.time()
        client = _connect_lightstreamer(endpoint, account_id, cst, xst)

        for idx, spec in enumerate(ATTEMPTS_SPEC):
            if time.time() - started > TOTAL_BUDGET_SECS:
                logger.warning(
                    "Total budget %.1fs exceeded — skipping remaining attempts",
                    TOTAL_BUDGET_SECS,
                )
                results[idx] = AttemptResult(
                    name=spec["name"], item=spec["item"],
                    fields=list(spec["fields"]),
                    note="skipped — exceeded total time budget",
                )
                continue
            results[idx] = _run_attempt(client, spec)

    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Fatal error during probe — writing partial JSON.")
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    working = _pick_working(results)
    _write_output(IG_ACC_TYPE, endpoint, results, working, fatal_error)
    _print_summary(results, working, fatal_error)
    if fatal_error is not None:
        return 3
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(main())
