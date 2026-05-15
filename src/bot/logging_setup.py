"""Logging configuration for the Phase 8 bot main loop.

One public entrypoint: :func:`setup_logging`. Called once at the top
of :func:`bot.main.main` *before* any other initialisation so that
pre-flight failures, signal handler installation, and credential
loading all land in the same configured log stream.

Conventions:

- Root logger at the requested level; per-package overrides set to
  ``WARNING`` for known-noisy third parties (``urllib3``, ``trading_ig``,
  ``lightstreamer``) regardless of the root level.
- Default format ``"%(asctime)s %(levelname)s %(name)s: %(message)s"``
  with ISO-8601 timestamps. Override via ``BOT_LOG_FORMAT``.
- Outputs: always one ``StreamHandler(sys.stderr)``; an optional
  ``FileHandler`` when ``log_file`` is non-empty.
- Idempotent: re-invocations clear and re-install handlers, so tests
  can call :func:`setup_logging` per-case without leaking handlers.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

from .constants import (
    BOT_LOG_DATEFMT,
    BOT_LOG_FORMAT,
    BOT_LOG_LEVEL,
)


# Packages whose default INFO output is too chatty for production logs.
_NOISY_PACKAGES = (
    "urllib3",
    "urllib3.connectionpool",
    "requests",
    "trading_ig",
    "lightstreamer",
    "lightstreamer.client",
)


def setup_logging(
    level: Optional[str] = None,
    *,
    log_file: Optional[str] = None,
    fmt: Optional[str] = None,
    datefmt: Optional[str] = None,
) -> None:
    """Configure the root logger.

    Parameters default to the :py:mod:`bot.constants` env-driven
    values; pass explicit overrides in tests to inspect the resulting
    handler set without touching the environment.
    """
    resolved_level = (level or BOT_LOG_LEVEL).upper()
    resolved_fmt = fmt or BOT_LOG_FORMAT
    resolved_datefmt = datefmt or BOT_LOG_DATEFMT

    formatter = logging.Formatter(resolved_fmt, resolved_datefmt)

    root = logging.getLogger()
    # Idempotent re-config: clear any handlers we (or a prior test)
    # installed so we don't double-emit every line.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(resolved_level)

    # Tamp down third-party noise regardless of root level.
    for name in _NOISY_PACKAGES:
        logging.getLogger(name).setLevel(logging.WARNING)


__all__ = ["setup_logging"]
