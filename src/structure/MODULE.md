# structure

Price structure: fractal swing-point detection.

## Owns
- 5-bar fractal swing-high / swing-low detection
- Structural break / continuation classification (HH/HL/LH/LL)
- Most-recent swing reference points for trailing stops

## Does NOT own
- Indicator math (`indicators/`)
- Regime selection (`regime/`)
- Trade-level trailing logic — exposes swing points; trailing happens in `execution/` or strategy code.
