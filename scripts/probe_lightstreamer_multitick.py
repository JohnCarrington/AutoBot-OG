"""Multi-tick Lightstreamer probe: capture every update over a 3-minute window.

The single-shot probe (``probe_lightstreamer.py``) confirmed the
working adapter format but only recorded the *first* update payload
on each attempt. Phase 7 adversarial review flagged two CRITICAL
unknowns about the wire format that need *multiple* updates to
resolve:

1. Does ``UTM`` advance every tick (i.e. it's the tick wall-clock),
   or does it stay on the bar-open boundary for the lifetime of a
   bar? The Phase 7 parser assumes the latter — if it's actually
   the former, the candle's ``close_time`` projection is wrong by
   up to ~5 minutes.
2. Are the BID/OFR fields raw broker quotes (e.g. ``1.30050``) or
   integer-scaled points (e.g. ``130050``)? The legacy production
   bot stores something — comparing the live values against the
   bot's rolling-candle CSV reveals where any scaling happens.

This probe subscribes to the same working adapter
(``CHART:{epic}:5MINUTE``, MERGE, 12 fields) and records *every*
update it receives over a 180-second window, then computes a small
analysis block:

- ``unique_utm_values`` — how many distinct UTM values we saw.
- ``utm_advance_pattern`` — "every-tick" if most consecutive
  updates carry a different UTM (so UTM is the tick time), or
  "bar-aligned-only" if the same UTM repeats across many updates
  (so UTM is the bar-open time and stays constant within a bar).
- ``cons_end_flips_observed`` — count of 0→1 transitions in
  ``CONS_END``, which tells us how many bar-close events landed
  inside the window.
- ``bid_open_sample_values`` — the first dozen distinct
  ``BID_OPEN`` strings, ordered by appearance. Cross-checking
  these against the production bot's rolling-candle CSV reveals
  any integer/decimal scaling.

Run on AutoBotV1 (or any host with the runtime deps below) where IG
credentials sit in a ``.env`` file in the working directory.

Runtime requirements:
- Python 3.10+
- ``python-dotenv``
- ``trading_ig`` (==0.0.16 on AutoBotV1)
- ``lightstreamer-client-lib`` (PyPI dist; import path ``lightstreamer.client``)

Output:
    {cwd}/probe_lightstreamer_multitick_output_<YYYY-MM-DD>.json

Privacy note:
    Only market-data fields are subscribed to (OHLC / BID / OFR /
    UTM / volume). No account-identifying fields appear in the
    captured updates list.

This file is **probe-only** — it does not import from any AutoBot
module, and Phase 7 implementation will not call into it.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import Counter
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
# Credentials (loaded from .env in CWD at import time)
# ---------------------------------------------------------------------------

load_dotenv()

IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")
IG_API_KEY = os.getenv("IG_API_KEY")
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
TEST_EPIC = "CS.D.GBPUSD.TODAY.IP"
SUBSCRIPTION_DURATION_SEC = 180  # 3 min — guaranteed to span a 5m bar close
LS_CONNECT_TIMEOUT_SEC = 10

FIELDS: list[str] = [
    "UTM",
    "LTV",
    "CONS_TICK_COUNT",
    "CONS_END",
    "BID_OPEN",
    "BID_HIGH",
    "BID_LOW",
    "BID_CLOSE",
    "OFR_OPEN",
    "OFR_HIGH",
    "OFR_LOW",
    "OFR_CLOSE",
]

# The output path is anchored to the working directory (not __file__),
# because the run command places this script under /opt/tradingbot on
# AutoBotV1 and the scp-back step expects the file alongside it there.
OUTPUT_PATH = (
    Path.cwd() / f"probe_lightstreamer_multitick_output_{PROBE_DATE}.json"
)

LS_ENDPOINT_BY_ACC = {
    "LIVE": "https://apd.marketdatasystems.com",
    "DEMO": "https://demo-apd.marketdatasystems.com",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("probe_lightstreamer_multitick")


# ---------------------------------------------------------------------------
# Lightstreamer SubscriptionListener — capture EVERY update
# ---------------------------------------------------------------------------


class _MultiTickListener(SubscriptionListener):  # type: ignore[misc]
    """Captures every onItemUpdate event, not just the first."""

    def __init__(self, fields: list[str]) -> None:
        self._fields = fields
        self._lock = threading.Lock()
        self.updates: list[dict[str, Any]] = []
        self.confirmed_event = threading.Event()
        self.error_event = threading.Event()
        self.error_code: Optional[int] = None
        self.error_message: Optional[str] = None

    def onSubscription(self) -> None:  # noqa: N802 — LS API
        self.confirmed_event.set()
        logger.info("Subscription confirmed by IG.")

    def onSubscriptionError(self, code: Any, message: Any) -> None:  # noqa: N802 — LS API
        with self._lock:
            try:
                self.error_code = int(code) if code is not None else None
            except (TypeError, ValueError):
                self.error_code = None
            self.error_message = str(message) if message else ""
        self.error_event.set()
        logger.error("Subscription error: code=%s message=%s", code, message)

    def onItemUpdate(self, item_update: Any) -> None:  # noqa: N802 — LS API
        # Record the field values exactly as the SDK returns them
        # (LS wire format is text-oriented, so values are usually
        # strings or None for "field unchanged in this delta").
        now = datetime.now(timezone.utc)
        wall_clock = now.isoformat(timespec="milliseconds")
        values: dict[str, Optional[str]] = {}
        for field_name in self._fields:
            try:
                raw = item_update.getValue(field_name)
            except Exception:
                raw = None
            values[field_name] = None if raw is None else str(raw)
        is_partial = any(v is None or v == "" for v in values.values())
        record = {
            "wall_clock_utc": wall_clock,
            "fields": values,
            "is_partial": is_partial,
        }
        with self._lock:
            self.updates.append(record)


# ---------------------------------------------------------------------------
# IG session → LS connection
# ---------------------------------------------------------------------------


def _extract_tokens(ig: IGService) -> tuple[str, str]:
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

    deadline = time.time() + LS_CONNECT_TIMEOUT_SEC
    last_status = ""
    while time.time() < deadline:
        last_status = client.getStatus()
        # "DISCONNECTED" contains "CONNECTED" as a substring, so we
        # match the CONNECTED:* prefix explicitly.
        upper = last_status.upper()
        if upper.startswith("CONNECTED:") or upper == "CONNECTED":
            logger.info("Lightstreamer connected (status=%s)", last_status)
            return client
        time.sleep(0.2)
    raise RuntimeError(
        f"LS failed to reach CONNECTED within {LS_CONNECT_TIMEOUT_SEC}s "
        f"(last status={last_status!r})"
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _analyse(updates: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise the captured update stream into a small analysis block.

    The two questions adversarial review wants answered:

    - **UTM cadence.** If every adjacent pair of updates carries a
      distinct UTM, UTM is the tick wall-clock and the Phase 7 parser
      is wrong to project ``close_time = UTM + 5min``. If most adjacent
      pairs share the same UTM (with occasional 5-min jumps), UTM is
      the bar-open time and the parser is correct.
    - **Price magnitude.** ``bid_open_sample_values`` is a small set
      of first-seen BID_OPEN strings. Manual cross-check against the
      production bot's rolling-candle CSV (``cache/GBPUSD_candles_rolling.csv``)
      reveals any integer scaling: if probe shows ``"1.30050"`` but
      cache shows ``"130050"``, production is scaling on the way in.
    """
    if not updates:
        return {
            "unique_utm_values": 0,
            "utm_advance_pattern": "no-updates",
            "cons_end_flips_observed": 0,
            "bid_open_sample_values": [],
            "utm_repeats_within_bar_max": 0,
            "consecutive_utm_differs_count": 0,
            "consecutive_utm_same_count": 0,
        }

    utm_seq = [u["fields"].get("UTM") for u in updates]
    unique_utm = sorted({u for u in utm_seq if u is not None})

    # Pattern detection: count adjacent pairs where UTM stays the same
    # vs. advances. The two regimes look very different:
    #   - bar-aligned: same UTM for many ticks, then jumps by 300000ms.
    #   - every-tick:  UTM differs on (almost) every adjacent pair.
    same_count = 0
    differ_count = 0
    for prev, curr in zip(utm_seq, utm_seq[1:]):
        if prev is None or curr is None:
            continue
        if prev == curr:
            same_count += 1
        else:
            differ_count += 1
    total_pairs = same_count + differ_count
    if total_pairs == 0:
        pattern = "single-update-only"
    elif same_count / total_pairs >= 0.5:
        pattern = "bar-aligned-only"
    else:
        pattern = "every-tick"

    # Within a single UTM value, how many consecutive updates carried
    # that same UTM? Bar-aligned UTM would give a large run length;
    # every-tick UTM would give run length ≈ 1.
    utm_run_counter: Counter[str] = Counter(u for u in utm_seq if u is not None)
    max_run_for_one_utm = max(utm_run_counter.values()) if utm_run_counter else 0

    # CONS_END flips: 0 -> 1 transitions.
    flips = 0
    prev_ce: Optional[str] = None
    for u in updates:
        ce = u["fields"].get("CONS_END")
        if prev_ce in ("0", 0) and ce in ("1", 1):
            flips += 1
        if ce is not None:
            prev_ce = ce

    # First-seen BID_OPEN values (preserve order, dedup). Cap at 12.
    bid_open_seen: list[str] = []
    seen_set: set[str] = set()
    for u in updates:
        v = u["fields"].get("BID_OPEN")
        if v is None or v == "":
            continue
        if v in seen_set:
            continue
        seen_set.add(v)
        bid_open_seen.append(v)
        if len(bid_open_seen) >= 12:
            break

    return {
        "unique_utm_values": len(unique_utm),
        "utm_advance_pattern": pattern,
        "cons_end_flips_observed": flips,
        "bid_open_sample_values": bid_open_seen,
        "utm_repeats_within_bar_max": max_run_for_one_utm,
        "consecutive_utm_differs_count": differ_count,
        "consecutive_utm_same_count": same_count,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    endpoint = LS_ENDPOINT_BY_ACC.get(IG_ACC_TYPE)
    if endpoint is None:
        raise RuntimeError(
            f"No LS endpoint mapping for IG_ACC_TYPE={IG_ACC_TYPE!r}"
        )
    item = f"CHART:{TEST_EPIC}:5MINUTE"
    logger.info(
        "IG account type: %s | LS endpoint: %s | test epic: %s | "
        "duration: %ds",
        IG_ACC_TYPE, endpoint, TEST_EPIC, SUBSCRIPTION_DURATION_SEC,
    )

    client = None
    listener = _MultiTickListener(FIELDS)
    fatal_error: Optional[str] = None
    sub: Optional[Subscription] = None

    try:
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

        client = _connect_lightstreamer(endpoint, account_id, cst, xst)

        sub = Subscription(mode="MERGE", items=[item], fields=FIELDS)
        sub.addListener(listener)
        client.subscribe(sub)
        logger.info(
            "→ subscribing %s (mode=MERGE, fields=%d)",
            item, len(FIELDS),
        )

        # Wait briefly for either a confirm or an error before settling
        # into the long collection sleep — same gating the single-shot
        # probe uses.
        gate_deadline = time.time() + 15
        while time.time() < gate_deadline:
            if listener.confirmed_event.is_set():
                break
            if listener.error_event.is_set():
                break
            time.sleep(0.2)
        if listener.error_event.is_set():
            raise RuntimeError(
                f"IG rejected the subscription: code={listener.error_code} "
                f"message={listener.error_message!r}"
            )

        # Collect updates for the configured window. Log a count every
        # 30 seconds so the operator can see progress.
        start = time.time()
        next_progress = start + 30
        while time.time() - start < SUBSCRIPTION_DURATION_SEC:
            time.sleep(0.5)
            if time.time() >= next_progress:
                with listener._lock:  # safe to peek
                    n = len(listener.updates)
                logger.info(
                    "[%4ds] updates collected so far: %d",
                    int(time.time() - start), n,
                )
                next_progress += 30

    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Fatal error during probe — writing partial JSON.")
    finally:
        if sub is not None and client is not None:
            try:
                client.unsubscribe(sub)
            except Exception as exc:
                logger.warning("unsubscribe failed (ignored): %s", exc)
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:
                logger.warning("LS disconnect failed (ignored): %s", exc)

    # Snapshot the updates list — the listener may still hold the lock
    # in theory, but the LS reader thread has been stopped by the
    # disconnect above, so this is safe.
    with listener._lock:
        updates_snapshot = list(listener.updates)

    analysis = _analyse(updates_snapshot)

    payload = {
        "probe_date": PROBE_DATE,
        "subscription": item,
        "duration_seconds": SUBSCRIPTION_DURATION_SEC,
        "total_updates_received": len(updates_snapshot),
        "updates": updates_snapshot,
        "analysis": analysis,
        "ig_acc_type": IG_ACC_TYPE,
        "ig_host": endpoint,
        "subscription_confirmed": listener.confirmed_event.is_set(),
        "subscription_error_code": listener.error_code,
        "subscription_error_message": listener.error_message,
        "fatal_error": fatal_error,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, default=str))

    # Operator summary on stdout.
    print()
    print("=" * 60)
    print("LIGHTSTREAMER MULTI-TICK PROBE SUMMARY")
    print("=" * 60)
    if fatal_error:
        print(f"  FATAL: {fatal_error}")
    print(f"  Updates received:           {payload['total_updates_received']}")
    print(f"  Unique UTM values:          {analysis['unique_utm_values']}")
    print(f"  UTM advance pattern:        {analysis['utm_advance_pattern']}")
    print(f"  CONS_END 0->1 flips:        {analysis['cons_end_flips_observed']}")
    print(
        f"  Max repeats per UTM:        "
        f"{analysis['utm_repeats_within_bar_max']}"
    )
    print(
        f"  Consec UTM same / differ:   "
        f"{analysis['consecutive_utm_same_count']} / "
        f"{analysis['consecutive_utm_differs_count']}"
    )
    print("  First BID_OPEN samples (verbatim):")
    for v in analysis["bid_open_sample_values"]:
        print(f"     {v!r}")
    print()
    print(f"Output written to: {OUTPUT_PATH}")

    if fatal_error is not None:
        return 3
    return 0 if payload["total_updates_received"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
