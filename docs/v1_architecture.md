# AutoBot-OG — v1 Architecture

**Status:** v1 specification (locked design). Implementation in progress.
**Scope:** GBPUSD only. Three strategies routed by a regime classifier.
**Audience:** future maintainers, reviewers, and Claude Code sessions that
need an authoritative reference for how the system is meant to behave.

This document is the foundational spec. Code may evolve, but the rules,
thresholds, and decisions captured here are the contract. When code and
this document disagree, the document is the source of truth until it is
explicitly amended.

---

## Section 1 — Overview

### 1.1 Project

AutoBot-OG is a systematic intraday trading bot for spot FX. The v1
deployment trades a single instrument (GBPUSD) on the IG broker API and
makes its own entry, management, and exit decisions without manual
intervention during a session.

### 1.2 Operational principle

The bot **reads what the market is doing**, then chooses a strategy that
is mechanically appropriate for that context. It does not try to predict
what the market will do next.

That principle expresses itself in three concrete commitments:

1. **Regime first, signal second.** No trade is ever taken without an
   active, confirmed regime. The regime determines which strategies are
   even eligible. A strategy that disagrees with the regime never fires.
2. **Confirm before acting.** Every entry requires three things in
   order: a *context* that says the setup is plausible, a *trigger* that
   shows the move actually happened, and a *confirmation* candle that
   keeps the move going. A single bar is never enough.
3. **Invalidation must be cheap and obvious.** Every entry has a
   pre-defined invalidation point (the stop). Stops are placed where
   the setup is *wrong*, not at fixed distances. If the invalidation is
   not visible on the chart, the trade is skipped.

### 1.3 Architecture summary

```
                +-----------------+
       H1 +---->| Regime Engine   |---- bullish TREND  --> EMA Continuation
       M5 +---->|  (classifier +  |---- bearish TREND  --> EMA Continuation
                |   state + gate) |---- RANGE          --> Bollinger Reclaim
                +--------+--------+---- VOLATILE       --> Liquidity Sweep
                         |
                         v
                +-----------------+         +-----------------+
                | Strategy        | trades  | Risk / Mgmt     |
                | (one per regime)|-------->| (SL, BE, trail) |
                +-----------------+         +-----------------+
                                                    |
                                                    v
                                            +---------------+
                                            | Execution (IG)|
                                            +---------------+
```

The classifier produces a regime label. The label routes to **exactly
one** strategy. The strategy may or may not find a setup. If it does,
the risk layer sizes the trade and the execution layer places it.

Crucially: **the classifier never routes to the opposite strategy**.
A `bullish TREND` regime never triggers a short-side mean reversion
even if Bollinger Bands look extended. If the regime says "trend up",
the only eligible strategy is "buy the pullback". This asymmetry is
deliberate and is the system's main protection against giving back
trend profits to mean-reversion logic.

### 1.4 What is in scope for v1

- GBPUSD single-instrument trading on the IG API.
- H1 regime classification, M5 execution.
- Three strategies: EMA Continuation, Bollinger Reclaim, Liquidity Sweep.
- Universal risk rules (BE-at-1R, structure trail, news blackout,
  spread filter, concurrent-position caps).

### 1.5 What is explicitly out of scope for v1

- Multi-pair routing. The regime engine is *pair-aware* in design but
  only GBPUSD is configured and validated.
