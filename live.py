"""
Live RSI(2) + SMA(200) bot for OANDA practice. Multi-instrument capable.

Usage:
    python live.py                # default: EUR_USD
    python live.py XAU_USD        # gold
    python live.py EUR_USD 1H 1000 0.0001   # full custom

State and logs are per-instrument (e.g. bot_state_XAU_USD.json, bot_XAU_USD.log).
"""
import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles
from oandapyV20.endpoints import orders, positions, trades as trades_ep
from oandapyV20.exceptions import V20Error

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
API_TOKEN = os.getenv("OANDA_API_TOKEN")
ENV = os.getenv("OANDA_ENV", "practice")

# Per-instrument config: units sized so notional ~$1k-10k, pip = OANDA's pipLocation
INSTRUMENT_CONFIG = {
    "EUR_USD": {"units": 1000,  "pip": 0.0001},
    "GBP_USD": {"units": 1000,  "pip": 0.0001},
    "USD_JPY": {"units": 1000,  "pip": 0.01},
    "AUD_USD": {"units": 1000,  "pip": 0.0001},
    "USD_CAD": {"units": 1000,  "pip": 0.0001},
    "XAU_USD": {"units": 10,    "pip": 0.01},     # 10oz gold, ~$46k notional - oversized; reduce to 1-5 if too much
    "XAG_USD": {"units": 50,    "pip": 0.0001},
    "SPX500_USD": {"units": 5,  "pip": 0.1},
    "NAS100_USD": {"units": 1,  "pip": 0.1},
    "BCO_USD": {"units": 100,   "pip": 0.01},
    "WTICO_USD": {"units": 100, "pip": 0.01},
}

# --- CLI args ---
INSTRUMENT = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
GRAN = sys.argv[2] if len(sys.argv) > 2 else "H1"
_cfg = INSTRUMENT_CONFIG.get(INSTRUMENT, {"units": 1000, "pip": 0.0001})
UNITS = int(sys.argv[3]) if len(sys.argv) > 3 else _cfg["units"]
PIP = float(sys.argv[4]) if len(sys.argv) > 4 else _cfg["pip"]

# --- strategy params ---
RSI_PERIOD = 2
RSI_LO = 10
RSI_HI = 90
SMA_EXIT = 5
SMA_TREND = 200
MAX_HOLD = 10
BOT_TAG = f"rsi-bot-{INSTRUMENT}"

# --- risk management (Vince + Clenow + Lopez de Prado) ---
# Equal-risk position sizing: risk fixed % of account per trade, sized by ATR
RISK_PER_TRADE_PCT = 0.0025   # 0.25% of NAV per trade (= 1/4 Kelly given our edge)
ATR_PERIOD = 14
STOP_ATR_MULTIPLE = 2.0       # stop = entry ± 2*ATR

# Account-level circuit breakers
DAILY_LOSS_LIMIT_PCT = 0.02   # stop trading if down 2% on the day
WEEKLY_LOSS_LIMIT_PCT = 0.05  # stop trading if down 5% on the week
MAX_CONSECUTIVE_LOSSES = 6    # pause after 6 losers in a row

