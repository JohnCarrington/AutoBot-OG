"""Per-pair on-disk candle archive (Phase 7).

One append-only CSV per pair under :data:`feed.constants.FEED_ARCHIVE_DIR`
(default ``data/candles/{pair}_5m.csv``). The archive is the cold-start
input for hydration — the rolling buffer is reseeded from the cache
file on every startup and topped up via REST only when the cache is
stale or short.

Design choices:

- **Append-only** — never rewrite, never sort. Each new bar gets a
  single ``open()/write()/close()`` round trip. Crash-safety is
  delegated to the OS: a partially-written final line is detected on
  the next read and discarded (see :py:meth:`load`).
- **File-tail dedup** — on every :py:meth:`append`, we keep the
  ``close_time_ms`` of the last successfully-written bar in memory.
  New bars with ``close_time_ms <= _last_ts`` are silently skipped.
  This is the "belt-and-braces" half of the two-layer dedup: the
  feed manager has already decided ``BAR_UPDATE`` vs ``BAR_CLOSE``,
  but if the manager ever ships a duplicate the archive still won't
  grow.
- **Atomic header** — the header row is written only when the file
  doesn't exist yet. Re-creating the archive (after manual deletion)
  rebuilds it cleanly without prepending a duplicate header.
- **Recoverable corruption** — :py:meth:`load` swallows
  parser errors per-row. The expected failure mode is a half-written
  trailing line after a SIGKILL; logging the row count helps
  diagnose anything more exotic.

Schema (locked, do not reorder):

    close_time, close_time_ms, open, high, low, close, volume

``close_time`` is the ISO-8601 UTC string for human-readable greps;
``close_time_ms`` is the integer epoch-ms duplicate consumed by the
fast-path dedup so we never have to parse the ISO column on the hot
path.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .constants import (
    FEED_ARCHIVE_COLUMNS,
    FEED_ARCHIVE_CSV_TEMPLATE,
    FEED_ARCHIVE_DIR,
)
from .types import Candle

logger = logging.getLogger(__name__)


def _ms(dt: datetime) -> int:
    """Convert a tz-aware datetime to epoch milliseconds (UTC)."""
    if dt.tzinfo is None:
        # Treating naive datetimes as UTC mirrors how the LS feed parser
        # constructs Candle.close_time. A misuse in tests would surface
        # as a wrong _last_ts comparison rather than silently passing,
        # so we coerce instead of raising.
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


class CandleArchive:
    """Append-only CSV writer + loader for a single pair.

    Parameters
    ----------
    pair : str
        Symbol used to build the CSV filename via
        :data:`FEED_ARCHIVE_CSV_TEMPLATE`.
    base_dir : str | Path, optional
        Override the default :data:`FEED_ARCHIVE_DIR` — useful in
        tests with ``tmp_path``.
    """

    def __init__(self, pair: str, base_dir: Optional[str | Path] = None) -> None:
        self._pair = pair
        self._base_dir = Path(base_dir) if base_dir is not None else Path(FEED_ARCHIVE_DIR)
        self._path = self._base_dir / FEED_ARCHIVE_CSV_TEMPLATE.format(pair=pair)
        self._last_ts: Optional[int] = None
        # Eagerly read the last timestamp on construction so the first
        # append() correctly dedupes even before any load() call.
        self._refresh_last_ts_from_disk()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pair(self) -> str:
        return self._pair

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_close_time_ms(self) -> Optional[int]:
        """Most recently written bar's epoch-ms timestamp, or ``None``."""
        return self._last_ts

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, candle: Candle) -> bool:
        """Append ``candle`` to the CSV. Returns ``True`` if written.

        Returns ``False`` (silently) when the candle's close-time is
        not strictly newer than the most recent stored bar — this is
        the file-tail dedup. The :class:`feed.feed_manager.FeedManager`
        is expected to already filter ``BAR_UPDATE`` events; this
        guard catches duplicates from gap-fill overlaps and the
        occasional out-of-order REST response.

        Raises
        ------
        ValueError
            If ``candle.pair`` does not match this archive's pair.
        """
        if candle.pair != self._pair:
            raise ValueError(
                f"CandleArchive for {self._pair!r} got candle for "
                f"{candle.pair!r}"
            )
        ts_ms = _ms(candle.close_time)
        if self._last_ts is not None and ts_ms <= self._last_ts:
            return False
        self._ensure_parent_dir()
        is_new_file = not self._path.exists()
        with self._path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if is_new_file:
                writer.writerow(FEED_ARCHIVE_COLUMNS)
            writer.writerow(
                [
                    candle.close_time.astimezone(timezone.utc).isoformat(),
                    ts_ms,
                    f"{candle.open:.6f}",
                    f"{candle.high:.6f}",
                    f"{candle.low:.6f}",
                    f"{candle.close:.6f}",
                    f"{candle.volume:.6f}",
                ]
            )
        self._last_ts = ts_ms
        return True

    def append_many(self, candles: Iterable[Candle]) -> int:
        """Append a sequence of candles; return the number actually written."""
        written = 0
        for c in candles:
            if self.append(c):
                written += 1
        return written

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self, limit: Optional[int] = None) -> list[Candle]:
        """Load the archive into a list of :class:`Candle`.

        Returns ``[]`` if the file does not exist. Malformed rows
        (wrong column count, unparseable price, half-written trailing
        line after a crash) are skipped with a warning — the loader
        never raises on bad data, so a corrupt tail never blocks
        startup.

        ``limit`` returns only the most recent N bars when set. This
        lets the hydration path read just what it needs without
        scanning multi-month archives.
        """
        if not self._path.exists():
            return []
        candles: list[Candle] = []
        bad_rows = 0
        try:
            with self._path.open("r", newline="") as fh:
                reader = csv.reader(fh)
                header: Optional[list[str]] = None
                for row in reader:
                    if header is None:
                        header = row
                        if header != list(FEED_ARCHIVE_COLUMNS):
                            logger.warning(
                                "CandleArchive %s: unexpected header %r; "
                                "treating row as data",
                                self._path,
                                row,
                            )
                            # If the first row isn't the locked header,
                            # try parsing it as data — supports archives
                            # that were started without one.
                            parsed = self._parse_row(row)
                            if parsed is not None:
                                candles.append(parsed)
                            else:
                                bad_rows += 1
                        continue
                    parsed = self._parse_row(row)
                    if parsed is None:
                        bad_rows += 1
                        continue
                    candles.append(parsed)
        except OSError as exc:
            logger.error(
                "CandleArchive %s: failed to open for read: %s",
                self._path,
                exc,
            )
            return []
        if bad_rows:
            logger.warning(
                "CandleArchive %s: skipped %d malformed row(s) during load",
                self._path,
                bad_rows,
            )
        if limit is not None and len(candles) > limit:
            candles = candles[-limit:]
        return candles

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse_row(self, row: list[str]) -> Optional[Candle]:
        """Parse a CSV row → :class:`Candle`; return ``None`` on failure."""
        if len(row) != len(FEED_ARCHIVE_COLUMNS):
            return None
        try:
            # Prefer the epoch-ms column for reconstruction — it survives
            # any quirk in the ISO column's tz formatting.
            ts_ms = int(row[1])
            close_time = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            return Candle(
                pair=self._pair,
                close_time=close_time,
                open=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                close=float(row[5]),
                volume=float(row[6]),
                source="REST",  # archive contents are treated as REST-grade
            )
        except (ValueError, TypeError):
            return None

    def _ensure_parent_dir(self) -> None:
        if not self._base_dir.exists():
            self._base_dir.mkdir(parents=True, exist_ok=True)

    def _refresh_last_ts_from_disk(self) -> None:
        """Populate ``_last_ts`` by reading the file's last valid row.

        Reads the whole file once at construction. The archive size is
        bounded by trading history (a year of M5 ≈ 75k rows), so even
        a multi-year file fits in memory comfortably. If the read fails
        we fall back to ``None`` and let :py:meth:`append` write
        without a dedup guard until the first new bar lands.
        """
        if not self._path.exists():
            return
        try:
            existing = self.load()
        except Exception as exc:
            logger.warning(
                "CandleArchive %s: load failed during init: %s — proceeding "
                "without _last_ts guard",
                self._path,
                exc,
            )
            return
        if existing:
            self._last_ts = _ms(existing[-1].close_time)


__all__ = ["CandleArchive"]


# Make sure the default archive directory exists at import time so
# the first call to ``append()`` never trips a missing-parent FileNotFoundError
# during a happy-path startup. This is intentional: by import time the
# user has decided to use the archive, and the directory is gitignored
# (see .gitignore for data/). Skipped when the directory string is
# overridden in tests via env var (FEED_ARCHIVE_DIR), since tests
# typically point at tmp_path themselves.
if os.getenv("FEED_ARCHIVE_DIR") is None:
    try:
        Path(FEED_ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only filesystem during test collection or build — quietly
        # skip; the first append() will retry.
        pass
