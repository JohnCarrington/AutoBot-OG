"""Pre-flight check list for the Phase 8 bot main loop.

The job of :func:`run` is to fail loud and early when the runtime is
misconfigured (missing env vars, unwritable disk, broken IG auth)
instead of crashing mid-trade. Each check returns a
:class:`bot.types.CheckResult`; the first failure short-circuits the
chain so an unauthenticated session never reaches the
"verify subscriptions" step.

The bot calls this in two places:

- :func:`run_static_checks` — before :py:meth:`BotLoop.start`. Covers
  env vars, disk writability, IG auth, persisted state. If anything
  fails the bot exits 1 without ever opening a Lightstreamer session.
- :func:`verify_subscriptions` — after :py:meth:`BotLoop.start`. The
  LS subscription state settles asynchronously, so this polls
  :py:attr:`LightstreamerSubscriber.subscribed_pairs` for up to
  :data:`BOT_PREFLIGHT_SUBSCRIPTION_TIMEOUT_SEC` seconds. Missing
  pairs at the deadline → exit 1.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from feed.constants import FEED_ARCHIVE_DIR
from feed.ig_rest.auth import IGSession, load_ig_credentials
from feed.lightstreamer.client import LightstreamerSubscriber

from .constants import (
    BOT_PREFLIGHT_SUBSCRIPTION_POLL_SEC,
    BOT_PREFLIGHT_SUBSCRIPTION_TIMEOUT_SEC,
)
from .types import CheckResult, PreFlightReport

logger = logging.getLogger(__name__)


# Required env vars for v1. IG auth checks (4) consumes these; we
# fail fast on missing ones rather than letting load_ig_credentials
# raise a misleading "auth failed" later.
_REQUIRED_ENV_VARS = (
    "IG_USERNAME",
    "IG_PASSWORD",
    "IG_API_KEY",
    "IG_ACC_TYPE",
)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


SessionFactory = Callable[[], IGSession]
"""Test seam for :func:`run_static_checks` — defaults to
:py:func:`feed.ig_rest.auth.create_ig_service`. Tests inject a stub so
no live network call happens."""


def run_static_checks(
    *,
    archive_dir: Optional[str] = None,
    execution_state_dir: Optional[str] = None,
    session_factory: Optional[SessionFactory] = None,
) -> PreFlightReport:
    """Run every pre-LS-connect check; return aggregate report.

    Parameters
    ----------
    archive_dir, execution_state_dir : str, optional
        Override the default paths checked for write access. Defaults
        to ``data/candles/`` and ``data/execution/``.
    session_factory : callable, optional
        Test seam — return an :py:class:`IGSession`. If omitted, calls
        :py:func:`feed.ig_rest.auth.create_ig_service` for real.
    """
    results: list[CheckResult] = []

    # 1. Required env vars present.
    missing = [name for name in _REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        results.append(
            CheckResult(
                name="env_vars",
                ok=False,
                message=f"Missing required env vars: {', '.join(missing)}",
            )
        )
        return PreFlightReport(results=tuple(results))
    results.append(
        CheckResult(name="env_vars", ok=True, message="all required vars present")
    )

    # 2. Disk writability for the two persistent stores.
    archive_path = Path(archive_dir or FEED_ARCHIVE_DIR)
    exec_path = Path(execution_state_dir or "data/execution")
    for label, path in (("archive_dir", archive_path), ("execution_state_dir", exec_path)):
        ok, msg = _check_writable(path)
        results.append(CheckResult(name=label, ok=ok, message=msg))
        if not ok:
            return PreFlightReport(results=tuple(results))

    # 3. IG credentials parseable (cheap — just env-read).
    try:
        load_ig_credentials()
    except Exception as exc:
        results.append(
            CheckResult(
                name="ig_credentials_parse",
                ok=False,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return PreFlightReport(results=tuple(results))
    results.append(
        CheckResult(
            name="ig_credentials_parse",
            ok=True,
            message="env credentials parsed",
        )
    )

    # 4. IG REST auth round-trip (real network call in production;
    # test seam returns a stub).
    factory = session_factory or (lambda: _default_session_factory())
    try:
        session = factory()
        if not session.account_id and not getattr(session.service, "ACC_NUMBER", None):
            raise RuntimeError(
                "IG session has no account_id — set IG_ACCOUNT_ID or ensure "
                "IGService.ACC_NUMBER populates after create_session()."
            )
    except Exception as exc:
        results.append(
            CheckResult(
                name="ig_session",
                ok=False,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return PreFlightReport(results=tuple(results))
    results.append(
        CheckResult(name="ig_session", ok=True, message="IG session created")
    )

    return PreFlightReport(results=tuple(results))


def verify_subscriptions(
    subscriber: LightstreamerSubscriber,
    expected_pairs: Iterable[str],
    *,
    timeout_sec: float = BOT_PREFLIGHT_SUBSCRIPTION_TIMEOUT_SEC,
    poll_sec: float = BOT_PREFLIGHT_SUBSCRIPTION_POLL_SEC,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Poll the subscriber until every expected pair is subscribed.

    Returns a :class:`CheckResult` describing the outcome. ``ok=True``
    iff every pair appeared in ``subscriber.subscribed_pairs`` within
    ``timeout_sec``. The default ``sleep`` / ``clock`` are injectable
    so tests can drive synthetic time.
    """
    expected = set(expected_pairs)
    deadline = clock() + timeout_sec
    last_seen: tuple[str, ...] = ()
    while clock() < deadline:
        last_seen = subscriber.subscribed_pairs
        if expected.issubset(set(last_seen)):
            return CheckResult(
                name="ls_subscriptions",
                ok=True,
                message=f"all {len(expected)} pair(s) subscribed",
            )
        sleep(poll_sec)
    missing = sorted(expected - set(last_seen))
    return CheckResult(
        name="ls_subscriptions",
        ok=False,
        message=(
            f"timed out after {timeout_sec:.1f}s waiting for "
            f"subscriptions; missing: {missing}; saw: {sorted(last_seen)}"
        ),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _check_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"mkdir({path}) failed: {exc}"
    probe = path / ".preflight_probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return False, f"write probe in {path} failed: {exc}"
    return True, f"{path} writable"


def _default_session_factory() -> IGSession:
    # Local import so the heavyweight ``trading_ig`` import only fires
    # when we're actually about to authenticate, not at module load.
    from feed.ig_rest.auth import create_ig_service

    return create_ig_service()


__all__ = [
    "SessionFactory",
    "run_static_checks",
    "verify_subscriptions",
]
