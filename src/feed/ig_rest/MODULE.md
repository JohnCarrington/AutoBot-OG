# feed/ig_rest

Thin wrapper around `trading_ig.IGService` (pinned `==0.0.16`). The
sole purpose is to expose a single, typed, mockable surface so the
execution and Phase 7 layers can call IG without touching the
third-party library directly.

## Owns
- :py:class:`IGClient` — composed REST client with allowance gating.
- :py:func:`create_ig_service` — env-driven session factory.
- Request dataclasses: :py:class:`OrderRequest`, :py:class:`AmendRequest`,
  :py:class:`CloseRequest`.
- Response dataclasses: :py:class:`BrokerPosition`,
  :py:class:`DealConfirmation`, :py:class:`MarketInfo`.
- :py:class:`AllowanceTracker` — pure trailing-window REST counter
  with escalating backoff (legacy ``BACKOFF_SCHEDULE`` replacement).
- Position / market / history primitives that turn the library's
  dict payloads into our typed responses.

## Public surface
- ``IGClient`` (+ ``AllowanceExceeded`` exception)
- ``IGSession``, ``IGCredentials``, ``create_ig_service``, ``load_ig_credentials``
- ``AllowanceTracker``, ``AllowanceSnapshot``
- Request / response dataclasses (see above)

## Sub-modules
- ``auth.py`` — env loader + IGService factory.
- ``client.py`` — :py:class:`IGClient`.
- ``positions.py`` — open / amend / close / read primitives.
- ``markets.py`` — market-info lookup.
- ``history.py`` — historical bars (Phase 7).
- ``allowance.py`` — REST allowance tracker.
- ``types.py`` — request / response shapes.

## Does NOT own
- Trade decisions or position state — those live in
  ``src/execution/``.
- Lightstreamer / streaming — Phase 7 will add a separate sub-package.

## Dependency pin
``trading_ig==0.0.16`` — discovered from the production bot's
``requirements.txt``. The legacy ``ig_auth.py`` shims are written
against this exact version.