# Floor on units (avoid placing tiny meaningless trades on low-vol days)
MIN_UNITS = max(1, int(_cfg.get("units", 1000) // 10))
MAX_UNITS = int(_cfg.get("units", 1000) * 5)  # cap regardless of vol

STATE_FILE = os.path.join(os.path.dirname(__file__), f"bot_state_{INSTRUMENT}.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), f"bot_{INSTRUMENT}.log")

client = API(access_token=API_TOKEN, environment=ENV)


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()[:19]}Z] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"position": 0, "entry_price": 0.0, "entry_time": None, "bars_held": 0, "trade_id": None}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# --- indicators ---
def rsi(closes, period):
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = max(d, 0); l = max(-d, 0)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr(bars, period=ATR_PERIOD):
    """Average True Range over `period` bars."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h = bars[i]["h"]; l = bars[i]["l"]; pc = bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


# --- risk-based position sizing (Clenow / Vince) ---
def get_account_nav():
    """Account net asset value in account currency (SGD for us)."""
    from oandapyV20.endpoints.accounts import AccountSummary
    r = AccountSummary(accountID=ACCOUNT_ID)
    client.request(r)
    return float(r.response["account"]["NAV"])


def size_by_risk(nav, atr_val, stop_atr_multiple=STOP_ATR_MULTIPLE):
    """Equal-risk sizing: risk = RISK_PER_TRADE_PCT * NAV; units = risk / (stop_distance * pip_value).
    For instruments quoted in account currency, pip_value approximates 1 per unit per pip.
    This is an approximation; for cross-currency pairs OANDA's pricing handles conversion.
    """
    risk_dollars = nav * RISK_PER_TRADE_PCT
    stop_distance = stop_atr_multiple * atr_val   # in price units
    # P/L per unit per 1.0 price move = 1 (in quote currency). Account-currency conversion is rough.
    units = risk_dollars / stop_distance if stop_distance > 0 else MIN_UNITS
    units = int(max(MIN_UNITS, min(MAX_UNITS, units)))
    return units


# --- circuit breakers (Lopez de Prado-style risk discipline) ---
def circuit_breaker_tripped(state, nav):
    """Returns (tripped, reason) tuple. Pauses trading if drawdown limits hit."""
    # daily loss limit
    today = datetime.now(timezone.utc).date().isoformat()
    daily = state.get("daily_pnl", {}).get(today, 0.0)
    daily_limit = -DAILY_LOSS_LIMIT_PCT * nav
    if daily < daily_limit:
        return True, f"daily loss limit hit ({daily:.2f} < {daily_limit:.2f})"
    # weekly
    iso_year, iso_week, _ = datetime.now(timezone.utc).isocalendar()
    week_key = f"{iso_year}-W{iso_week}"
    weekly = state.get("weekly_pnl", {}).get(week_key, 0.0)
    weekly_limit = -WEEKLY_LOSS_LIMIT_PCT * nav
    if weekly < weekly_limit:
        return True, f"weekly loss limit hit ({weekly:.2f} < {weekly_limit:.2f})"
    # consecutive losses
    cons = state.get("consecutive_losses", 0)
    if cons >= MAX_CONSECUTIVE_LOSSES:
        return True, f"max consecutive losses ({cons}) reached - manual reset required"
    return False, ""


def record_trade_pnl(state, pnl_val):
    """Update daily/weekly pnl rollups + consecutive loss counter."""
    try:
        pnl = float(pnl_val)
    except (TypeError, ValueError):
        return state
    today = datetime.now(timezone.utc).date().isoformat()
    iso_year, iso_week, _ = datetime.now(timezone.utc).isocalendar()
    week_key = f"{iso_year}-W{iso_week}"
    state.setdefault("daily_pnl", {})[today] = state.get("daily_pnl", {}).get(today, 0.0) + pnl
    state.setdefault("weekly_pnl", {})[week_key] = state.get("weekly_pnl", {}).get(week_key, 0.0) + pnl
    if pnl < 0:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
    else:
        state["consecutive_losses"] = 0
    return state


# --- broker ops ---
def fetch_recent(n=260):
    params = {"granularity": GRAN, "count": n, "price": "M"}
    r = InstrumentsCandles(instrument=INSTRUMENT, params=params)
    client.request(r)
    cs = [c for c in r.response["candles"] if c.get("complete")]
    return [{"time": c["time"], "c": float(c["mid"]["c"]),
             "h": float(c["mid"]["h"]), "l": float(c["mid"]["l"]),
             "o": float(c["mid"]["o"])} for c in cs]


def place_market(side, units_abs, stop_price=None):
    """Place a market order with optional broker-side stop loss."""
    units = units_abs if side == 1 else -units_abs
    order = {
        "instrument": INSTRUMENT, "units": str(units),
        "type": "MARKET", "timeInForce": "FOK", "positionFill": "DEFAULT",
        "clientExtensions": {"tag": BOT_TAG, "id": f"{BOT_TAG}-{int(time.time())}"},
        "tradeClientExtensions": {"tag": BOT_TAG},
    }
    if stop_price is not None:
        precision = 5 if PIP < 0.001 else 3
        order["stopLossOnFill"] = {"price": f"{round(stop_price, precision)}"}
    r = orders.OrderCreate(accountID=ACCOUNT_ID, data={"order": order})
    client.request(r)
    fill = r.response.get("orderFillTransaction")
    if not fill:
        log(f"!! Order not filled: {json.dumps(r.response)[:300]}")
        return None
    return fill


def close_bot_position(state):
    """Close only the bot's trade by ID, leaving manual trades alone."""
    tid = state.get("trade_id")
    if not tid:
        return None
    try:
        r = trades_ep.TradeClose(accountID=ACCOUNT_ID, tradeID=str(tid))
        client.request(r)
        fill = r.response.get("orderFillTransaction")
        return fill
    except V20Error as e:
        log(f"!! Close failed for trade {tid}: {e}")
        return None


