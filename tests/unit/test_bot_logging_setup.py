"""Tests for bot.logging_setup."""
from __future__ import annotations

import logging
from pathlib import Path

from bot.logging_setup import setup_logging


def test_setup_logging_installs_stderr_handler() -> None:
    setup_logging(level="WARNING")
    root = logging.getLogger()
    # Exactly one StreamHandler.
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
    assert len(stream_handlers) == 1
    assert root.level == logging.WARNING


def test_setup_logging_optional_file_handler(tmp_path: Path) -> None:
    log_path = tmp_path / "bot.log"
    setup_logging(level="INFO", log_file=str(log_path))
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    logger = logging.getLogger("bot.test")
    logger.info("hello")
    file_handlers[0].flush()
    content = log_path.read_text()
    assert "hello" in content


def test_setup_logging_is_idempotent() -> None:
    setup_logging(level="INFO")
    setup_logging(level="INFO")
    setup_logging(level="INFO")
    root = logging.getLogger()
    # Repeated calls do not stack handlers.
    assert len(root.handlers) == 1


def test_setup_logging_silences_noisy_packages() -> None:
    setup_logging(level="DEBUG")
    for name in ("urllib3", "trading_ig", "lightstreamer"):
        assert logging.getLogger(name).level == logging.WARNING


def test_setup_logging_replaces_old_handlers_on_reconfig() -> None:
    setup_logging(level="INFO")
    handlers_before = list(logging.getLogger().handlers)
    setup_logging(level="DEBUG")
    handlers_after = list(logging.getLogger().handlers)
    # Different handler instances each time — idempotent but fresh.
    assert handlers_before != handlers_after
