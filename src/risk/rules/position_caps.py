"""Position-cap rule (§6.8).

Three independent caps:

- Global: at most ``MAX_GLOBAL_POSITIONS`` open positions across all pairs.
- Per-pair: at most ``MAX_PER_PAIR`` positions on the candidate pair.
- Per-regime: at most ``MAX_PER_REGIME`` positions in the candidate
  regime. The spec ("no duplicate regime stacking") means we cannot
  stack two TREND positions even if they are on different pairs in v2.
  v1 is GBPUSD-only so this collapses to a single-position check, but
  the data model is per-regime forward-compatible.
"""
from __future__ import annotations

from ..constants import MAX_GLOBAL_POSITIONS, MAX_PER_PAIR, MAX_PER_REGIME
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

    # 2a: fields renamed; behaviour preserved — comparison is
    # self-equality (DayType==DayType or RegimeLabel==RegimeLabel),
    # which works under either type while values are mixed during the
    # 2a/2b transition.
    same_day_type = [
        p
        for p in positions
        if p.day_type_at_entry == candidate.intended_day_type
    ]
    if len(same_day_type) >= MAX_PER_REGIME:
        return RuleResult(
            allow=False,
            rule=_RULE_NAME,
            reason=(
                f"per-regime cap reached for "
                f"{candidate.intended_day_type.value}: "
                f"{len(same_day_type)} open (max {MAX_PER_REGIME})"
            ),
        )

    return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")


__all__ = ["check_position_caps"]