def market_is_open(bars):
    """OANDA H1 candle timestamps are open-time, so a freshly-closed candle is already ~60 min old.
    If the most recent complete candle is more than ~3h old, market is likely closed (weekend)."""
    if not bars:
        return False
    last = datetime.fromisoformat(bars[-1]["time"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age < 180 * 60


def sync_with_broker(state):
    """If state says we have a trade but broker doesn't (or vice versa), reconcile."""
    tid = state.get("trade_id")
    if tid is None:
        return state
    try:
        r = trades_ep.TradeDetails(accountID=ACCOUNT_ID, tradeID=str(tid))
        client.request(r)
        tr = r.response.get("trade", {})
        if tr.get("state") != "OPEN":
            log(f"Trade {tid} no longer open at broker (was {state['position']}). Resetting state to FLAT.")
            return {"position": 0, "entry_price": 0.0, "entry_time": None, "bars_held": 0, "trade_id": None}
    except V20Error:
        log(f"Trade {tid} not found at broker. Resetting state to FLAT.")
        return {"position": 0, "entry_price": 0.0, "entry_time": None, "bars_held": 0, "trade_id": None}
    return state


# --- main tick ---
def tick():
    bars = fetch_recent(260)
    if not market_is_open(bars):
        log("Market appears closed (weekend/holiday). Skipping.")
        return

    closes = [b["c"] for b in bars]
    last_close = closes[-1]
    last_time = bars[-1]["time"][:19]
    rsi_val = rsi(closes, RSI_PERIOD)
    sma5 = sma(closes, SMA_EXIT)
    sma200 = sma(closes, SMA_TREND)
    if rsi_val is None or sma5 is None or sma200 is None:
        log("Not enough data yet.")
        return

    state = sync_with_broker(load_state())
    pos = state["position"]
    atr_val = atr(bars, ATR_PERIOD)

    log(f"BAR {last_time}  close={last_close:.5f}  RSI(2)={rsi_val:.1f}  "
        f"SMA5={sma5:.5f}  SMA200={sma200:.5f}  ATR={atr_val:.5f}  pos={pos}  held={state.get('bars_held',0)}")

    # EXIT logic first
    if pos == 1:
        state["bars_held"] += 1
        if last_close > sma5 or state["bars_held"] >= MAX_HOLD:
            fill = close_bot_position(state)
            if fill:
                pnl = fill.get("pl", "?")
                log(f"EXIT LONG  trade={state['trade_id']}  price={fill['price']}  pnl={pnl}")
                state = record_trade_pnl(state, pnl)
            state.update({"position": 0, "entry_price": 0.0, "entry_time": None,
                          "bars_held": 0, "trade_id": None})
            pos = 0
    elif pos == -1:
        state["bars_held"] += 1
        if last_close < sma5 or state["bars_held"] >= MAX_HOLD:
            fill = close_bot_position(state)
            if fill:
                pnl = fill.get("pl", "?")
                log(f"EXIT SHORT trade={state['trade_id']}  price={fill['price']}  pnl={pnl}")
                state = record_trade_pnl(state, pnl)
            state.update({"position": 0, "entry_price": 0.0, "entry_time": None,
                          "bars_held": 0, "trade_id": None})
            pos = 0

    # ENTRY logic with risk-based sizing + circuit breakers
    if pos == 0:
        signal_long = rsi_val < RSI_LO and last_close > sma200
        signal_short = rsi_val > RSI_HI and last_close < sma200
        if signal_long or signal_short:
            try:
                nav = get_account_nav()
            except Exception as e:
                log(f"!! Could not fetch NAV, skipping entry: {e}")
                save_state(state); return
            tripped, reason = circuit_breaker_tripped(state, nav)
            if tripped:
                log(f"BLOCKED entry: {reason}")
                save_state(state); return
            if atr_val is None or atr_val <= 0:
                log(f"BLOCKED entry: ATR unavailable")
                save_state(state); return
            units_abs = size_by_risk(nav, atr_val)
            if signal_long:
                stop_price = last_close - STOP_ATR_MULTIPLE * atr_val
                fill = place_market(1, units_abs, stop_price)
                if fill:
                    tid = fill.get("tradeOpened", {}).get("tradeID")
                    state.update({"position": 1, "entry_price": float(fill["price"]),
                                  "entry_time": last_time, "bars_held": 0, "trade_id": tid})
                    log(f"ENTRY LONG  trade={tid}  price={fill['price']}  units={units_abs}  "
                        f"stop={stop_price:.5f}  risk={RISK_PER_TRADE_PCT*100:.2f}%nav")
            else:
                stop_price = last_close + STOP_ATR_MULTIPLE * atr_val
                fill = place_market(-1, units_abs, stop_price)
                if fill:
                    tid = fill.get("tradeOpened", {}).get("tradeID")
                    state.update({"position": -1, "entry_price": float(fill["price"]),
                                  "entry_time": last_time, "bars_held": 0, "trade_id": tid})
                    log(f"ENTRY SHORT trade={tid}  price={fill['price']}  units={units_abs}  "
                        f"stop={stop_price:.5f}  risk={RISK_PER_TRADE_PCT*100:.2f}%nav")

    save_state(state)


def seconds_until_next_h1_plus(buffer_sec=60):
    now = datetime.now(timezone.utc)
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return (next_hour - now).total_seconds() + buffer_sec


def main():
    log(f"=== RSI bot starting on {INSTRUMENT} {GRAN} (pip={PIP}) ===")
    log(f"Rules: RSI({RSI_PERIOD})<{RSI_LO}/>{RSI_HI}, SMA exit({SMA_EXIT}), trend({SMA_TREND}), max hold {MAX_HOLD}h")
    log(f"Risk: {RISK_PER_TRADE_PCT*100:.2f}% NAV/trade, ATR-sized, stop={STOP_ATR_MULTIPLE}x ATR")
    log(f"Limits: daily={DAILY_LOSS_LIMIT_PCT*100:.1f}%, weekly={WEEKLY_LOSS_LIMIT_PCT*100:.1f}%, max consec losses={MAX_CONSECUTIVE_LOSSES}")
    # Run one tick immediately
    try:
        tick()
    except Exception as e:
        log(f"!! Initial tick error: {e}")
    while True:
        sleep_s = seconds_until_next_h1_plus(60)
        log(f"Sleeping {sleep_s:.0f}s until next H1 close + 60s ...")
        time.sleep(sleep_s)
        try:
            tick()
        except Exception as e:
            log(f"!! Tick error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
