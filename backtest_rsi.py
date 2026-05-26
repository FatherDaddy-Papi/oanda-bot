"""
RSI(2) mean-reversion backtester (Larry Connors style).

Rules:
  - Long entry:  RSI(2) < 10
  - Long exit:   close > SMA(5)   OR  held > MAX_HOLD bars
  - Short entry: RSI(2) > 90
  - Short exit:  close < SMA(5)   OR  held > MAX_HOLD bars
  - 1 position at a time

Usage:
    python backtest_rsi.py [INSTRUMENT] [YEARS] [RSI_PERIOD] [RSI_LO] [RSI_HI] [SMA] [GRAN]
    python backtest_rsi.py EUR_USD 2 2 10 90 5 H1
"""
import os
import sys
import math
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
client = API(access_token=os.getenv("OANDA_API_TOKEN"), environment=os.getenv("OANDA_ENV", "practice"))

UNITS = 1000
MAX_HOLD = 10

# Per-instrument realistic OANDA practice spreads, sampled live (single off-peak
# snapshot, 2026-05-26). Pip values match the broker convention used in
# cloud_trade.PIP_MAP. Override via backtest(spread_pips=, pip=) for scenarios.
INSTRUMENT_SPECS = {
    "EUR_USD":    {"pip": 0.0001, "spread_pips": 1.0},
    "GBP_USD":    {"pip": 0.0001, "spread_pips": 1.5},
    "AUD_USD":    {"pip": 0.0001, "spread_pips": 1.5},
    "NZD_USD":    {"pip": 0.0001, "spread_pips": 2.0},
    "USD_CAD":    {"pip": 0.0001, "spread_pips": 1.8},
    "USD_CHF":    {"pip": 0.0001, "spread_pips": 1.8},
    "USD_JPY":    {"pip": 0.01,   "spread_pips": 1.2},
    "EUR_JPY":    {"pip": 0.01,   "spread_pips": 1.8},
    "GBP_JPY":    {"pip": 0.01,   "spread_pips": 2.4},
    "XAU_USD":    {"pip": 0.01,   "spread_pips": 81.0},
    "XAG_USD":    {"pip": 0.0001, "spread_pips": 348.0},
    "NAS100_USD": {"pip": 0.1,    "spread_pips": 19.0},
    "SPX500_USD": {"pip": 0.1,    "spread_pips": 6.0},
    "US30_USD":   {"pip": 1.0,    "spread_pips": 3.0},
}
DEFAULT_PIP = 0.0001
DEFAULT_SPREAD_PIPS = 1.0


def specs_for(instrument):
    """Return (pip, spread_pips) for an instrument, with defaults if unknown."""
    s = INSTRUMENT_SPECS.get(instrument)
    if s is None:
        return DEFAULT_PIP, DEFAULT_SPREAD_PIPS
    return s["pip"], s["spread_pips"]


def fetch_candles(instrument, years, granularity):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * years)
    all_candles = []
    cursor = start
    while cursor < end:
        params = {"granularity": granularity,
                  "from": cursor.isoformat().replace("+00:00", "Z"),
                  "count": 5000, "price": "M"}
        r = InstrumentsCandles(instrument=instrument, params=params)
        client.request(r)
        batch = [c for c in r.response.get("candles", []) if c.get("complete")]
        if not batch:
            break
        if all_candles and batch[0]["time"] == all_candles[-1]["time"]:
            batch = batch[1:]
        if not batch:
            break
        all_candles.extend(batch)
        last_time = datetime.fromisoformat(batch[-1]["time"].replace("Z", "+00:00"))
        if last_time <= cursor:
            break
        cursor = last_time + timedelta(seconds=1)
        if last_time >= end - timedelta(hours=1):
            break
    return [{"time": c["time"],
             "o": float(c["mid"]["o"]), "h": float(c["mid"]["h"]),
             "l": float(c["mid"]["l"]), "c": float(c["mid"]["c"])}
            for c in all_candles]


