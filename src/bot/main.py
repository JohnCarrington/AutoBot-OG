"""Phase 8 entrypoint: ``main()`` wires .env → preflight → BotLoop.

Exit codes:

- ``0`` — graceful shutdown via SIGTERM/SIGINT
- ``1`` — pre-flight failure (env vars, IG auth, subscriptions); the
  bot never started trading
- ``2`` — runtime crash (5-strike failure counter trip, or an
  uncaught exception escaped the LS handler)

The main thread is a single ``threading.Event.wait()`` — all the
trading work happens on the LS reader thread inside the BotLoop event
callback. Signals are delivered to the main thread by Python's signal
machinery, so the handler can simply set the shutdown event and
let ``main()`` proceed to the drain.

There is **no** auto-restart loop; if the bot crashes, an external
supervisor (systemd, docker, etc.) restarts the process. v1 keeps the
process minimal so the supervisor decides recovery policy.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from alerts import Alert, AlertCategory, AlertSeverity, TelegramAlerter
from config.pair_config import PAIRS
from execution.executor import Executor
from execution.position_manager import PositionManager
from feed.feed_manager import FeedManager
from feed.ig_rest.auth import create_ig_service
from feed.ig_rest.client import IGClient
from feed.ig_rest.history import fetch_historical_prices
from feed.lightstreamer.client import LightstreamerSubscriber
from risk.guard import RiskGuard

from . import preflight as preflight_mod
from .constants import (
    BOT_LOG_FILE,
    BOT_LOG_LEVEL,
    BOT_SHUTDOWN_DRAIN_POLL_SEC,
    BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC,
)
from .logging_setup import setup_logging
from .loop import BotLoop
from .types import BotRuntimeConfig

logger = logging.getLogger("bot.main")


# Exit codes (locked Phase 8 + Phase 10):
# - 0: graceful shutdown
# - 1: pre-flight failure
# - 2: runtime crash (failure threshold trip)
# - 3: refused-to-start guard (shadow_mode + non-empty positions; H1
#      layer 1 from Phase 10 review)
EXIT_ABORT: int = 3


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """Run the bot until shutdown. Returns the process exit code."""
    load_dotenv()  # populate os.environ from .env before any constant read
    setup_logging(level=BOT_LOG_LEVEL, log_file=BOT_LOG_FILE or None)
    ig_env = (os.getenv("IG_ACC_TYPE") or "?").upper()
    shadow_mode = _read_shadow_mode_env()
    logger.info(
        "AutoBot-OG main() starting (env=%s, shadow_mode=%s)",
        ig_env, shadow_mode,
    )

    config = _load_config()

    # --- Static pre-flight (env, disk, IG auth) -------------------------
    static_report = preflight_mod.run_static_checks()
    for r in static_report.results:
        logger.info("preflight[%s]: ok=%s msg=%s", r.name, r.ok, r.message)
    if not static_report.ok:
        for msg in static_report.fail_messages():
            logger.error("preflight failure: %s", msg)
        return 1

    # --- Construct the alerter (single instance shared across runtime) --
    # Done BEFORE _build_runtime so the same instance flows into Executor
    # + BotLoop. No-op mode if creds missing — TelegramAlerter logs a
    # WARNING once at construction and short-circuits send/tick/close.
    alerter = TelegramAlerter()

    # --- Build the runtime tree -----------------------------------------
    try:
        bot, subscriber = _build_runtime(
            config, alerter=alerter, shadow_mode=shadow_mode,
        )
    except Exception:
        logger.exception("Failed to build runtime tree")
        # No bot constructed → the alerter never reached BotLoop, so
        # close it directly so any pending state drains.
        alerter.close()
        return 1

    # --- H1 layer 1 (Phase 10 review): refuse to start in shadow mode
    #     with pre-existing positions. SHADOW_MODE only intercepts the
    #     open-position broker call; pre-existing positions would still
    #     trigger real apply_amend / close_position calls via
    #     _run_sl_evaluation and _execute_force_close. Operator must
    #     close existing positions (via IG web UI) before enabling
    #     shadow mode for re-validation. CRITICAL Telegram alert so
    #     the operator sees the abort even if they're not watching the
    #     systemctl status.
    if shadow_mode:
        existing = bot.position_manager_for_startup_check().all()
        if existing:
            deal_ids = [p.deal_id for p in existing]
            msg = (
                f"Cannot start in SHADOW_MODE with {len(existing)} "
                f"existing position(s): {deal_ids}. SHADOW_MODE only "
                f"intercepts new opens; pre-existing positions would "
                f"trigger real broker amends and force-closes. Close "
                f"the position(s) via IG web UI (and clear "
                f"data/execution/positions.json) before re-enabling "
                f"SHADOW_MODE."
            )
            logger.critical(msg)
            _emit_startup_aborted_alert(
                alerter=alerter, message=msg, deal_ids=deal_ids,
            )
            alerter.close()
            return EXIT_ABORT

    # --- Signal wiring (set before start() so a fast SIGINT works) ------
    def _signal_handler(signum: int, _frame) -> None:
        logger.warning(
            "Received signal %s — requesting graceful shutdown",
            signal.Signals(signum).name if signum in iter(signal.Signals) else signum,
        )
        # External signal — no reason supplied, so no
        # FAILURE_THRESHOLD_TRIPPED alert. The operator already knows
        # they sent the signal.
        bot.request_shutdown()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # --- Hydrate + start_live --------------------------------------------
    hydration_summary: Optional[dict] = None
    try:
        hydration_summary = bot.hydrate()
        bot.start()
    except Exception:
        logger.exception("Hydration / start failed")
        bot.stop(inflight_timeout_sec=BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC)
        return 1

    # --- Subscription verification (post-start, async settling) ---------
    # H4 (adversarial review 2026-05-15): BotLoop.start() leaves state at
    # STARTING — events arriving during this window are dropped. Only
    # promote to NORMAL via mark_ready() after subscriptions confirm.
    sub_check = preflight_mod.verify_subscriptions(
        subscriber=subscriber,
        expected_pairs=config.pairs,
    )
    logger.info("preflight[%s]: ok=%s msg=%s", sub_check.name, sub_check.ok, sub_check.message)
    if not sub_check.ok:
        bot.stop(inflight_timeout_sec=BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC)
        return 1
    bot.mark_ready()

    # --- STARTUP alert (after mark_ready, before main-thread block) -----
    _emit_startup_alert(
        alerter=alerter,
        ig_env=ig_env,
        pairs=config.pairs,
        hydration_summary=hydration_summary or {"cached_bars": 0, "rest_bars": 0},
        shadow_mode=shadow_mode,
    )

    # --- Main thread: block on shutdown event ---------------------------
    logger.info("BotLoop running — awaiting shutdown signal")
    bot.shutdown_event().wait()

    # --- SHUTDOWN alert BEFORE bot.stop() drains the alerter ------------
    # Queueing the SHUTDOWN here means the bot.stop() path's
    # alerter.close() drains it as part of the normal shutdown. INFO
    # lands as the final "we're going down cleanly" line; CRITICAL
    # bypasses coalescing so it ships immediately even if the
    # close-drain is slow.
    crashed = bot.crashed
    _emit_shutdown_alert(alerter=alerter, crashed=crashed)

    # --- Graceful teardown ----------------------------------------------
    logger.info(
        "Beginning shutdown drain (timeout=%.1fs)",
        BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC,
    )
    bot.stop(
        inflight_timeout_sec=BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC,
        poll_sec=BOT_SHUTDOWN_DRAIN_POLL_SEC,
    )
    exit_code = 2 if crashed else 0
    logger.info("AutoBot-OG main() exiting with code %d", exit_code)
    return exit_code


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_config() -> BotRuntimeConfig:
    """Read top-level switches from .env / process env into a config dataclass."""
    pairs_env = os.getenv("BOT_PAIRS")
    pairs = (
        tuple(p.strip().upper() for p in pairs_env.split(",") if p.strip())
        if pairs_env
        else PAIRS
    )
    pair_to_epic = {p: f"CS.D.{p}.TODAY.IP" for p in pairs}
    return BotRuntimeConfig(
        pairs=pairs,
        pair_to_epic=pair_to_epic,
        log_level=BOT_LOG_LEVEL,
        log_file=BOT_LOG_FILE or None,
    )


def _build_runtime(
    config: BotRuntimeConfig,
    *,
    alerter: Optional[TelegramAlerter] = None,
    shadow_mode: bool = False,
) -> tuple[BotLoop, LightstreamerSubscriber]:
    """Wire all the Phase 1-7 components into a :class:`BotLoop`.

    The optional ``alerter`` is shared across :class:`Executor` and
    :class:`BotLoop` so every alert flows through the same coalescer
    and Telegram client. Tests that don't care about alerts can omit
    it; production wiring constructs one in :func:`main` and passes
    it here.

    Phase 10: ``shadow_mode`` (default false = safe) propagates to
    BotLoop only — the executor stays a narrow IG adapter and never
    knows about the deployment mode. The intercept lives in
    :py:meth:`BotLoop._evaluate_and_execute`.
    """
    # IG session + client.
    session = create_ig_service()
    ig_client = IGClient(session=session)

    # Persistent state.
    position_manager = PositionManager.load_from_path()

    # 2d: regime engines deleted. Nothing in the bot needs them
    # anymore — the dispatcher keys on day_type and the EOD rule keys
    # on structure htf_bias.
    risk_guard = RiskGuard()

    # Executor.
    def _epic_resolver(pair: str) -> str:
        epic = config.pair_to_epic.get(pair)
        if epic is None:
            raise KeyError(f"No epic mapping for pair {pair!r}")
        return epic

    executor = Executor(
        position_manager=position_manager,
        client=ig_client,
        epic_resolver=_epic_resolver,
        alerter=alerter,
    )

    # Feed manager — pulls the LS subscriber factory and the history
    # fetcher closures from the IG session.
    cst, xst = _extract_tokens(session)
    account_id = session.account_id or getattr(session.service, "ACC_NUMBER", None)
    if not account_id:
        raise RuntimeError("No IG account_id available for LS subscription")

    def _history_fetcher(epic: str, resolution: str, num_points: int) -> dict:
        return fetch_historical_prices(
            session, epic=epic, resolution=resolution, num_points=num_points,
        )

    # FeedManager doesn't expose the underlying LS subscriber, but we
    # need it for the post-start subscription check. The capturing
    # factory grabs the instance on the way through.
    captured: list[LightstreamerSubscriber] = []

    def _subscriber_factory(on_update, on_status) -> LightstreamerSubscriber:
        sub = LightstreamerSubscriber(
            acc_type=session.acc_type,
            account_id=account_id,
            cst=cst,
            xst=xst,
            on_update=on_update,
            on_status=on_status,
        )
        captured.append(sub)
        return sub

    from feed.feed_manager import PairSetup  # local import: avoid top noise

    feed_manager = FeedManager.from_pairs(
        [PairSetup(pair=p, epic=config.pair_to_epic[p]) for p in config.pairs],
        history_fetcher=_history_fetcher,
        subscriber_factory=_subscriber_factory,
    )

    bot = BotLoop(
        feed_manager=feed_manager,
        ig_client=ig_client,
        executor=executor,
        risk_guard=risk_guard,
        position_manager=position_manager,
        pairs=config.pairs,
        pair_to_epic=config.pair_to_epic,
        clock=lambda: datetime.now(timezone.utc),
        alerter=alerter,
        shadow_mode=shadow_mode,
    )

    # ``start_live`` creates the subscriber; until then ``captured`` is
    # empty. We expose a tiny duck-typed object with the single property
    # ``verify_subscriptions`` needs. L4 (Phase 8 follow-up): inline as
    # a SimpleNamespace-with-descriptor rather than a nested class.
    class _LateSubscriberView:
        __slots__ = ()

        @property
        def subscribed_pairs(self) -> tuple[str, ...]:
            return captured[0].subscribed_pairs if captured else ()

    return bot, _LateSubscriberView()  # type: ignore[return-value]


def _extract_tokens(session) -> tuple[str, str]:
    """Pull CST + X-SECURITY-TOKEN out of an IGSession's underlying service."""
    sess = getattr(session.service, "session", None)
    headers = getattr(sess, "headers", None) if sess is not None else None
    if not headers:
        raise RuntimeError(
            "IG service has no session headers — create_session() did not "
            "populate the token surface."
        )
    norm = {str(k).upper(): v for k, v in headers.items()}
    cst = norm.get("CST")
    xst = norm.get("X-SECURITY-TOKEN")
    if not cst or not xst:
        raise RuntimeError(
            "CST / X-SECURITY-TOKEN missing from IG session headers "
            f"(have: {sorted(norm.keys())})"
        )
    return cst, xst


