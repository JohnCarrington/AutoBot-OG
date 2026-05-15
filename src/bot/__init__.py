"""bot — Phase 8 main loop, public surface.

The orchestrator that ties Phase 1-7 together. Consumes
:class:`feed.FeedEvent` instances, runs the indicator → structure →
regime → strategy → risk → execution pipeline, manages SL amends, and
fires periodic reconciliation + Phase-4-owned EOD enforcement.

Public surface:

- :class:`BotLoop` — the orchestrator. Instantiated by :func:`main`
  with all Phase 1-7 dependencies injected.
- :class:`BotState` — lifecycle states the loop transitions through.
- :func:`main` — process entrypoint. Reads ``.env``, runs pre-flight,
  starts the bot, blocks on shutdown, drains cleanly.
"""
from .loop import BotLoop
from .main import main
from .types import BotState

__all__ = ["BotLoop", "BotState", "main"]
