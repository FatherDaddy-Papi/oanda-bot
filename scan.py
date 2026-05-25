"""
Multi-instrument signal scanner. Shows current RSI(2), distance from key MAs,
and recent range stats so we can spot setups across markets at once.
"""
import os
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles
from oandapyV20.endpoints import pricing

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
client = API(access_token=os.getenv("OANDA_API_TOKEN"), environment=os.getenv("OANDA_ENV", "practice"))

INSTRUMENTS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "EUR_JPY", "GBP_JPY",
               "XAU_USD", "XAG_USD", "SPX500_USD", "NAS100_USD", "US30_USD", "DE30_EUR",
               "BCO_USD", "WTICO_USD", "NATGAS_USD"]


def rsi(closes, period):
    if len(closes) <= period: return None
    g = []; l = []
    for i in range(1, period+1):
        d = closes[i]-closes[i-1]
        g.append(max(d,0)); l.append(max(-d,0))
    ag = sum(g)/period; al = sum(l)/period
    for i in range(period+1, len(closes)):
        d = closes[i]-closes[i-1]
        ag = (ag*(period-1)+max(d,0))/period
        al = (al*(period-1)+max(-d,0))/period
    if al == 0: return 100.0
    return 100 - 100/(1+ag/al)


def fetch(inst, n=210, gran="H1"):
    r = InstrumentsCandles(instrument=inst, params={"granularity": gran, "count": n, "price": "M"})
    client.request(r)
    cs = [c for c in r.response["candles"] if c.get("complete")]
    return cs


def main():
    print(f"{'INSTRUMENT':<10} {'PRICE':>10} {'RSI(2)':>7} {'>SMA200?':>9} {'%24hMove':>10} {'14d ATR':>10}  {'SIGNAL'}")
    print("-" * 95)
    for inst in INSTRUMENTS:
        try:
            cs = fetch(inst, 210, "H1")
            if len(cs) < 200:
                print(f"{inst:<10}  (insufficient data)")
                continue
            closes = [float(c["mid"]["c"]) for c in cs]
            highs = [float(c["mid"]["h"]) for c in cs]
            lows = [float(c["mid"]["l"]) for c in cs]
            last = closes[-1]
            r = rsi(closes, 2)
            sma200 = sum(closes[-200:])/200
            above = "yes" if last > sma200 else "no"
            move_24h = (last/closes[-24] - 1) * 100 if len(closes) >= 25 else 0
            # 14-bar (14h) ATR as rough volatility
            atr = sum(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
                      for i in range(-14, 0)) / 14
            # signal logic
            sigs = []
            if r is not None and r < 10 and above == "yes":
                sigs.append("LONG (RSI extreme oversold in uptrend)")
            elif r is not None and r > 90 and above == "no":
                sigs.append("SHORT (RSI extreme overbought in downtrend)")
            elif r is not None and r < 15 and above == "yes":
                sigs.append("watch long")
            elif r is not None and r > 85 and above == "no":
                sigs.append("watch short")
            sig_str = " | ".join(sigs) if sigs else "-"
            print(f"{inst:<10} {last:>10.4f} {r:>7.1f} {above:>9} {move_24h:>+9.2f}% {atr:>10.4f}  {sig_str}")
        except Exception as e:
            print(f"{inst:<10}  ERROR: {e}")

if __name__ == "__main__":
    main()
