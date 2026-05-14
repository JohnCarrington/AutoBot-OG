# execution

Order placement, broker-side SL management, position tracking, and
reconciliation against IG.

## Owns
- :py:class:`Executor` — translates approved :py:class:`Signal` →
  broker open via :py:class:`feed.ig_rest.IGClient`.
- :py:class:`PositionManager` — in-memory `dict[deal_id, ExecutionPosition]`
  with by-pair and by-signal-source indices, JSON persistence.
- :py:func:`evaluate_sl_amend` — pure SL-management decision (BE move
  at +1R with buffer + structure / EMA20 trail).
- :py:func:`reconcile` — conservative broker-vs-local divergence pass.
- :py:class:`ExecutionPosition` and adapter to :py:class:`risk.types.OpenPosition`
  via ``to_risk_open_position(current_price)``.

## Public surface
- ``Executor``, ``PositionManager``, ``PositionsState``
- ``evaluate_sl_amend``, ``reconcile``
- ``ExecutionPosition``, ``AmendOrder``, ``AmendResult``,
  ``TradeOrder``, ``TradeResult``, ``SLAmendment``
- ``ReconciliationEvent``, ``ReconciliationKind``,
  ``ReconciliationReport``, ``ReconciliationSeverity``,
  ``ReconciliationActions``, ``ReconciliationOutcome``

## Sub-modules
- ``executor.py`` — open + amend orchestrator with retry-once policy.
- ``position_manager.py`` — `PositionManager` + indices.
- ``sl_management.py`` — `evaluate_sl_amend` (pure function).
- ``reconciliation.py`` — `reconcile` (pure function).
- ``state/positions_state.py`` — atomic JSON persistence.
- ``types.py`` — dataclasses + enums.
- ``constants.py`` — env-overridable tunables.

## Does NOT own
- Regime / strategy / risk decisions (`regime/`, `strategies/`, `risk/`).
- IG REST protocol (`feed/ig_rest/`) — execution depends on
  :py:class:`feed.ig_rest.IGClient`, which is the single seam.
- Alert forwarding (`alerts/`, Phase 7) — execution writes
  :py:class:`ReconciliationEvent` to a jsonl log; the Phase 7
  alerts module reads it.
- Position sizing-by-balance — v1 uses a fixed
  ``EXECUTION_DEFAULT_SIZE_UNITS = 1.0``; Phase 7+ may add account-
  driven sizing.

See ``docs/v1_architecture.md`` §6.3.1 (locked SL decisions) and
§6.11 (Phase 6 module structure).