- Breakout-hold strategy. The fourth canonical entry style is dropped
  for v1 (see [Section 7.3](#73-drop-breakout-hold-for-v1)).
- Partial exits and scaling out. Single position, single exit.
- ZigZag / pivot-based structure. 5-bar fractal only.
- Discretionary overrides. The bot is fully systematic.

---

## Section 2 — The Framework

Every quality entry across every strategy is built on the same three
ingredients. This is the framework that the individual strategies
specialise.

### 2.1 The three ingredients

#### Context

The bigger-picture conditions that make the trade plausible. Without
context, the entry is just a pattern in isolation and has no edge.
Context is supplied by the **regime classifier** (Section 4): a
bullish TREND regime is the context for a long pullback entry; a
RANGE regime is the context for a Bollinger reclaim.

If the regime is wrong, the strategy is wrong, even if the candle
pattern is textbook.

#### Confirmation

The signal that the move has *already started*. A pullback is not
enough — we need the bar that resumes the trend. A pierce of the
Bollinger Band is not enough — we need the reclaim. A sweep of
liquidity is not enough — we need the reclaim close *above* the
swept level.

Confirmation is what distinguishes a trade from a forecast.

#### Invalidation

The exact price at which the setup is wrong. Not a fixed pip stop —
a structural level beyond which the thesis no longer holds. The stop
is placed at that level (with an ATR-derived buffer). If the
invalidation point is not clearly visible on the chart, the trade
does not qualify.

### 2.2 Strong signs and bad signs

Every setup also has qualitative markers. **Strong signs** raise
confidence (and in practice, allow slightly larger size within the
risk model). **Bad signs** disqualify the entry even if the
mechanical pattern is complete. The strategies in Section 5 enumerate
these per pattern.

### 2.3 Canonical entry styles

There are four canonical entry archetypes in the framework. **v1
implements only the first three.**

| # | Entry style              | Regime         | Status in v1   |
|---|--------------------------|----------------|----------------|
| 1 | Trend Continuation       | TREND          | Implemented    |
| 2 | Mean Reversion (BB)      | RANGE          | Implemented    |
| 3 | Liquidity Sweep Reversal | VOLATILE       | Implemented    |
| 4 | Breakout Hold            | (transitional) | Deferred to v2 |

#### 2.3.1 Trend Continuation Entry

**Why it works.** A market in a confirmed trend has more participants
willing to add to the move than fade it. Pullbacks against the trend
attract those participants near reference levels (moving averages,
recent swings). Buying a pullback into a known reference point with
confirmation of the resumption is high expectancy because the path of
least resistance is already established.

**Context.**
- H1 regime is TREND (bullish for longs, bearish for shorts).
- M5 EMA slope agrees with H1 direction.
- Price is in a pullback *toward* the EMA50, not extended away from it.

**Pullback.**
- A counter-trend retrace of at least 2–3 M5 bars.
- Pullback either touches the M5 EMA50 or closes one bar past it on
  the wrong side (for longs: closes below EMA50).
- Pullback does not break the most recent M5 swing low (for longs).

**Confirmation.**
- A *reclaim* candle: closes back on the correct side of EMA50.
- A *confirmation* candle that follows in the trend direction.
- Two-bar confirmation is required: the reclaim alone is not enough.

**Strong signs.**
- MACD histogram aligned with trend (positive for longs).
- Pullback completed without breaking a prior M5 swing.
- Volume / range expansion on the reclaim bar (qualitative).
- Pullback finds the EMA50 *and* aligns with a prior swing level.

**Bad signs (skip).**
- Pullback is deep and breaks prior swing structure.
- MACD histogram flipped against the trend during the pullback.
- Regime classifier confidence dropping during the pullback.
- Pullback is into a major higher-timeframe level the trend just
  cleared (likely retest failure).

#### 2.3.2 Mean Reversion (Bollinger Reclaim)

**Why it works.** In a ranging market, price oscillates between supply
and demand zones. The Bollinger Bands at (20, 2σ) describe roughly
where price has been "extended" relative to its short-term mean. A
pierce of the band followed by a *reclaim back inside* is a
mechanical demonstration that the extension was rejected — supply or
demand stepped in. The mean (the BB midline) is the natural target.

**Context.**
- H1 regime is RANGE.
- Bollinger band width compressed (`bb_width_norm < 1.8`).
- Weak EMA slope (no strong trend pressure).

**Setup.**
- M5 candle closes *outside* the Bollinger Band (the pierce).
- The *next* M5 candle closes *back inside* the band (the reclaim).

**Confirmation.**
- A third M5 candle that closes in the direction of the reclaim
  (away from the pierced band).

**Strong signs.**
- Pierce occurs on an extreme wick rather than a sustained close.
- Reclaim candle is large-bodied (decisive).
- Pierce coincides with a prior swing low/high (double signal).
- BB width was compressed *before* the pierce (real range, not a
  trend pretending to consolidate).

**Bad signs (skip).**
- BB width is expanding rapidly (regime is shifting to VOLATILE).
- H1 EMA slope is non-trivial and against the reclaim direction.
- Pierce candle had unusually large body (momentum, not exhaustion).
- High-impact news is within the blackout window.

#### 2.3.3 Liquidity Sweep Reversal

**Why it works.** Markets tend to run obvious liquidity (recent swing
highs, prior session highs/lows, round numbers) before turning.
Stops cluster at those levels; a brief sweep takes that liquidity
and immediately reverses. The reclaim of the swept level is a
signal that the move past it was not driven by genuine continuation
flow but by liquidity-seeking algorithms.

**Context.**
- Volatile expansion regime, or a transition into VOLATILE.
- Price within close proximity of an identifiable liquidity level
  (recent swing high/low, prior day high/low, session high/low).
- Active session (London or NY); the Asia session is excluded.

**Sweep.**
- An M5 candle pushes *through* the liquidity level (wick or close).
- The same or next candle closes *back* on the original side.

**Confirmation.**
- A confirmation candle in the reversal direction following the
  reclaim. The reclaim alone is not the entry.

**Strong signs.**
- Sweep wick is significantly longer than the candle body.
- Multiple liquidity levels swept in one push (stop cascade).
- Reclaim closes above (for longs) the *original swing*, not just
  the wick of the sweep candle.
- Session timing aligns (London open / NY open is prime).

**Bad signs (skip).**
- Sweep candle closes well beyond the level (it was real breakout).
- Reclaim is weak / multiple bars / no decisive close.
- Sweep occurs into a higher-timeframe trend (low-probability fade).
- Outside London/NY active hours.

---

## Section 3 — Indicator Specifications

This section is the contract for what every indicator computes and
with what parameters. The Phase 1 implementation in `src/indicators/`
is the reference; this section governs that implementation.

### 3.1 Timeframes

| Timeframe | Role                                                 |
|-----------|------------------------------------------------------|
| H1        | Regime classification (primary). Macro bias.         |
| M5        | Execution. Entry pattern detection. SL sizing.       |

H4 / D1 are read-only context inputs in v1 (not used for gating).
M1 / tick is out of scope.

### 3.2 EMA — Exponential Moving Average

| Parameter        | Value                                |
|------------------|--------------------------------------|
| Period           | 50                                   |
| `adjust`         | `False` (recursive seeded form)      |
| Seed             | SMA(50) of first 50 bars             |
| Applied to       | Close                                |
| Used on          | H1 (macro bias), M5 (execution)      |

**H1 EMA50** is the macro bias anchor used by the regime classifier.
**M5 EMA50** is the pullback anchor for trend continuation and the
reclaim reference for the entry pattern.

`adjust=False` is non-negotiable: it gives a single recursive series
that does not change when historical bars arrive. The seeded form
makes the early values deterministic across backtests.

#### 3.2.1 ATR-normalised slope

The raw EMA slope is meaningless across volatility regimes. We
normalise by ATR(14) so that "steep" means the same thing whether
the pair is in a quiet drift or a strong push.

```
slope_norm = (ema[t] - ema[t - 10]) / atr14[t]
```

10-bar lookback on the same timeframe. Output is dimensionless.

**Regime thresholds (H1):**

| Regime band   | Condition                          |
|---------------|------------------------------------|
| Strong TREND  | `|slope_norm| > 0.35`              |
| RANGE         | `-0.15 <= slope_norm <= 0.15`      |
| Intermediate  | `0.15 < |slope_norm| <= 0.35`      |

**Hysteresis.** A flat-to-trending transition requires `|slope_norm| > 0.35`.
A trending-to-flat transition requires `|slope_norm| < 0.15`. The
intermediate band between 0.15 and 0.35 holds the previous regime —
it does not flip until it crosses the *opposite* threshold. This is
the mechanism that prevents single-bar slope noise from flipping the
classifier.

### 3.3 Bollinger Bands

| Parameter      | Value                                          |
|----------------|------------------------------------------------|
| Period         | 20                                             |
| Std multiplier | 2.0                                            |
| Source         | Close                                          |
| Used on        | M5 (execution); H1 optional (regime context)   |

Output: `upper`, `middle` (the SMA20), `lower`.

#### 3.3.1 ATR-normalised width

Raw BB width varies with the absolute price level and the volatility
regime. We normalise by ATR(14) so width is comparable.

```
bb_width_norm = (upper - lower) / atr14
```

**Regime thresholds:**

| Regime band     | Condition                              |
|-----------------|----------------------------------------|
| RANGE (tight)   | `bb_width_norm < 1.8`                  |
| TREND (broad)   | `bb_width_norm > 2.5`                  |
| VOLATILE expand | rapid widening (> 20% bar-on-bar)      |

The volatile classification triggers on *rate of change* of width,
not absolute width. A market can be in a wide steady trend (broad
width, stable) or in a volatility blow-up (broad width, expanding
fast). Only the latter is VOLATILE.

### 3.4 MACD

| Parameter        | Value           |
|------------------|-----------------|
| Fast EMA         | 12              |
| Slow EMA         | 26              |
| Signal EMA       | 9               |
| Source           | Close           |
| Used on          | H1 only         |

MACD is **confirmation**, not classification. It contributes to the
regime label only as a tiebreaker — its primary use in v1 is:

- **Aligned with EMA slope direction**: high confidence.
- **Disagrees with EMA slope direction**: medium confidence.

It sits at the bottom of the classifier hierarchy (Section 4).

### 3.5 ATR — Average True Range

| Parameter | Value                                                  |
|-----------|--------------------------------------------------------|
| Period    | 14                                                     |
| Method    | Wilder (recursive smoothing)                           |
| Used on   | H1 (normalisation), M5 (stop sizing)                   |

ATR is the engine for two unrelated jobs:

1. **Normalisation.** EMA slope and BB width are both divided by ATR
   so their thresholds are scale-invariant across volatility regimes.
2. **Stop sizing.** M5 ATR is the basis for `0.8 × ATR_M5`,
   `1.2 × ATR_M5`, etc. used in stop placement (Section 5).

### 3.6 Structure — 5-bar fractal

Implementation in `src/structure/fractals.py` (Phase 2).

**Swing high definition.** A bar is a swing high if its high is
strictly greater than the highs of the two bars immediately before
*and* the two bars immediately after.

```
bar[t].high > bar[t-2].high
bar[t].high > bar[t-1].high
bar[t].high > bar[t+1].high
bar[t].high > bar[t+2].high
```

Swing lows are the analogous condition on lows.

**Confirmation lag.** A swing point is not knowable in real time —
the two trailing bars are required. Every swing is therefore
confirmed with a **2-bar lag**. Strategy code must treat the most
recent confirmed swing as the latest reference; the bar 1–2 ago is
not yet structure.

**Structure classification:**

| Pattern               | Label    | Interpretation             |
|-----------------------|----------|----------------------------|
| HH (higher high) + HL | Bullish  | Trend up                   |
| LH (lower high)  + LL | Bearish  | Trend down                 |
| HH + LL               | Volatile | No clean read (broadening) |
| LH + HL               | Volatile | No clean read (narrowing)  |

Structure is the *highest priority* input to the regime classifier
(Section 4.1). When structure disagrees with EMA slope, structure wins.

#### 3.6.1 Why 5-bar fractal (not ZigZag)

- Deterministic. No repaint, no parameter that has to be tuned for
  amplitude.
- Two-bar lag is acceptable on H1 and M5 — both far short of the
  regime hysteresis window.
- Trivial to test (the test suite enumerates exact swing arrays).

ZigZag-style detectors are deferred to v3 if a higher-resolution
structure read proves necessary (see Section 8).

---

## Section 4 — Regime Classifier

The regime classifier is the gate. It outputs one of four labels:

- `TREND` (with direction `bullish` or `bearish`)
- `RANGE`
- `VOLATILE`
- `UNKNOWN` (warm-up / insufficient data)

Plus a `regime_live` boolean indicating whether the label is stable
enough to act on.

### 4.1 Hierarchy

When inputs disagree, higher priority wins ties. The order is:

| Priority | Input         | Why this priority                                   |
|----------|---------------|-----------------------------------------------------|
| 1 (top)  | Structure     | Slowest, hardest to fake. Trumps everything.        |
| 2        | EMA slope     | Smooth, reliable, normalised.                       |
| 3        | BB width      | Volatility envelope, not directional alone.         |
| 4        | MACD          | Confirmation, not classification.                   |

#### 4.1.1 Decision tree

```
1. If structure is Volatile (HH+LL or LH+HL):
      regime := VOLATILE
      direction := neutral

2. Else if structure is Bullish AND ema_slope_norm >= 0.15:
      regime := TREND
      direction := bullish

3. Else if structure is Bearish AND ema_slope_norm <= -0.15:
      regime := TREND
      direction := bearish

4. Else if |ema_slope_norm| <= 0.15 AND bb_width_norm < 1.8:
      regime := RANGE
      direction := neutral

5. Else if bb_width_norm rapid expansion (> 20% bar-on-bar):
      regime := VOLATILE
      direction := neutral

6. Else:
      regime := previous regime (sticky / hysteresis hold)
```

#### 4.1.2 MACD as confidence

MACD does not change the label. It modulates the confidence score
that the classifier exposes:

- TREND label + MACD aligned with direction → confidence: **high**
- TREND label + MACD disagrees                → confidence: **medium**
- RANGE / VOLATILE labels: MACD ignored

Strategies may consume confidence as an optional sizing modifier in
future versions; v1 reads it for logging only.

### 4.2 Stability — H1 confirmation + M5 agreement

The classifier output is not actionable until two stability gates are
cleared. Both are required for `regime_live = True`.

#### 4.2.1 H1 confirmation

A pending regime change must be the classifier's output on a
**closed H1 bar**. Mid-bar reads are advisory only. One H1 close in
the new regime is sufficient — v1 uses 1 H1 close (not 2) by design;
see [Section 7.1](#71-1-h1-close--3-m5-agreement-not-2-h1-closes).

#### 4.2.2 M5 agreement

After the H1 commits to a new regime, the M5 timeframe must agree
on the next **3 consecutive M5 closes** before `regime_live = True`.
This is the second stability gate.

The M5 agreement check (Section 4.3) is *looser* than the H1
classification — it asks "is M5 not contradicting H1?", not "does M5
independently classify this regime?".

#### 4.2.3 Timeline

```
H1 close at T0 with new regime label
    -> pending_regime set
    -> m5_confirmation_count = 0

Each subsequent M5 close:
    if M5 agrees with pending_regime:
        m5_confirmation_count += 1
    else:
        m5_confirmation_count = 0     # must be CONSECUTIVE
        pending_regime cleared if M5 disagrees decisively

When m5_confirmation_count == 3:
    current_regime = pending_regime
    regime_live = True
    last_regime_change_time = now
```

Worst case to commit a new regime: 1 H1 close + 3 M5 closes =
~75 minutes. Typical case (mid-H1 transition): closer to ~25 minutes
from the underlying structural change. This is the lag the design
explicitly trades for stability.

### 4.3 M5 validation (looser than H1)

The M5 gate intentionally does not duplicate the H1 classifier. It
asks a simpler question: *is M5 doing anything that would contradict
the pending H1 regime?*

| Pending H1 regime    | M5 agreement condition                              |
|----------------------|-----------------------------------------------------|
| Bullish TREND        | `m5.ema_slope_norm > 0` AND `m5.close > m5.ema_50`  |
| Bearish TREND        | `m5.ema_slope_norm < 0` AND `m5.close < m5.ema_50`  |
| RANGE                | `m5.close` inside BB AND `m5.bb_width_norm < 2.5`   |
| VOLATILE             | (no M5 gate — instability is the point)             |

For VOLATILE, by definition we expect M5 to be erratic. Requiring
M5 to "agree" with a VOLATILE regime would simply re-classify it,
which is the wrong job for this layer.

### 4.4 State

The regime engine holds the following in-memory state per pair:

| Field                     | Type           | Notes                              |
|---------------------------|----------------|------------------------------------|
| `current_regime`          | enum           | Last committed regime              |
| `current_direction`       | enum           | bullish / bearish / neutral        |
| `current_confidence`      | enum           | high / medium / low                |
| `pending_regime`          | enum or None   | Awaiting M5 agreement              |
| `pending_direction`       | enum or None   |                                    |
| `m5_confirmation_count`   | int 0..3       | Reset on disagreement              |
| `last_regime_change_time` | timestamp      | For instability circuit breaker    |
| `regime_live`             | bool           | True only when both gates passed   |
| `reason`                  | string         | Human-readable decision rationale  |
| `debug`                   | dict           | Raw indicator values used          |

State is in-memory only in v1. It is rebuilt from the last N hours of
bar data on cold start (N ≥ 200 H1 bars to fully seed ATR/EMA/BB).
Persistence to disk is deferred to v2.

### 4.5 What the classifier does not do

- It does not place trades.
- It does not choose between two eligible strategies — there is
  exactly one strategy per regime in v1.
- It does not consider news, spread, or position state. Those are the
  risk layer's responsibility (Section 6).
- It does not look at price action beyond its declared inputs
  (EMA, BB, ATR, MACD, structure). No candle pattern matching.

---

## Section 5 — Strategies

Three strategies. Exactly one is eligible at a time, determined by
the active regime. Each strategy is responsible for pattern detection,
stop placement, and target / management rules within its regime.

### 5.1 RANGE → Bollinger Reclaim (Mean Reversion)

**Regime gate.**
- `current_regime = RANGE` AND `regime_live = True`.
- `bb_width_norm < 1.8` (compressed).
- `|ema_slope_norm| <= 0.15` on H1 (weak slope confirmed).

**Entry pattern (LONG; SHORT is symmetric).**

Three M5 candles in sequence:

1. **Pierce candle.** Closes *outside* the lower Bollinger Band.
2. **Rejection candle.** Closes *back inside* the band. The wick on
   the wrong side does not matter — close is what counts.
3. **Confirmation candle.** Closes *higher* than the rejection
   candle close.

Entry order is placed on the close of the confirmation candle, at
market or limit-on-touch of the next bar open (broker-dependent).

**Stop loss.**
```
SL_distance = max(12 pips, 0.8 × ATR_M5)
SL_price    = pierce_wick_low - SL_distance       (LONG)
```

The stop is anchored to the **pierce wick extreme** (the actual
invalidation), not to the entry price. If price returns to the
pierce extreme, the rejection thesis is broken.

**Take profit.**

| Tier      | Target                                  |
|-----------|-----------------------------------------|
| Primary   | BB midline (the SMA20)                  |
| Secondary | Opposite Bollinger Band                 |

v1 uses **primary only** as a fixed limit. The secondary target is
documented for v2 when partial exits become available.

**Management.**
- At `+1R`: move SL to **breakeven** (entry price; no buffer in v1).
- Trail: behind M5 EMA20 *or* most recent confirmed M5 swing low
  (whichever is closer to current price for a LONG).
- Exit: limit fill at BB midline, or trail-out, whichever comes first.

**Time exit.** Closed at NY close regardless of P&L. RANGE setups do
not survive overnight; see [Section 6.5](#65-end-of-day-behaviour).

### 5.2 TREND → EMA Continuation

**Regime gate.**
- `current_regime = TREND` AND `regime_live = True`.
- Structure aligned with direction (bullish structure for longs,
  bearish for shorts).
- `|ema_slope_norm| > 0.35` on H1.

**Entry pattern (LONG; SHORT is symmetric).**

1. **Pullback.** At least 2–3 M5 bars of counter-trend retrace. The
   pullback must reach the M5 EMA50 or close one bar slightly past it.
2. **Reclaim candle.** A bar that closes back above the M5 EMA50.
3. **Confirmation candle.** A bar closing higher than the reclaim
   close — and a bullish-bodied bar.

**Optional confluence.** MACD histogram positive on H1 (aligned with
trend) gives confidence: **high**. Trade still qualifies without it
at confidence: **medium**.

**Stop loss.**
```
SL_distance = max(12 pips, 1.2 × ATR_M5)
SL_price    = pullback_low - SL_distance           (LONG)
```

The stop is anchored to the pullback low. If price breaks below the
pullback low, the continuation thesis is wrong: the pullback became
a reversal.

**Take profit.**

There is **no fixed TP** for trend continuation. The strategy aims to
capture the runner.

**Management.**
- At `+1R`: move SL to **breakeven**.
- Trail: behind most recent confirmed M5 swing low *or* M5 EMA20,
  whichever is more conservative.
- Exit: stop-out at the trail.

**Time exit.** Can hold overnight Mon–Thu if (a) currently in profit
and (b) H1 regime is still TREND in the same direction. Force-closed
at NY close on Fridays. See Section 6.5.

### 5.3 VOLATILE → Liquidity Sweep

**Regime gate.**
- `current_regime = VOLATILE` AND `regime_live = True`.
- An identifiable liquidity level within proximity of current price.
  In v1, this means one of:
  - Most recent confirmed M5 swing high / swing low.
  - Prior session (London / NY / Asia) high or low.
- Active session: **London or NY only**. Asia rejected.

**Entry pattern (LONG; SHORT is symmetric).**

A LONG sweep entry fades a sweep of a liquidity *low*:

1. **Sweep candle.** M5 candle wick or close *below* the liquidity
   level.
2. **Reclaim candle.** Same candle or next candle closes *above* the
   level. A wick-only sweep that closes back above on the same bar is
   the strongest variant.
3. **Confirmation candle.** Next M5 bar closes higher than the reclaim
   close.

**Stop loss.**
```
SL_distance = max(12 pips, 1.0 × ATR_M5)
SL_price    = sweep_wick_low - SL_distance         (LONG)
```

The stop is anchored to the sweep extreme. If price returns there,
either the sweep was real continuation or another sweep is in
progress — either way, the entry is invalidated.

**Take profit.**

Hybrid in design; v1 simplifies to one position with a structure trail.

| Tier             | Target                                       |
|------------------|----------------------------------------------|
| (Future) Scale 1 | `+1.5R` partial — deferred to v2             |
| v1 single        | Trail by structure (swing points)            |

**Management.**
- At `+1R`: move SL to **breakeven**.
- Trail: behind most recent confirmed M5 swing low.
- Exit: stop-out at the trail.

**Time exit.** Closed at NY close regardless of P&L. Sweep setups do
not survive overnight.

### 5.4 Strategy summary

| Field              | Bollinger Reclaim    | EMA Continuation       | Liquidity Sweep        |
|--------------------|----------------------|------------------------|------------------------|
| Regime             | RANGE                | TREND                  | VOLATILE               |
| Confirmation bars  | 3 (pierce/reclaim/c) | 3 (pullback/reclaim/c) | 3 (sweep/reclaim/c)    |
| Stop anchor        | Pierce wick          | Pullback low/high      | Sweep wick             |
| ATR multiplier     | 0.8                  | 1.2                    | 1.0                    |
| Min SL (pips)      | 12                   | 12                     | 12                     |
| TP type            | Fixed (BB midline)   | Trail only             | Trail only (v1)        |
| BE at              | +1R                  | +1R                    | +1R                    |
| Hold overnight     | No                   | Yes (M–Th, in profit)  | No                     |

---

## Section 6 — Risk Layer & Trade Management

The risk layer is strategy-agnostic. Every entry passes through these
rules before reaching the broker, and every open position is managed
by the same machinery.

### 6.1 Initial stop loss by pair

Stops are always the **maximum** of a pip floor and an ATR-derived
distance. Volatility wins when it is higher than the floor; the floor
catches the case where ATR is unusually compressed and a textbook
ATR stop would be tighter than reasonable execution noise.

| Pair    | Pip floor (range)  | ATR multiplier (by strategy)              |
|---------|--------------------|-------------------------------------------|
| GBPUSD  | 15–20 pips         | 0.8 / 1.0 / 1.2 (range / sweep / trend)   |
| EURUSD  | 12–15 pips         | (out of scope for v1; v2)                 |

For v1, the pip floor is **15 pips** for GBPUSD; values above are the
band within which the floor can be adjusted in the config without
re-spec.

**Never tighten below current volatility.** A stop that is smaller
than `multiplier × ATR_M5` for the strategy is silently raised to
that ATR-derived minimum. The actual stop is
`max(pip_floor, multiplier × ATR_M5)` — the strategy specifies the
multiplier, the pair specifies the pip floor.

### 6.2 Breakeven

When unrealised profit reaches `+1R` (measured against the initial
stop distance), the stop is moved to the **entry price**.

- No buffer above entry (no "+2 pip lock-in"). The trade is now
  free-roll.
- Not at fixed pips. The legacy "BE at +5 pips" rule from earlier
  iterations is **explicitly retired**.
- Once at BE, the stop only moves further in the trade's favour
  (trail) — it never moves back.

### 6.3 Trailing

Trailing is structure-based, not pip-based.

| Strategy           | Primary trail            | Secondary trail   |
|--------------------|--------------------------|-------------------|
| Bollinger Reclaim  | M5 EMA20                 | M5 swing low/high |
| EMA Continuation   | M5 swing low/high        | M5 EMA20          |
| Liquidity Sweep    | M5 swing low/high        | M5 EMA20          |

For both primaries, the candidate trail level is computed every M5
bar close, and the stop is moved to the more conservative of the two
(closer to current price, but never away from current price).

**Fixed pip trails are not used in v1.** They produce worse trend
captures and have no structural justification.

### 6.4 Partial exits

**None in v1.** Every trade is one position with one exit.

Rationale: IG's API for partial closes is non-trivial and adds
execution risk (partial fills, leftover positions, mis-sized stops on
the remainder). The v2 design will revisit partials at `+1.5R` /
`+2R` for the sweep and range strategies. See Section 7.4.

### 6.5 End-of-day behaviour

EOD = New York close (17:00 ET / 22:00 UTC during US summer time).

| Regime / strategy   | EOD behaviour                                          |
|---------------------|--------------------------------------------------------|
| RANGE / Reclaim     | Always close at NY close.                              |
| VOLATILE / Sweep    | Always close at NY close.                              |
| TREND / Continuation| Hold overnight Mon–Thu **iff** (in profit) AND        |
|                     | (H1 regime still TREND, same direction). Force-close   |
|                     | Fridays at NY close.                                   |

The asymmetry is deliberate: range and sweep have no thesis that
survives a session gap; trend has a thesis that explicitly does
(positions stay in line with the macro bias).

### 6.6 News blackout

A hard window applied to known event timestamps from the configured
economic calendar.

| Severity        | Blackout                                                  |
|-----------------|-----------------------------------------------------------|
| High-impact     | **±15 min** around the event. No new entries, do not move |
|                 | stops mid-window. (Existing trade exits still trigger.)   |
| Medium-impact   | Soft block: skip new entries; existing trades unchanged.  |
| Low-impact      | Ignored.                                                  |

High-impact events for GBPUSD include (non-exhaustive):
- UK and US CPI, PPI, retail sales
- US NFP / employment situation
- Bank of England and FOMC rate decisions and minutes
- UK and US GDP releases
- Surprise central bank communications

### 6.7 Spread filter

Reject new entries when the current spread is too wide relative to
either an absolute or volatility-relative threshold:

```
spread_cap = min(3 pips, 0.3 × ATR_M5)
if current_spread > spread_cap:
    reject entry
```

The volatility-relative term protects against entries during
genuinely thin liquidity moments; the absolute pip term catches
extreme spread widening on otherwise calm bars.

### 6.8 Concurrent position caps

| Cap                              | Value | Notes                              |
|----------------------------------|-------|------------------------------------|
| Max global open positions        | 2     | Across all pairs.                  |
| Max per pair                     | 1     | No stacking on the same instrument.|
| Regime stacking                  | mixed | One TREND + one RANGE OK. Two of   |
|                                  |       | the same regime is NOT allowed.    |

For v1 (GBPUSD only), the global cap and the per-pair cap are
effectively equal — at most 1 open position at a time. The
multi-pair semantics are documented for the v2 multi-pair rollout.

### 6.9 Circuit breakers

Three independent circuit breakers, each halts new entries (already-open
positions continue to be managed normally).

#### 6.9.1 Daily drawdown stop

- Track cumulative realised + unrealised R-multiple for the trading
  day (NY-close to NY-close).
- If cumulative reaches **−3R**, no new entries for the remainder of
  the day. Resets at NY close.

#### 6.9.2 Consecutive loss pause

- After **4 consecutive losing trades** (any strategy, any pair),
  pause new entries for **4 hours**.
- Counter resets on the next winning trade.

#### 6.9.3 Regime instability pause

- If the regime classifier flips between regimes **more than 3 times
  within 1 rolling hour**, pause new entries for the affected pair
  for **1 hour**, or until a single regime holds `regime_live = True`
  for a full H1 close — whichever is longer.
- The instability counter is per-pair and decays linearly over the
  rolling window.

### 6.10 Phase 4 module structure

The risk layer ships as `src/risk/` with `RiskGuard` as the single
public orchestrator. Two methods drive everything: `allow_entry` gates
prospective trades through a five-rule pipeline; `positions_to_force_close`
emits EOD close orders.

```
src/risk/
├── guard.py                     # RiskGuard class (orchestrator)
├── types.py                     # OpenPosition, AccountState, CandidateTrade,
│                                # MarketSnapshot, RuleResult, RiskDecision,
│                                # ForceCloseOrder
├── constants.py                 # env-overridable tunables
├── news_calendar/               # Phase 4-A: Finnhub-backed calendar
├── rules/
│   ├── spread_filter.py         # §6.7
│   ├── position_caps.py         # §6.8
│   ├── news_blackout.py         # §6.6
│   ├── circuit_breakers.py      # §6.9 (DD + loss streak + instability)
│   └── eod_enforcement.py       # §6.5 (pre-EOD suppression + close orders)
└── state/
    └── circuit_breaker_state.py # JSON-persisted state for §6.9
```

#### Pipeline order in `allow_entry`

```
circuit_breakers → position_caps → news_blackout → spread_filter
                                                  → pre_eod_suppression
```

Each rule returns a `RuleResult{allow, rule, reason}`; the first
rejection short-circuits. Ordering reasoning: cheapest-state-only
first, live-market last. Circuit breakers gate on persisted state +
emission queries (highest information density per check); position
caps are an O(small n) capacity check; news blackout is a calendar
lookup; spread filter is the only rule touching live spread; pre-EOD
suppression is a time-of-day check whose result depends on candidate
regime and weekday.

#### Spec-ambiguity resolutions

The locked Phase 4 decisions, captured here so future revisions can
re-litigate them deliberately:

- **"Profitable" for TREND overnight hold (§6.5)** — `current_pnl_r >= 1.0`
  (i.e. position has reached +1R). Cleaner than depending on BE-amend
  state which `OpenPosition` may not carry.
- **Regime-instability counts (§6.9.3)** — trigger when EITHER
  `commits > REGIME_INSTABILITY_COMMITS (=3)` OR
  `m5_resets > REGIME_INSTABILITY_M5_RESETS (=5)` in the rolling
  `REGIME_INSTABILITY_WINDOW_MIN (=60)` minutes.
- **What counts as a "transition"** — commit events only
  (`current_regime` changes during the bar). Cancelled pendings do not
  count.
- **What counts as an "M5 reset"** — `m5_confirmation_count` transitions
  from `>0` to `0` **without a commit** (a disagreeing M5 closed
  against an in-flight pending). A successful third-M5 promotion also
  drops the counter to 0, but is recorded as `committed=True,
  was_m5_reset=False`. The risk layer's instability counter must count
  legitimate commits and actual resets in separate buckets (review
  C1, 2026-05-14).
- **Pause duration (§6.9.3)** — `max(1h, time-until-next-regime-live-H1-close)`.
  Implemented as a primary 1-hour cooldown with an extension check on
  every subsequent `allow_entry` that consults
  `RegimeEngine.regime_live_at_last_h1_close()`. The helper returns
  True only when (a) `is_live()` was True at the last H1 close *and*
  (b) the committed regime was non-VOLATILE — VOLATILE is "live" for
  sweep strategies but is by definition the unstable state, so it
  must not clear the instability cooldown (review H1, 2026-05-14).
  The snapshot is updated only inside `process_h1_close` (not on
  M5-driven commits), matching the spec's "full H1 close" wording
  (review H2, 2026-05-14) — accepted as an up-to-one-H1-window
  opportunity cost.
- **Pause scope** — per-pair. v1 single-pair collapses this to global,
  but the state model carries `regime_instability_pair`.
- **EOD time (§6.5)** — DST-aware. NY close at 17:00 in
  `RISK_NY_TZ (=America/New_York)`. Converted to UTC at decision time
  via `zoneinfo`: 21:00 UTC under EDT, 22:00 UTC under EST.
- **News blackout (§6.6) at the `allow_entry` boundary** — HIGH and
  MEDIUM impact events both reject new entries equally. The HIGH-only
  prohibition on stop modifications lives in Phase 5's execution layer.
- **Pre-EOD suppression** — 30 min before NY close, suppress new
  entries for RANGE/VOLATILE always; suppress TREND only on Fridays
  (TREND can hold overnight Mon-Thu). A TREND entered inside the
  Mon-Thu buffer is allowed by the gate but has no realistic path to
  +1R before NY close, so `apply_eod_force_close` will close it with
  a `trend_below_overnight_R` reason that includes the inside-buffer
  diagnostic note (review H4, 2026-05-14).
- **TREND overnight hold gates** — a TREND survives NY close iff
  (Mon-Thu) AND (`current_pnl_r >= 1.0`) AND (engine's committed
  regime still TREND, aligned direction) AND (engine has no
  contradicting pending transition staged). The pending check
  (review H3, 2026-05-14) accepts `pending_regime in {None, TREND}`
  where pending TREND must match the position's entry direction;
  RANGE / VOLATILE / opposite-direction-TREND pendings all
  force-close. Without this, a TREND that committed cleanly at H1
  but is *already* losing the regime via an in-flight RANGE pending
  would ride a no-longer-aligned bias overnight.
- **SL sizing** — out of scope for Phase 4. The execution layer
  (Phase 5) computes `SL = max(MIN_SL_PIPS, multiplier × ATR_M5)` per
  §6.1 and feeds the resulting stop into broker placement.

#### Env-var overrides

All thresholds in `risk/constants.py` are read once at import time from
environment variables matching the constant name (e.g.
`RISK_DAILY_DD_LIMIT_R`, `RISK_SPREAD_ABS_CAP_PIPS`). The pattern
mirrors `config/pair_config.py::MIN_SL_PIPS`. Defaults match this
section's locked spec. No YAML config in v1; v2 may add one once an
ops layer exists.

#### Persisted state

`CircuitBreakerState` lives at `data/risk/circuit_breaker_state.json`
(the `data/` tree is gitignored). The state object is loaded once at
construction and persisted lazily — only when a rule marks it dirty.
Corrupt JSON or unparseable datetime fields degrade to a fresh state
(fail-open is the v1-spec-aligned default; the next trade outcome
will repopulate the cooldown machinery).

#### Engine integration

The risk layer reads from `RegimeEngine` via three public methods:

- `current_regime` and `current_direction` (attributes) for the EOD
  "still TREND, same direction" check.
- `get_recent_emissions(window_minutes, now_utc)` for instability
  counting.
- `regime_live_at_last_h1_close()` for the cooldown extension.

The engine appends a `RegimeEmission` snapshot at the end of every
`process_h1_close` / `process_m5_close` (bounded deque, `maxlen=2000`).
The snapshot carries enough to drive both the commits-in-window and
M5-resets-in-window scans without re-walking history.

---

## Section 7 — Architectural Decisions

These are the load-bearing decisions made during v1 design. Each one
has alternatives that were considered and rejected; this section
records *why* the choice was made so future revisions can revisit
the assumptions instead of the conclusions.

### 7.1 1 H1 close + 3 M5 agreement (not 2 H1 closes)

**Decision.** Confirm a new regime with 1 H1 close, then require 3
consecutive M5 closes that agree with the H1 read before
`regime_live = True`.

**Alternatives considered.**
- 2 H1 closes (no M5 gate). Reliable but slow: worst case ~2 hours
  to commit. By the time a new TREND is confirmed, the first leg of
  the move is already over.
- 1 H1 close, no M5 gate. Too noisy; one outlier H1 bar can flip.

**Why this choice.** Lag is the enemy of trend capture and the enemy
of range entries (the band may already have travelled the midline by
the time we commit). 1 H1 close gives us the macro decision; 3 M5
closes give us the second-opinion confirmation without waiting
another full H1. Lag drops from ~120 minutes to ~25 minutes in the
typical case while keeping the false-flip rate roughly at parity.

### 7.2 5-bar fractal for structure (not ZigZag)

**Decision.** Use the 5-bar fractal definition (Section 3.6).

**Alternatives considered.**
- ZigZag with amplitude threshold (in pips or ATR). Higher resolution
  but introduces a tunable parameter that has to be re-fit per pair
  and per regime.
- N-bar swing with N > 5. Slower; reduces the number of usable swing
  points for trailing.
- Tick-level pivots. Out of scope (no tick data pipeline in v1).

**Why this choice.** Determinism over richness. The fractal definition
is unambiguous, the 2-bar confirmation lag is acceptable given the
classifier's much larger hysteresis window, and the test suite can
enumerate every case. ZigZag re-fitting would be a continuous
maintenance burden. Revisit if v3 needs higher resolution.

### 7.3 Drop breakout-hold for v1

**Decision.** Implement only 3 of the 4 canonical entry styles. The
breakout-hold pattern is deferred.

**Alternatives considered.**
- Implement breakout-hold gated to a "transitional" regime between
  RANGE and TREND. Mechanically the least stable of the four —
  breakouts have a high false-positive rate without a clear
  confirmation candle definition that doesn't overlap continuation.

**Why this choice.** A real breakout-and-hold either becomes a TREND
(captured by EMA Continuation on the first pullback) or fails into a
sweep (captured by Liquidity Sweep). The breakout itself, traded
without confirmation of one of those two outcomes, has too many
false positives. v1 covers the post-breakout structure via the other
strategies; v2 can revisit if data shows missed opportunity.

### 7.4 Single-position, no partial exits

**Decision.** One position per trade, one exit. No scaling out.

**Alternatives considered.**
- Two-position model: half off at +1.5R, runner with structure trail.
  Mechanically appealing for the sweep strategy in particular.

**Why this choice.** IG's API for partial closes is brittle. Partial
fills, residual positions, stop-resize-after-partial — all are
nontrivial to make safe under all latency / disconnect scenarios. v1
elects to ship a smaller surface area correctly rather than a larger
one with edge-case execution risk. Partials return in v2 with
dedicated reconciliation logic.

### 7.5 Strategy-specific EOD behaviour

**Decision.** Range and sweep strategies always close at NY close.
Trend strategy can hold overnight Mon–Thu if in profit and the H1
regime is still TREND.

**Alternatives considered.**
- Flat by NY close for all strategies (simpler, gives up trend runs).
- Hold all strategies overnight (gives back gap risk on range/sweep
  setups whose thesis ended at the session close).

**Why this choice.** The asymmetry tracks the underlying thesis.
Range and sweep entries are explicit short-term structures that have
no edge across a session boundary. Trend entries are riding macro
bias that, by construction, persists across sessions. Preserving
that asymmetry is what gives the trend strategy its outsized
contribution to expectancy in backtests.

### 7.6 GBPUSD-only v1

**Decision.** Validate the full stack on GBPUSD first. Do not deploy
to multiple pairs.

**Alternatives considered.**
- Multi-pair (EURUSD + GBPUSD) from v1. Doubles surface area; the
  pip floors and ATR profiles differ enough that per-pair tuning
  would be required at the same time as the engine is being
  stabilised.

**Why this choice.** GBPUSD is the most expressive of the major
pairs for this framework — it ranges, trends, and sweeps in clear
sessions, and the volatility regime is high enough that the
ATR-based thresholds don't approach the pip floor. Validate on the
hardest single pair first; generalise once the rules hold.

### 7.7 News blackout mandatory

**Decision.** ±15 min around high-impact events; no new entries,
positions in place are held with their existing stops (not adjusted
mid-window). Medium-impact events are a soft block.

**Alternatives considered.**
- No blackout — let the bot decide via spread filter. Spreads do
  widen but not always in time, and slippage on news prints is
  bimodal.
- Wider blackout (±30 min). Costs too many post-news continuation
  entries that work well.

**Why this choice.** The 15-minute window is the empirical sweet
spot for FX majors — long enough to cover the immediate price spike
and re-quote, short enough to re-enable post-event trades when
volatility returns to normal.

### 7.8 Spread filter mandatory

**Decision.** Reject entries if current spread is greater than
`min(3 pips, 0.3 × ATR_M5)`.

**Alternatives considered.**
- Volatility-relative only (`0.3 × ATR_M5`). Fails on quiet bars
  where 0.3 × ATR is unrealistically small.
- Absolute only (e.g. `< 3 pips`). Permits entries when spread is
  3p but ATR is 4p — that's effectively a 25%-of-volatility entry
  cost.

**Why this choice.** Two failure modes, two filters. `min(...)` is
the conservative combination — the entry has to clear both.

### 7.9 Concurrent position caps mandatory

**Decision.** 2 global, 1 per pair, no same-regime stacking.

**Alternatives considered.**
- Unlimited concurrent positions. Risk-of-ruin scales nonlinearly
  with correlated exposure.
- 1 global. Costs the regime-mix opportunity (a TREND on GBPUSD plus
  a RANGE on EURUSD in v2 would be two independent edges).

**Why this choice.** The caps balance the regime-diversification
benefit (good edges across regimes are uncorrelated) against the
correlated-loss risk (two trend trades in the same direction on
similar pairs are not independent). 2 global / 1 per pair / no
same-regime is the smallest cap structure that allows the v2
multi-pair use case while disallowing the obvious failure modes.

### 7.10 Daily DD, consecutive-loss, and instability circuit breakers

**Decision.** All three are mandatory in v1 (Section 6.9).

**Alternatives considered.**
- DD breaker only. Doesn't catch the "death by 1000 small losses"
  pattern in a volatile, unstable regime where the system keeps
  finding (false) entries.
- Manual circuit breakers (alert on threshold, operator decides).
  Defeats the purpose of unattended operation.

**Why this choice.** Each breaker addresses a different failure
mode: drawdown (one bad day in real R), behavioural pattern
(consecutive losses suggest regime mis-classification), and the
engine's own confidence (rapid flipping suggests the inputs are
contradictory). Together they form a layered safety net.

---

## Section 8 — Future Considerations (post-v1)

The following are explicitly out of scope for v1 but are anticipated
upgrade paths. Listed roughly in expected order of consideration.

### 8.1 EURUSD pair addition (v2)

- Per-pair pip floor (12–15 pips) and ATR profile config.
- Activate the existing multi-pair semantics in the risk layer
  (concurrent caps, no-stacking rule).
- Re-validate regime thresholds — EURUSD's lower realised volatility
  may require recalibrated `slope_norm` thresholds.

### 8.2 Breakout-hold strategy (v2 or v3)

- Add the fourth canonical entry style.
- Likely gated on a new "transitional" sub-regime between RANGE and
  TREND, rather than treated as a fifth top-level regime.
- Requires definition of breakout level (range high/low? prior swing?
  prior session?) and confirmation rules that distinguish from
  continuation.

### 8.3 Partial exits (v2)

- First scale at `+1.5R` for sweep and range strategies (where a
  natural mid-target exists — opposite BB / midline / first liquidity
  level).
- Runner remains under existing structure trail.
- Requires hardened reconciliation logic against IG's partial-close
  API (residual position detection, stop-resize, fill verification).

### 8.4 Cascade-style classifier extension (v3 if needed)

- Replace the flat priority hierarchy with a cascade: structure
  determines an outer regime "shell", then sub-classifiers refine
  within (e.g. structure=Bullish + slope mid-band = "trend pause"
  sub-regime).
- Only worth doing if v1 logs show the current 4-label set is
  conflating distinct opportunities.

### 8.5 ZigZag / pivot structure detector (v3 if fractal proves limited)

- Add an amplitude-aware pivot detector alongside the fractal.
- Use case: trailing stops that respect larger swings instead of
  every 5-bar wiggle.
- Trigger: backtest evidence that the fractal trail is exiting trend
  positions prematurely.

### 8.6 Other deferred items (no fixed version)

- Persistent regime state across restarts (currently in-memory).
- Auto-tuned thresholds (per pair, per session) instead of hand-set
  constants.
- Tick-level execution layer (currently bar-close only).
- Discretionary override hooks for the operator.
- Live PnL / position dashboard.

---

## Appendix A — Quick Reference: Thresholds

```
EMA slope norm (H1):
    enter TREND : |slope_norm| > 0.35
    exit  TREND : |slope_norm| < 0.15
    RANGE band  : -0.15 <= slope_norm <= 0.15

BB width norm:
    RANGE      : bb_width_norm < 1.8
    TREND      : bb_width_norm > 2.5
    VOLATILE   : bar-on-bar widening > 20%

ATR multipliers (M5 stop distance):
    Range / Reclaim     : 0.8 × ATR_M5
    Sweep               : 1.0 × ATR_M5
    Trend / Continuation: 1.2 × ATR_M5

Pip floor (GBPUSD): 15 pips

Confirmation:
    Regime commit  : 1 H1 close + 3 consecutive M5 closes
    Entry pattern  : 3 candles (trigger / reclaim / confirmation)
    Swing point    : 5-bar fractal, 2-bar confirmation lag

Risk:
    BE move        : at +1R
    Spread cap     : min(3 pips, 0.3 × ATR_M5)
    News blackout  : ±15 min high-impact
    Daily DD stop  : -3R cumulative
    Loss streak    : 4 in a row → 4h pause
    Instability    : 3 regime flips in 1h → 1h pause (per pair)
    Concurrent     : 2 global, 1 per pair, no same-regime stack
```

## Appendix B — Module mapping

| Section ref         | Implementation module         | Phase   |
|---------------------|-------------------------------|---------|
| §3 (all indicators) | `src/indicators/`             | Phase 1 |
| §3.6 (structure)    | `src/structure/`              | Phase 2 |
| §4 (classifier)     | `src/regime/`                 | Phase 3 |
| §5 (strategies)     | `src/strategies/`             | Phase 4 |
| §6 (risk / mgmt)    | `src/risk/`, `src/execution/` | Phase 5 |
| Broker I/O          | `src/feed/`, `src/execution/` | Phase 5 |
| Run loop            | `src/bot/`                    | Phase 6 |
| Alerts / logging    | `src/alerts/`                 | Phase 6 |

Each `src/<module>/MODULE.md` is the authoritative description of
that module's boundaries and is treated as a sub-spec under this
document.