# ---------------------------------------------------------------------------
# Alert payload builders
# ---------------------------------------------------------------------------


def _git_short_hash() -> str:
    """Return ``git rev-parse --short HEAD`` or ``"unknown"`` on any error.

    The bot may be deployed from a tarball (no ``.git`` dir) or run
    in a container that doesn't ship git — both are valid v1
    deployment shapes. Failing closed to ``"unknown"`` keeps the
    STARTUP alert alive in those environments instead of crashing
    the bot for the sake of an observability string.
    """
    return _git_command(["rev-parse", "--short", "HEAD"])


def _git_branch_name() -> str:
    """Return current branch (``--abbrev-ref HEAD``) or ``"unknown"``."""
    return _git_command(["rev-parse", "--abbrev-ref", "HEAD"])


def _git_command(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=2.0,
        )
    except Exception:
        return "unknown"
    out = (result.stdout or "").strip()
    return out or "unknown"


def _emit_startup_alert(
    *,
    alerter: TelegramAlerter,
    ig_env: str,
    pairs: tuple[str, ...],
    hydration_summary: dict,
    shadow_mode: bool = False,
) -> None:
    """Construct and dispatch the STARTUP alert.

    Format (as locked in the integration plan, plus the M3 degraded-
    pair suffix added in Phase 9 cleanup, plus the ``[SHADOW MODE]``
    title-line marker added in Phase 10):

        🤖 BOT STARTUP [SHADOW MODE]?
        Account: {DEMO|LIVE|?}
        Pairs: {N} ({pair list})
        Hydration: {cached_bars} cached, {rest_bars} REST [(degraded: <pair list>)]
        Build: {short_hash} ({branch})

    The ``[SHADOW MODE]`` suffix on the title line is the operator's
    immediate signal that no real trades will fire on this run —
    matching the boot-time WARNING the BotLoop logs in shadow mode.

    M7 (Session-3 commit-2b review): ``_git_short_hash`` and
    ``_git_branch_name`` shell out to ``git``, each with a 2s
    timeout. The pre-cleanup body called ``_git_short_hash`` twice
    (once in the body, once in the short_text), risking a
    cumulative ~6s STARTUP stall if git was unresponsive. We capture
    once into locals.
    """
    cached = int(hydration_summary.get("cached_bars", 0))
    rest = int(hydration_summary.get("rest_bars", 0))
    degraded = list(hydration_summary.get("degraded_pairs", []))
    pair_list = ", ".join(pairs) if pairs else "(none)"
    hydration_line = f"Hydration: {cached} cached, {rest} REST"
    if degraded:
        hydration_line += f" (degraded: {', '.join(degraded)})"
    git_hash = _git_short_hash()
    git_branch = _git_branch_name()
    title = "\U0001f916 BOT STARTUP"
    if shadow_mode:
        title += " [SHADOW MODE]"
    full = (
        f"{title}\n"
        f"Account: {ig_env}\n"
        f"Pairs: {len(pairs)} ({pair_list})\n"
        f"{hydration_line}\n"
        f"Build: {git_hash} ({git_branch})"
    )
    short = f"started ({git_hash}, {ig_env})"
    if shadow_mode:
        short += " [SHADOW]"
    alerter.send(
        Alert(
            category=AlertCategory.SYSTEM,
            event_subtype="STARTUP",
            severity=AlertSeverity.INFO,
            pair=None,
            full_text=full,
            short_text=short,
            timestamp=datetime.now(timezone.utc),
            debug={
                "ig_env": ig_env,
                "pairs": list(pairs),
                "shadow_mode": shadow_mode,
                "hydration": {
                    "cached_bars": cached,
                    "rest_bars": rest,
                    "degraded_pairs": degraded,
                },
            },
        )
    )


