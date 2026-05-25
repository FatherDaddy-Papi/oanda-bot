"""
Donchian breakout backtester on OANDA historical data.

Usage:
    python backtest.py [INSTRUMENT] [YEARS] [ENTRY] [EXIT] [GRANULARITY]
    python backtest.py EUR_USD 2 20 10 H1     # default
    python backtest.py EUR_USD 5 55 20 D      # classic Turtle on daily
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
SPREAD_PIPS = 1.0       # assumed avg spread (pips)
PIP = 0.0001            # 1 pip for EUR_USD/GBP_USD etc; JPY pairs use 0.01; gold uses 0.10


def fetch_candles(instrument, years, granularity):
    """OANDA caps at 5000 candles per request; chunk through history."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * years)
    all_candles = []
    cursor = start
    while cursor < end:
        params = {
            "granularity": granularity,
            "from": cursor.isoformat().replace("+00:00", "Z"),
            "count": 5000,
            "price": "M",
        }
        r = InstrumentsCandles(instrument=instrument, params=params)
        client.request(r)
        batch = r.response.get("candles", [])
        if not batch:
            break
        batch = [c for c in batch if c.get("complete")]
        if not batch:
            break
        # avoid dup if first candle == last of previous batch
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
    # convert to simple tuples
    bars = []
    for c in all_candles:
        m = c["mid"]
        bars.append({
            "time": c["time"],
            "o": float(m["o"]), "h": float(m["h"]),
            "l": float(m["l"]), "c": float(m["c"]),
        })
    return bars


def backtest(bars, entry_lookback, exit_lookback):
    position = 0          # +1 long, -1 short, 0 flat
    entry_price = 0.0
    trades = []
    equity_curve = []
    cum_pnl = 0.0

    for i in range(entry_lookback, len(bars)):
        prior_entry = bars[i - entry_lookback:i]
        prior_exit = bars[i - exit_lookback:i]
        hh20 = max(b["h"] for b in prior_entry)
        ll20 = min(b["l"] for b in prior_entry)
        hh10 = max(b["h"] for b in prior_exit)
        ll10 = min(b["l"] for b in prior_exit)
        close = bars[i]["c"]

        # Exits first (so a same-bar exit + entry handled properly)
        if position == 1 and close < ll10:
            # close long at close - 0.5 spread (we pay bid)
            exit_px = close - 0.5 * SPREAD_PIPS * PIP
            pnl = (exit_px - entry_price) * UNITS
            trades.append({"side": "LONG", "entry": entry_price, "exit": exit_px, "pnl": pnl,
                           "exit_time": bars[i]["time"]})
            cum_pnl += pnl
            position = 0
        elif position == -1 and close > hh10:
            exit_px = close + 0.5 * SPREAD_PIPS * PIP
            pnl = (entry_price - exit_px) * UNITS
            trades.append({"side": "SHORT", "entry": entry_price, "exit": exit_px, "pnl": pnl,
                           "exit_time": bars[i]["time"]})
            cum_pnl += pnl
            position = 0

        # Entries
        if position == 0:
            if close > hh20:
                # buy long at close + 0.5 spread
                entry_price = close + 0.5 * SPREAD_PIPS * PIP
                position = 1
            elif close < ll20:
                entry_price = close - 0.5 * SPREAD_PIPS * PIP
                position = -1

        equity_curve.append(cum_pnl)

    return trades, equity_curve, position, entry_price


def stats(trades, equity_curve):
    if not trades:
        return {"n": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    # max drawdown
    peak = -math.inf
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    return {
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": total,
        "avg_win": (sum(wins) / len(wins)) if wins else 0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0,
        "expectancy": total / len(trades),
        "max_dd": max_dd,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) != 0 else float("inf"),
    }


def main():
    instrument = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    years = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    entry_lb = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    exit_lb = int(sys.argv[4]) if len(sys.argv) > 4 else 10
    granularity = sys.argv[5] if len(sys.argv) > 5 else "H1"
    print(f"Fetching {years}y of {instrument} {granularity} candles...")
    bars = fetch_candles(instrument, years, granularity)
    if len(bars) < entry_lb + 5:
        print(f"  Not enough bars ({len(bars)}) for lookback {entry_lb}")
        return
    print(f"  got {len(bars)} bars  ({bars[0]['time']} -> {bars[-1]['time']})")
    print(f"Running Donchian {entry_lb}/{exit_lb} backtest...")
    trades, equity, final_pos, _ = backtest(bars, entry_lb, exit_lb)
    s = stats(trades, equity)
    print()
    print("=" * 60)
    print(f"  RESULTS  ({instrument} {granularity}, {years}y, {entry_lb}/{exit_lb}, {UNITS} units, 1 pip spread)")
    print("=" * 60)
    if s["n"] == 0:
        print("  No trades.")
        return
    print(f"  Trades:         {s['n']}")
    print(f"  Wins / Losses:  {s['wins']} / {s['losses']}  ({s['win_rate']:.1f}% win rate)")
    print(f"  Total P/L:      {s['total_pnl']:+,.2f}  (account ccy)")
    print(f"  Avg win:        {s['avg_win']:+,.2f}")
    print(f"  Avg loss:       {s['avg_loss']:+,.2f}")
    print(f"  Expectancy:     {s['expectancy']:+,.4f} per trade")
    print(f"  Profit factor:  {s['profit_factor']:.2f}")
    print(f"  Max drawdown:   {s['max_dd']:,.2f}")
    print(f"  Final position: {'LONG' if final_pos==1 else 'SHORT' if final_pos==-1 else 'FLAT'}")
    print("=" * 60)
    print()
    print("Last 5 trades:")
    for t in trades[-5:]:
        print(f"  {t['exit_time'][:19]}  {t['side']:5s}  in {t['entry']:.5f}  out {t['exit']:.5f}  pnl {t['pnl']:+.2f}")


if __name__ == "__main__":
    main()
