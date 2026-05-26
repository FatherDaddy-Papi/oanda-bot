"""
Single-tick stateless RSI(2)+SMA(200) bot.

Designed for GitHub Actions cron: runs once, processes the latest H1 bar,
places/closes orders if signaled, then exits. No local state file required —
position state is reconstructed from OANDA by querying open trades with our tag.

Usage:
    python tick_once.py [INSTRUMENT]
    python tick_once.py EUR_USD
"""
import os
import sys
from datetime import datetime, timezone
from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles
from oandapyV20.endpoints import orders, trades as trades_ep, accounts
from oandapyV20.exceptions import V20Error
import risk_gate

# Try to load .env for local dev; CI provides env vars directly via secrets
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
API_TOKEN = os.environ.get("OANDA_API_TOKEN")
ENV = os.environ.get("OANDA_ENV", "practice")
if not ACCOUNT_ID or not API_TOKEN:
    print("ERROR: OANDA_ACCOUNT_ID and OANDA_API_TOKEN required in env")
    sys.exit(1)

INSTRUMENT = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"

# Pip locations (manually maintained; matches OANDA's instrument metadata)
PIP_MAP = {
    "EUR_USD": 0.0001, "GBP_USD": 0.0001, "AUD_USD": 0.0001, "NZD_USD": 0.0001,
    "USD_JPY": 0.01,   "EUR_JPY": 0.01,   "GBP_JPY": 0.01,
    "USD_CAD": 0.0001, "USD_CHF": 0.0001,
    "XAU_USD": 0.01,   "XAG_USD": 0.0001,
    "SPX500_USD": 0.1, "NAS100_USD": 0.1, "US30_USD": 1.0, "DE30_EUR": 0.1,
    "BCO_USD": 0.01,   "WTICO_USD": 0.01, "NATGAS_USD": 0.01,
}
PIP = PIP_MAP.get(INSTRUMENT, 0.0001)

# Strategy params
GRAN = "H1"
RSI_PERIOD = 2
RSI_LO = 10
RSI_HI = 90
SMA_EXIT = 5
SMA_TREND = 200
MAX_HOLD_HOURS = 10
ATR_PERIOD = 14
STOP_ATR_MULTIPLE = 2.0
RISK_PER_TRADE_PCT = 0.0025  # 0.25% of NAV per trade (1/4 Kelly)
BOT_TAG = f"rsi-bot-{INSTRUMENT}"
UNITS_CAP = 50000  # hard cap on units, regardless of risk math

client = API(access_token=API_TOKEN, environment=ENV)


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()[:19]}Z] {INSTRUMENT}  {msg}", flush=True)


# --- indicators ---
def rsi(closes, period):
    if len(closes) <= period:
        return None
    gs, ls = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gs.append(max(d, 0)); ls.append(max(-d, 0))
    ag = sum(gs) / period; al = sum(ls) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr(bars, period=ATR_PERIOD):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h = bars[i]["h"]; l = bars[i]["l"]; pc = bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


# --- broker ops ---
def fetch_bars(n=260):
    r = InstrumentsCandles(instrument=INSTRUMENT, params={"granularity": GRAN, "count": n, "price": "M"})
    client.request(r)
    cs = [c for c in r.response["candles"] if c.get("complete")]
    return [{"time": c["time"], "c": float(c["mid"]["c"]),
             "h": float(c["mid"]["h"]), "l": float(c["mid"]["l"]),
             "o": float(c["mid"]["o"])} for c in cs]


def get_open_bot_trade():
    """Look up our bot's open trade by clientExtensions.tag. Returns None if flat."""
    r = trades_ep.OpenTrades(accountID=ACCOUNT_ID)
    client.request(r)
    for t in r.response.get("trades", []):
        if t["instrument"] != INSTRUMENT:
            continue
        ext = t.get("clientExtensions") or {}
        if ext.get("tag") == BOT_TAG:
            return t
    return None


def get_nav():
    r = accounts.AccountSummary(accountID=ACCOUNT_ID)
    client.request(r)
    return float(r.response["account"]["NAV"])