def _emit_startup_aborted_alert(
    *,
    alerter: TelegramAlerter,
    message: str,
    deal_ids: list[str],
) -> None:
    """CRITICAL alert for the H1 layer-1 refusal-to-start path.

    Severity CRITICAL bypasses coalescing so the alert ships
    immediately even if the alerter is closed in the same call
    chain. The body restates the abort message verbatim so the
    operator sees the offending deal_ids in Telegram.
    """
    alerter.send(
        Alert(
            category=AlertCategory.SYSTEM,
            event_subtype="STARTUP_ABORTED",
            severity=AlertSeverity.CRITICAL,
            pair=None,
            full_text=f"⛔ BOT STARTUP ABORTED\n{message}",
            short_text=(
                f"startup aborted: shadow_mode + {len(deal_ids)} "
                f"existing position(s)"
            ),
            timestamp=datetime.now(timezone.utc),
            debug={
                "reason": "shadow_mode_with_existing_positions",
                "deal_ids": list(deal_ids),
            },
        )
    )


def _read_shadow_mode_env() -> bool:
    """Read ``BOT_SHADOW_MODE`` env var with a strict truthy parse.

    Default false (safe). Accepts ``true|1|yes|on`` (case-insensitive)
    as truthy; everything else is false. Strict parsing means a typo
    like ``BOT_SHADOW_MODE=trues`` falls back to false (safe) rather
    than truthy-by-non-empty-string.
    """
    raw = (os.getenv("BOT_SHADOW_MODE") or "").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _emit_shutdown_alert(
    *,
    alerter: TelegramAlerter,
    crashed: bool,
) -> None:
    """Construct and dispatch the SHUTDOWN alert.

    Severity is INFO for clean shutdown, CRITICAL when the bot
    crashed (failure-threshold trip). CRITICAL bypasses coalescing
    so it ships immediately even if the close-drain is slow.
    """
    if crashed:
        full = "\U0001f916 BOT SHUTDOWN (CRASHED) — failure threshold tripped"
        short = "shutdown (crashed)"
        severity = AlertSeverity.CRITICAL
    else:
        full = "\U0001f916 BOT SHUTDOWN — clean exit"
        short = "shutdown (clean)"
        severity = AlertSeverity.INFO
    alerter.send(
        Alert(
            category=AlertCategory.SYSTEM,
            event_subtype="SHUTDOWN",
            severity=severity,
            pair=None,
            full_text=full,
            short_text=short,
            timestamp=datetime.now(timezone.utc),
            debug={"crashed": crashed},
        )
    )


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
