"""Position-cap rule (§6.8).

Three independent caps:

- Global: at most ``MAX_GLOBAL_POSITIONS`` open positions across all pairs.
- Per-pair: at most ``MAX_PER_PAIR`` positions on the candidate pair.
- Per-strategy: at most ``MAX_PER_STRATEGY`` positions per strategy
  (B-4, 2c). Replaces the prior per-regime cap — the day-type spine
  doesn't carry the "no two TREND positions" intent anymore; the spec
  intent is preserved by binding the cap to the strategy that fires.
"""
from __future__ import annotations

from ..constants import MAX_GLOBAL_POSITIONS, MAX_PER_PAIR, MAX_PER_STRATEGY
from ..types import CandidateTrade, OpenPosition, RuleResult


_RULE_NAME = "position_caps"


def check_position_caps(
    candidate: CandidateTrade,
    positions: list[OpenPosition],
) -> RuleResult:
    """Allow only if adding ``candidate`` would not exceed any cap.

    Pure function. Position counts are computed by scanning the supplied
    ``positions`` list — the caller is responsible for keeping that list
    in sync with broker state before each call.
    """
    if len(positions) >= MAX_GLOBAL_POSITIONS:
        return RuleResult(
            allow=False,
            rule=_RULE_NAME,
            reason=(
                f"global cap reached: {len(positions)} open positions "
                f"(max {MAX_GLOBAL_POSITIONS})"
            ),
        )

    same_pair = [p for p in positions if p.pair == candidate.pair]
    if len(same_pair) >= MAX_PER_PAIR:
        return RuleResult(
            allow=False,
            rule=_RULE_NAME,
            reason=(
                f"per-pair cap reached for {candidate.pair}: "
                f"{len(same_pair)} open (max {MAX_PER_PAIR})"
            ),
        )

    same_strategy = [
        p for p in positions if p.strategy_name == candidate.strategy_name
    ]
    if len(same_strategy) >= MAX_PER_STRATEGY:
        return RuleResult(
            allow=False,
            rule=_RULE_NAME,
            reason=(
                f"per-strategy cap reached for "
                f"{candidate.strategy_name}: "
                f"{len(same_strategy)} open (max {MAX_PER_STRATEGY})"
            ),
        )

    return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")


__all__ = ["check_position_caps"]
