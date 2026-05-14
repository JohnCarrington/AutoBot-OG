# alerts

Outbound notifications.

## Owns
- Telegram bot client
- Trade-event messages (entry, exit, SL move)
- Error and circuit-breaker notifications (DD stop hit, cooldown engaged, news blackout, feed disconnect)
- Daily summary message

## Does NOT own
- Deciding *when* to alert — callers invoke; this module formats and ships.
- Persistence of alert history (logging belongs elsewhere)
