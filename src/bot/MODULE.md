# bot

Top-level orchestration: the main event loop and scheduler.

## Owns
- Application entry point and run loop
- Tick / candle event dispatch to downstream modules
- Lifecycle: startup, shutdown, graceful restart
- Wiring feed → regime → strategies → risk → execution → alerts

## Does NOT own
- Indicator math (`indicators/`)
- Regime classification logic (`regime/`)
- Strategy entry/exit rules (`strategies/`)
- Risk gates (`risk/`) or order placement (`execution/`)
- Broker connectivity (`feed/`)