def rsi(closes, period):
    """Wilder's RSI; returns list aligned to closes (NaN-equivalent: None for first `period` entries)."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    # initial average gain/loss
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100 - 100 / (1 + rs)
    # Wilder smoothing
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def backtest(bars, rsi_period, rsi_lo, rsi_hi, sma_period, trend_sma=0, spread_pips=None, pip=None):
    spread_pips = DEFAULT_SPREAD_PIPS if spread_pips is None else spread_pips
    pip = DEFAULT_PIP if pip is None else pip
    closes = [b["c"] for b in bars]
    rs = rsi(closes, rsi_period)
    ma = sma(closes, sma_period)
    trend = sma(closes, trend_sma) if trend_sma > 0 else [None] * len(closes)
    position = 0
    entry_price = 0.0
    bars_held = 0
    trades = []
    equity_curve = []
    cum_pnl = 0.0

    start_i = max(rsi_period + 1, sma_period, trend_sma)
    for i in range(start_i, len(bars)):
        if rs[i] is None or ma[i] is None:
            equity_curve.append(cum_pnl)
            continue
        close = closes[i]

        # exits
        if position == 1:
            bars_held += 1
            if close > ma[i] or bars_held >= MAX_HOLD:
                exit_px = close - 0.5 * spread_pips * pip
                pnl = (exit_px - entry_price) * UNITS
                trades.append({"side": "LONG", "entry": entry_price, "exit": exit_px,
                               "pnl": pnl, "exit_time": bars[i]["time"], "bars": bars_held})
                cum_pnl += pnl
                position = 0
        elif position == -1:
            bars_held += 1
            if close < ma[i] or bars_held >= MAX_HOLD:
                exit_px = close + 0.5 * spread_pips * pip
                pnl = (entry_price - exit_px) * UNITS
                trades.append({"side": "SHORT", "entry": entry_price, "exit": exit_px,
                               "pnl": pnl, "exit_time": bars[i]["time"], "bars": bars_held})
                cum_pnl += pnl
                position = 0

        # entries (with optional trend filter)
        if position == 0:
            trend_up = trend_sma == 0 or (trend[i] is not None and close > trend[i])
            trend_dn = trend_sma == 0 or (trend[i] is not None and close < trend[i])
            if rs[i] < rsi_lo and trend_up:
                entry_price = close + 0.5 * spread_pips * pip
                position = 1
                bars_held = 0
            elif rs[i] > rsi_hi and trend_dn:
                entry_price = close - 0.5 * spread_pips * pip
                position = -1
                bars_held = 0

        equity_curve.append(cum_pnl)

    return trades, equity_curve, position


def stats(trades, equity_curve):
    if not trades:
        return {"n": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    peak = -math.inf
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        max_dd = max(max_dd, peak - eq)
    return {
        "n": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": total,
        "avg_win": (sum(wins) / len(wins)) if wins else 0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0,
        "expectancy": total / len(trades),
        "max_dd": max_dd,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) != 0 else float("inf"),
        "avg_bars_held": sum(t["bars"] for t in trades) / len(trades),
    }


def main():
    instrument = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    years = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    rsi_period = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    rsi_lo = float(sys.argv[4]) if len(sys.argv) > 4 else 10
    rsi_hi = float(sys.argv[5]) if len(sys.argv) > 5 else 90
    sma_period = int(sys.argv[6]) if len(sys.argv) > 6 else 5
    gran = sys.argv[7] if len(sys.argv) > 7 else "H1"
    trend_sma = int(sys.argv[8]) if len(sys.argv) > 8 else 0

    pip, spread_pips = specs_for(instrument)
    print(f"Fetching {years}y of {instrument} {gran}...")
    bars = fetch_candles(instrument, years, gran)
    print(f"  got {len(bars)} bars  ({bars[0]['time']} -> {bars[-1]['time']})")
    filt = f", trend filter SMA({trend_sma})" if trend_sma > 0 else ""
    print(f"Running RSI({rsi_period}) <{rsi_lo}/>{rsi_hi}, exit on SMA({sma_period}) cross, max hold {MAX_HOLD}{filt}")
    print(f"Slippage: spread={spread_pips} pips, pip={pip}")
    trades, eq, final_pos = backtest(bars, rsi_period, rsi_lo, rsi_hi, sma_period, trend_sma,
                                     spread_pips=spread_pips, pip=pip)
    s = stats(trades, eq)
    print()
    print("=" * 60)
    print(f"  RESULTS  ({instrument} {gran}, {years}y, RSI{rsi_period}<{rsi_lo}/>{rsi_hi}, SMA{sma_period}, {UNITS} units)")
    print("=" * 60)
    if s["n"] == 0:
        print("  No trades.")
        return
    print(f"  Trades:         {s['n']}")
    print(f"  Wins / Losses:  {s['wins']} / {s['losses']}  ({s['win_rate']:.1f}%)")
    print(f"  Total P/L:      {s['total_pnl']:+,.2f}")
    print(f"  Avg win:        {s['avg_win']:+,.2f}")
    print(f"  Avg loss:       {s['avg_loss']:+,.2f}")
    print(f"  Expectancy:     {s['expectancy']:+,.4f}/trade")
    print(f"  Profit factor:  {s['profit_factor']:.2f}")
    print(f"  Max drawdown:   {s['max_dd']:,.2f}")
    print(f"  Avg bars held:  {s['avg_bars_held']:.1f}")
    print(f"  Final position: {'LONG' if final_pos==1 else 'SHORT' if final_pos==-1 else 'FLAT'}")
    print("=" * 60)
    print()
    print("Last 5 trades:")
    for t in trades[-5:]:
        print(f"  {t['exit_time'][:19]}  {t['side']:5s}  in {t['entry']:.5f}  out {t['exit']:.5f}  pnl {t['pnl']:+.2f}  ({t['bars']}bars)")


if __name__ == "__main__":
    main()