def market_is_open(bars):
    if not bars:
        return False
    last = datetime.fromisoformat(bars[-1]["time"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age < 180 * 60


def place_market(side, units_abs, stop_price):
    units = units_abs if side == 1 else -units_abs
    precision = 5 if PIP < 0.001 else 3
    data = {"order": {
        "instrument": INSTRUMENT, "units": str(units),
        "type": "MARKET", "timeInForce": "FOK", "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": f"{round(stop_price, precision)}"},
        "clientExtensions": {"tag": BOT_TAG,
                              "id": f"{BOT_TAG}-{int(datetime.now(timezone.utc).timestamp())}"},
        "tradeClientExtensions": {"tag": BOT_TAG},
    }}
    r = orders.OrderCreate(accountID=ACCOUNT_ID, data=data)
    client.request(r)
    return r.response.get("orderFillTransaction")


def close_trade(tid):
    r = trades_ep.TradeClose(accountID=ACCOUNT_ID, tradeID=str(tid))
    client.request(r)
    return r.response.get("orderFillTransaction")


def hours_since(iso_ts):
    # OANDA timestamps like "2026-05-25T17:00:00.000000000Z" or with milliseconds
    clean = iso_ts.split(".")[0] + "+00:00"
    try:
        dt = datetime.fromisoformat(clean)
    except ValueError:
        # fallback: assume Z
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00").split(".")[0] + "+00:00")
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


# --- main ---
def main():
    bars = fetch_bars(260)
    if not market_is_open(bars):
        log("Market closed. Skipping.")
        return

    closes = [b["c"] for b in bars]
    last_close = closes[-1]
    last_time = bars[-1]["time"][:19]
    rsi_val = rsi(closes, RSI_PERIOD)
    sma5 = sma(closes, SMA_EXIT)
    sma200 = sma(closes, SMA_TREND)
    atr_val = atr(bars, ATR_PERIOD)
    if any(v is None for v in (rsi_val, sma5, sma200, atr_val)):
        log("Insufficient data — need more bars.")
        return

    open_trade = get_open_bot_trade()
    pos = 0
    if open_trade:
        cu = int(open_trade["currentUnits"])
        pos = 1 if cu > 0 else -1 if cu < 0 else 0

    log(f"BAR {last_time}  close={last_close:.5f}  RSI={rsi_val:.1f}  "
        f"SMA5={sma5:.5f}  SMA200={sma200:.5f}  ATR={atr_val:.5f}  pos={pos}")

    # --- EXIT ---
    if open_trade and pos != 0:
        hours = hours_since(open_trade["openTime"])
        exit_now = False
        reason = ""
        if pos == 1 and last_close > sma5:
            exit_now = True; reason = "close>SMA5"
        elif pos == -1 and last_close < sma5:
            exit_now = True; reason = "close<SMA5"
        elif hours >= MAX_HOLD_HOURS:
            exit_now = True; reason = f"max hold {hours:.1f}h"
        if exit_now:
            try:
                fill = close_trade(open_trade["id"])
                if fill:
                    log(f"EXIT  trade={open_trade['id']}  price={fill['price']}  "
                        f"pnl={fill.get('pl', '?')}  ({reason})")
                else:
                    log(f"EXIT requested ({reason}) but no fill returned")
            except V20Error as e:
                log(f"!! Close failed: {e}")
            return  # don't open new position same bar

    # --- ENTRY (only if flat) ---
    if pos == 0:
        signal_long = rsi_val < RSI_LO and last_close > sma200
        signal_short = rsi_val > RSI_HI and last_close < sma200
        if not (signal_long or signal_short):
            log("No signal.")
            return
        try:
            allow, reason = risk_gate.check(client, ACCOUNT_ID)
        except Exception as e:
            log(f"!! Risk gate query failed, blocking entry: {e}")
            return
        if not allow:
            log(f"BLOCKED  {reason}")
            return
        log(f"Risk gate: {reason}")
        try:
            nav = get_nav()
        except Exception as e:
            log(f"!! NAV fetch failed: {e}")
            return
        risk_dollars = nav * RISK_PER_TRADE_PCT
        stop_dist = STOP_ATR_MULTIPLE * atr_val
        units = int(max(1, min(UNITS_CAP, risk_dollars / stop_dist)))
        if signal_long:
            stop_price = last_close - stop_dist
            try:
                fill = place_market(1, units, stop_price)
                if fill:
                    log(f"ENTRY LONG  units={units}  price={fill['price']}  "
                        f"stop={stop_price:.5f}  risk={RISK_PER_TRADE_PCT*100:.2f}%nav")
                else:
                    log("ENTRY LONG attempted, no fill")
            except V20Error as e:
                log(f"!! Entry failed: {e}")
        else:
            stop_price = last_close + stop_dist
            try:
                fill = place_market(-1, units, stop_price)
                if fill:
                    log(f"ENTRY SHORT units={units}  price={fill['price']}  "
                        f"stop={stop_price:.5f}  risk={RISK_PER_TRADE_PCT*100:.2f}%nav")
                else:
                    log("ENTRY SHORT attempted, no fill")
            except V20Error as e:
                log(f"!! Entry failed: {e}")


if __name__ == "__main__":
    main()
