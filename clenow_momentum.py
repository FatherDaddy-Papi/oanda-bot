"""Clenow-shaped cross-sectional momentum on D1, multi-asset basket.

Adapted from Andreas Clenow's "Stocks on the Move" recipe. NOT a faithful
reproduction -- Clenow's universe is the S&P 500 (500 names). We have a
14-instrument multi-asset basket. The shallow cross-section materially
weakens the diversification argument Clenow relies on. Read this as
"Clenow-shaped," not Clenow.

Recipe (per rebalance, weekly on Friday close, executed at next bar):
  1. For each instrument, compute trend strength =
        slope(log_price ~ time, lookback days) * R^2     (annualized)
     Slope from OLS regression of log price on time index.
  2. Filter: keep only instruments where close > SMA(ma_period).
  3. Rank surviving instruments by trend strength, descending.
  4. Hold top K. Position weights are volatility-parity:
        weight_i = (1 / atr_pct_i) / sum_{j in held}(1 / atr_pct_j)
     so each held position contributes equal expected daily vol.
  5. Friction: per-instrument realistic spread is charged on every
     entry and exit (relative to entry price). Reused from
     backtest_rsi.INSTRUMENT_SPECS.

We track per-week portfolio returns (one observation per rebalance) as
the basis for Sharpe and DSR. Equity curve is cumulative compounded.
"""
import math
import os
from datetime import datetime
from dotenv import load_dotenv

from backtest_rsi import fetch_candles, specs_for, INSTRUMENT_SPECS

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

UNIVERSE = [
    "EUR_USD", "GBP_USD", "AUD_USD", "USD_JPY", "USD_CAD",
    "NAS100_USD", "SPX500_USD", "US30_USD", "DE30_EUR",
    "XAU_USD", "XAG_USD", "BCO_USD", "WTICO_USD", "NATGAS_USD",
]
ATR_PERIOD = 20


