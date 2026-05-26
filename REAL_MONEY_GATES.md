# 12 Gates to Real Money

Before this bot — or any bot, or any strategy — touches real money, all 12 gates must pass. This document is a forcing function, not a victory lap. Most gates currently fail. That is correct: the point is to make it impossible to talk yourself into trading real money on hopes and vibes.

Update the status of each gate honestly. If a gate goes from PASS back to FAIL because something changed (new strategy, new instrument, new code path), update it. If you find yourself wanting to weaken a gate to make it pass, write down why in the gate's notes and treat the urge with suspicion.

Status legend: ✅ PASS · ⚠️ PARTIAL · ❌ FAIL · 🟦 N/A (only with written justification)

---

## Strategy validity

### Gate 1 — Realistic slippage modeling
The backtester uses per-instrument actual broker spreads, sampled across multiple sessions (London open, NY open, off-peak, rollover). Not a flat 1-pip assumption. Slippage in volatile bars (top ATR decile) is modeled separately.

**Status: ⚠️ PARTIAL**
- ✅ Per-instrument spreads from a single OANDA snapshot are wired in (`backtest_rsi.INSTRUMENT_SPECS`, calibrated 2026-05-26).
- ❌ Spreads not yet sampled across sessions; current values are a single off-peak point.
- ❌ Volatility-regime spread widening not modeled.
- ❌ Entry execution lag (next-bar-open) not modeled.

### Gate 2 — Deflated Sharpe ≥ 0.95
The strategy's Deflated Sharpe Ratio (Bailey & López de Prado 2014), computed over a configurable parameter grid, exceeds 0.95. Naive PSR is not enough — DSR must account for all parameter combinations explored, formally or informally.

**Status: ❌ FAIL**
- RSI(2)+SMA(200) DSR on the three post-slippage survivors: EUR_USD 0.36, NAS100_USD 0.52, GBP_JPY 0.51. None clear 0.95. See `deflated_sharpe.py`.
- This is the gate that killed v1 of the strategy. Until a new candidate strategy passes it, this project does not advance.

### Gate 3 — Out-of-sample validation
Strategy parameters are frozen on data up to date X. Performance is then measured on data strictly after X, untouched during development. Profit factor degradation from in-sample to out-of-sample is < 30%.

**Status: ❌ FAIL**
- `validate.py` exists but its current call sites use whatever the latest tuned params are. No formal pre-registration of the parameter freeze date and the post-X data is required.
- A new strategy must pass this *before* DSR is computed on the post-X window.

### Gate 4 — Multi-regime survival
Strategy is profitable in at least two of: (a) high-vol crisis (e.g., COVID March 2020), (b) trending (e.g., 2022), (c) chop/range (e.g., much of 2024). Single-regime profitability does not count.

**Status: ❌ FAIL**
- Currently tested only on 2024–2026 H1 data. No regime decomposition.

### Gate 5 — Minimum trade count
Backtest has ≥ 200 completed trades per instrument over the test window. Below that, stats are not statistically meaningful regardless of PF.

**Status: ✅ PASS** (in raw count; doesn't help because Gate 2 fails)
- All instruments produced 280–550 trades over 2y H1.

---

## Execution & infrastructure

### Gate 6 — Tested loss limits and kill switch
Daily and weekly loss limits exist in code AND have been deliberately tripped at least once to verify they actually block entries. Kill switch has been flipped on and off with a real bar passing through both states.

**Status: ⚠️ PARTIAL**
- ✅ `risk_gate.py` implements daily (-2% NAV) and weekly (-5% NAV) limits + kill switch via `RISK_HALT.txt`.
- ✅ Unit-tested locally: kill switch path verified, NAV-relative limit math verified.
- ❌ Not yet tripped in a real running cron. Before real money: deliberately set a limit to a level the bot will breach within hours, confirm entries actually stop, then restore.

### Gate 7 — Restart safety
The bot can be killed at any moment, redeployed, and resume correctly. No local state file is needed for correctness; position state is reconstructed from the broker.

**Status: ✅ PASS**
- `tick_once.py` queries OANDA `OpenTrades` filtered by `clientExtensions.tag` to find its own positions. Each run is fully stateless w.r.t. positions.

### Gate 8 — Order rejection / partial fill handling
The bot has been deliberately given an order that will be rejected by the broker (insufficient margin, market closed, invalid price), and verified it logs cleanly, does not silently retry, does not create a phantom position in local state.

**Status: ❌ FAIL**
- Current code catches `V20Error` and logs, but no deliberate rejection test has been run end-to-end on the cloud bot.
- Also: market-closed path is handled (`market_is_open`), but margin-rejection and FOK-not-filled paths are not exercised.

### Gate 9 — Monitoring and alerting
If the bot fails for any reason — error, network issue, OANDA outage, GitHub Actions outage — you will know within 1 hour. You do not have to remember to check.

**Status: ❌ FAIL**
- Currently relies on manually opening the Actions UI. No push alert on workflow failure.
- Minimum acceptable: GitHub Actions failure notification → email or webhook → phone notification.

---

## Risk & operations

### Gate 10 — Position sizing as % of NAV
Risk per trade is computed as a percentage of current NAV at the moment of the trade. No fixed unit counts, no constants that drift as the account grows or shrinks.

**Status: ✅ PASS**
- `tick_once.py` line ~234: `risk_dollars = nav * RISK_PER_TRADE_PCT`, then units derived from ATR-based stop distance.

### Gate 11 — Correlation cap
The bot will not open multiple positions in the same direction across highly correlated instruments (e.g., XAU + XAG both long = doubled metals exposure). A correlation group cap limits total simultaneous exposure within a group.

**Status: ❌ FAIL**
- Not implemented. Was never needed during the strategy-validity phase, but is mandatory before real money.
- Suggested groups: {XAU, XAG} metals · {NAS100, SPX500, US30} US equity · {EUR_USD, GBP_USD, AUD_USD} USD-shorts · {USD_JPY, EUR_JPY, GBP_JPY} JPY-shorts.

### Gate 12 — 6 months of paper trading on the exact deployed code
The bot has run for ≥ 6 months on a paper account using the same code, same instruments, same parameters that will be used live. Net P&L is positive after all costs. No edits to the strategy during this window — only infra fixes are permitted.

**Status: ❌ FAIL**
- Deployment was within the last few days. Far from 6 months.
- This is also the gate that resets if Gate 2 is failed and a new strategy replaces the old one — the 6-month clock starts over.

---

## How to use this document

1. **Never edit a gate's *requirement* to make it pass.** Edit the code, the data, or the strategy. The gate text is the contract.
2. **Update the status section** after every meaningful change to the bot. PASS can become FAIL if you change strategy, instruments, or material code.
3. **All 12 must be PASS or 🟦 N/A simultaneously** for real money. Even one ❌ is a stop.
4. **If a gate is N/A, write the justification inline.** "N/A because we don't use stops" is not a justification; "N/A because this strategy only takes long positions in cash markets, where short-side stop handling does not apply" is.
5. **Re-read this document before any real-money decision, no matter how confident you feel.** Confidence is not a gate.

---

_Last reviewed: 2026-05-26. Owner: project maintainer. Generated alongside the slippage fix (commit 0fb76ac) and Deflated Sharpe analysis (commit 8b294d1)._
