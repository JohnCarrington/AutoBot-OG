"""Persisted state for the risk-layer circuit breakers.

Tracks counter + cooldown information that must survive process
restarts. JSON-backed at ``data/risk/circuit_breaker_state.json`` by
default (the ``data/`` tree is gitignored). Tests pass a temp path
explicitly.

Concurrency: this file is written by a single bot instance only. v1 has
no multi-writer requirement; the JSON write is a plain truncate-and-
write. If multi-process writes become a need in v2, switch to
``tempfile`` + ``os.replace`` for atomicity.

Corruption handling: load failures (missing file, malformed JSON,
unparseable datetime) all degrade to a *fresh* state with a warning
logged. Failing-open is the v1-spec-aligned default; the next trade
outcome / DD trigger will repopulate the relevant fields.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ..constants import NY_CLOSE_HOUR_LOCAL, NY_TZ_NAME


logger = logging.getLogger(__name__)


DEFAULT_STATE_PATH: Path = Path("data/risk/circuit_breaker_state.json")


def current_session_date_ny(now_utc: datetime) -> date:
    """Return the NY-session date that ``now_utc`` falls into.

    The FX trading session "ends" at 17:00 America/New_York
    (``NY_CLOSE_HOUR_LOCAL``). The session is labelled by the date on
    which it ends. So:

    - Monday 14:00 ET   → session label = Monday's date.
    - Monday 17:00 ET   → session label = Tuesday's date (new session
      just started).
    - Tuesday 09:00 ET  → session label = Tuesday's date.

    Daily-drawdown state keys off this label so the DD resets at NY
    close, DST-aware via :mod:`zoneinfo`.
    """
    ny = ZoneInfo(NY_TZ_NAME)
    now_ny = now_utc.astimezone(ny)
    if now_ny.time() >= time(NY_CLOSE_HOUR_LOCAL, 0):
        return now_ny.date() + timedelta(days=1)
    return now_ny.date()


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning(
            "[risk-state] could not parse datetime %r — dropping", value
        )
        return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning(
            "[risk-state] could not parse date %r — dropping", value
        )
        return None


@dataclass
class CircuitBreakerState:
    """Persistent state for the three circuit breakers.

    Fields are tagged for direct JSON round-trip. Updates set ``_dirty
    = True``; :py:meth:`save_if_dirty` is called by the
    :py:class:`risk.guard.RiskGuard` after each rule pass that may have
    mutated state.

    Attributes
    ----------
    loss_streak : int
        Number of consecutive losing trades observed. Resets to 0 on
        any winning trade (PnL_R > 0); incremented on any non-positive
        outcome (PnL_R <= 0).
    consecutive_loss_cooldown_until_utc : Optional[datetime]
        Set when ``loss_streak`` first reaches
        :data:`risk.constants.CONSECUTIVE_LOSS_THRESHOLD`. Cleared
        automatically when the cooldown elapses on the next check.
    daily_dd_session_date : Optional[date]
        NY-session date the DD state belongs to. When the current
        session date differs from this on a check, the DD-related
        fields are cleared (new session = fresh DD budget).
    daily_dd_cooldown_until_utc : Optional[datetime]
        Set when realised + unrealised R for the session reaches
        :data:`risk.constants.DAILY_DD_LIMIT_R`. By convention set to
        the next session boundary (next NY close) so it auto-clears
        when ``daily_dd_session_date`` rolls.
    regime_instability_cooldown_until_utc : Optional[datetime]
        Primary 1-hour cooldown after exceeding the commit-or-reset
        thresholds. After this elapses, the regime-live check kicks in.
    regime_instability_pair : Optional[str]
        Pair the instability cooldown applies to. v1 single-pair, but
        the data model is per-pair.
    """

    path: Path = field(default=DEFAULT_STATE_PATH)
    loss_streak: int = 0
    consecutive_loss_cooldown_until_utc: Optional[datetime] = None
    daily_dd_session_date: Optional[date] = None
    daily_dd_cooldown_until_utc: Optional[datetime] = None
    regime_instability_cooldown_until_utc: Optional[datetime] = None
    regime_instability_pair: Optional[str] = None
    _dirty: bool = field(default=False, repr=False)

    # --- Construction --------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str | None = None) -> "CircuitBreakerState":
        """Load state from disk, or return a fresh state if absent / bad.

        Missing file is the normal first-run case. Malformed JSON or
        unparseable datetimes log a warning and degrade to fresh — the
        v1 design treats the on-disk state as a cache of "what
        happened", not a contract. Failing open lets the bot start.
        """
        p = Path(path) if path is not None else DEFAULT_STATE_PATH
        if not p.exists():
            return cls(path=p)
        try:
            with p.open("r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[risk-state] could not load %s (%s) — starting fresh", p, exc
            )
            return cls(path=p)
        return cls(
            path=p,
            loss_streak=int(data.get("loss_streak", 0)),
            consecutive_loss_cooldown_until_utc=_parse_dt(
                data.get("consecutive_loss_cooldown_until_utc")
            ),
            daily_dd_session_date=_parse_date(
                data.get("daily_dd_session_date")
            ),
            daily_dd_cooldown_until_utc=_parse_dt(
                data.get("daily_dd_cooldown_until_utc")
            ),
            regime_instability_cooldown_until_utc=_parse_dt(
                data.get("regime_instability_cooldown_until_utc")
            ),
            regime_instability_pair=data.get("regime_instability_pair"),
        )

    # --- Persistence ---------------------------------------------------------

    def save(self) -> None:
        """Write state to disk unconditionally."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "loss_streak": self.loss_streak,
            "consecutive_loss_cooldown_until_utc": _iso_or_none(
                self.consecutive_loss_cooldown_until_utc
            ),
            "daily_dd_session_date": (
                self.daily_dd_session_date.isoformat()
                if self.daily_dd_session_date is not None
                else None
            ),
            "daily_dd_cooldown_until_utc": _iso_or_none(
                self.daily_dd_cooldown_until_utc
            ),
            "regime_instability_cooldown_until_utc": _iso_or_none(
                self.regime_instability_cooldown_until_utc
            ),
            "regime_instability_pair": self.regime_instability_pair,
        }
        with self.path.open("w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        self._dirty = False

    def save_if_dirty(self) -> None:
        """Persist only if ``_dirty`` was set since the last save."""
        if self._dirty:
            self.save()

    def mark_dirty(self) -> None:
        """Flag the state as needing persistence. Rules call this."""
        self._dirty = True

    # --- Session rollover ----------------------------------------------------

    def reset_daily_dd_if_new_session(self, now_utc: datetime) -> bool:
        """If the NY session has rolled, clear DD-related fields.

        Returns ``True`` when a reset happened (so the caller knows the
        state needs persisting).
        """
        current = current_session_date_ny(now_utc)
        if self.daily_dd_session_date == current:
            return False
        self.daily_dd_session_date = current
        self.daily_dd_cooldown_until_utc = None
        self.mark_dirty()
        return True

    # --- Equality helpers (for tests) ---------------------------------------

    def to_serialisable_dict(self) -> dict:
        """Public dict view for tests / diagnostics."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_") and f.name != "path"
        }


__all__ = [
    "CircuitBreakerState",
    "DEFAULT_STATE_PATH",
    "current_session_date_ny",
]


# `timezone` is imported above for forward-compatible code (Phase 5 calls).
_ = timezone