def trend_strength(closes):
    """OLS slope of log price on time, scaled by R^2. Annualized for D1.
    Returns 0 if degenerate (e.g. zero variance)."""
    n = len(closes)
    if n < 2 or any(c <= 0 for c in closes):
        return 0.0
    log_p = [math.log(c) for c in closes]
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(log_p) / n
    num = sum((xs[i] - mx) * (log_p[i] - my) for i in range(n))
    den_x = sum((x - mx) ** 2 for x in xs)
    den_y = sum((y - my) ** 2 for y in log_p)
    if den_x == 0 or den_y == 0:
        return 0.0
    slope = num / den_x          # log-return per day
    r = num / math.sqrt(den_x * den_y)
    return slope * 252.0 * (r * r)   # annualized, R^2-scaled


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr_pct(bars, period=ATR_PERIOD):
    """ATR as fraction of current close (i.e. relative volatility)."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h = bars[i]["h"]; l = bars[i]["l"]; pc = bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / period
    return atr / bars[-1]["c"]


def friction_pct(instrument):
    """Round-trip cost (entry + exit) as fraction of price.
    Uses per-instrument realistic spread."""
    pip, spread_pips = specs_for(instrument)
    # Need an anchor price. Use a representative recent close from when run.
    # In the backtest loop we pass the actual price, so this is a fallback only.
    spread_abs = spread_pips * pip
    # Per-rebalance cost = full spread (cross the spread to enter + cross to exit).
    # When held position is rolled forward (same instrument in next top-K), we
    # don't actually transact -- the backtest loop tracks transitions and only
    # charges friction on actual entries/exits.
    return spread_abs


def backtest_portfolio(bars_by_inst, lookback, ma_period, top_k):
    """Run the portfolio backtest. Returns (per_week_returns, equity_curve).

    All instruments must have aligned dates. We iterate over the date series
    of the first instrument (assumed reference); skip any rebalance where
    an instrument has missing data.
    """
    insts = list(bars_by_inst.keys())
    ref = bars_by_inst[insts[0]]
    # Align all instruments to the dates of the reference
    dates_ref = [b["time"][:10] for b in ref]
    bars_by_date = {}  # inst -> dict[date] -> bar
    for inst, bars in bars_by_inst.items():
        bars_by_date[inst] = {b["time"][:10]: b for b in bars}

    # Determine indices where we can rebalance: Friday closes
    # Weekly cadence = every 5 bars approximately for D1 instruments that trade Mon-Fri.
    # We rebalance every 5 D1 bars in the reference series, starting after enough
    # warmup for lookback and ma_period.
    warmup = max(lookback, ma_period) + 2
    held = {}     # inst -> (entry_price_per_inst, weight)
    weekly_returns = []
    equity = 1.0
    equity_curve = [equity]

    for i in range(warmup, len(ref) - 1, 5):
        # at end of bar i, we form rankings, execute at close (model fill at close)
        rankings = []
        for inst in insts:
            d = dates_ref[i]
            if d not in bars_by_date[inst]:
                continue
            bars_inst = [b for b in bars_by_inst[inst] if b["time"][:10] <= d]
            if len(bars_inst) < warmup:
                continue
            closes = [b["c"] for b in bars_inst]
            cl = closes[-1]
            ts = trend_strength(closes[-lookback:])
            ma = sma(closes, ma_period)
            if ma is None or cl <= ma:
                continue  # not in uptrend
            a = atr_pct(bars_inst, ATR_PERIOD)
            if a is None or a <= 0:
                continue
            rankings.append((inst, ts, cl, a))

        # Pick top K by trend strength
        rankings.sort(key=lambda r: r[1], reverse=True)
        chosen = rankings[:top_k]
        if not chosen:
            # No positions; carry equity flat for the week
            weekly_returns.append(0.0)
            equity_curve.append(equity)
            continue

        # Vol-parity weights
        inv_vols = [1.0 / r[3] for r in chosen]
        total_iv = sum(inv_vols)
        weights = {r[0]: iv / total_iv for r, iv in zip(chosen, inv_vols)}

        # Build new holdings dict
        new_held = {r[0]: r[2] for r in chosen}  # inst -> entry_price (this bar's close)

        # Forward returns: from this bar's close (i) to next rebalance bar's close (i+5)
        i_next = min(i + 5, len(ref) - 1)
        wk_ret = 0.0
        for inst, w in weights.items():
            p_now = bars_by_date[inst].get(dates_ref[i])
            p_next_date = dates_ref[i_next]
            p_next = bars_by_date[inst].get(p_next_date)
            if p_now is None or p_next is None:
                continue
            raw_ret = (p_next["c"] - p_now["c"]) / p_now["c"]
            # Friction: full round-trip spread only if we enter THIS week or exit at end.
            # Simpler honest accounting: charge half-spread on every weekly rebalance
            # transition for changes. Roll-forwards pay nothing.
            pip, spread_pips = specs_for(inst)
            spread_rel = (spread_pips * pip) / p_now["c"]
            entered_now = inst not in held
            exits_next = False  # decided at i_next; assume worst case (charge if not in prior held)
            # Approximation: charge 0.5*spread for entry, 0.5*spread for exit on
            # any new position. Roll-forwards (in both held and new_held) skip both.
            friction = 0.0
            if entered_now:
                friction += 0.5 * spread_rel  # entry
            # Exit will be charged when this position is closed (handled implicitly
            # by symmetric treatment next iteration). To keep math simple we charge
            # the FULL round-trip up front on any new entry.
            if entered_now:
                friction += 0.5 * spread_rel  # exit (estimated)
            wk_ret += w * (raw_ret - friction)

        weekly_returns.append(wk_ret)
        equity *= (1.0 + wk_ret)
        equity_curve.append(equity)
        held = new_held

    return weekly_returns, equity_curve


def fetch_universe(years=5):
    """Fetch D1 candles for the entire universe. Cached in memory per process."""
    print(f"Fetching {years}y D1 for {len(UNIVERSE)} instruments...")
    out = {}
    for inst in UNIVERSE:
        bars = fetch_candles(inst, years, "D")
        print(f"  {inst:<12} {len(bars)} bars")
        out[inst] = bars
    return out


def summarize(weekly_returns, equity_curve):
    if not weekly_returns:
        return {"n": 0}
    n = len(weekly_returns)
    m = sum(weekly_returns) / n
    var = sum((r - m) ** 2 for r in weekly_returns) / (n - 1) if n > 1 else 0
    sd = math.sqrt(var) if var > 0 else 0
    sharpe_week = m / sd if sd > 0 else 0
    sharpe_ann = sharpe_week * math.sqrt(52)  # weekly observations
    wins = [r for r in weekly_returns if r > 0]
    final_equity = equity_curve[-1] if equity_curve else 1.0
    peak = -math.inf
    max_dd = 0.0
    for e in equity_curve:
        if e > peak: peak = e
        max_dd = max(max_dd, (peak - e) / peak)
    return {
        "n": n, "mean_week": m, "sd_week": sd,
        "sharpe_ann": sharpe_ann, "final_equity": final_equity,
        "total_return_pct": (final_equity - 1.0) * 100,
        "win_rate": 100 * len(wins) / n, "max_dd_pct": max_dd * 100,
    }


def main():
    bars = fetch_universe(years=5)
    print(f"\nRunning default config: lookback=90, ma=100, top_k=5")
    wr, eq = backtest_portfolio(bars, lookback=90, ma_period=100, top_k=5)
    s = summarize(wr, eq)
    print()
    print("=" * 60)
    print(f"  Clenow-shaped momentum, default config")
    print("=" * 60)
    print(f"  Rebalances:      {s['n']}")
    print(f"  Total return:    {s['total_return_pct']:+.1f}%")
    print(f"  Sharpe (ann):    {s['sharpe_ann']:+.2f}")
    print(f"  Weekly win rate: {s['win_rate']:.1f}%")
    print(f"  Max drawdown:    {s['max_dd_pct']:.1f}%")
    print("=" * 60)


if __name__ == "__main__":
    main()
