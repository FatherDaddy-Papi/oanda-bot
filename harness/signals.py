"""Signal layer: transparent, textbook trend/momentum indicators.

Every component returns a discrete vote in {-1, 0, +1}. The composite is their
mean, so it lives in [-1, +1] and you can always see WHY it landed there.

Nothing here is fitted or optimized. These are the standard building blocks the
"AI trades for me" videos gesture at. The honesty is in keeping them legible and
NOT claiming they constitute an edge.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


@dataclass
class Signal:
    instrument: str
    asof: str
    price: float
    direction: str               # LONG | SHORT | FLAT
    composite: float             # [-1, +1]
    atr: float                   # absolute ATR in price units
    atr_pct: float               # ATR as % of price (volatility context)
    components: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def compute(instrument: str, df: pd.DataFrame, p: dict) -> Signal:
    """Compute the composite signal for one instrument's OHLCV frame."""
    close = df["close"]
    last = df.iloc[-1]
    price = float(last["close"])

    ema_fast = ema(close, p["ema_fast"])
    ema_slow = ema(close, p["ema_slow"])
    regime = close.rolling(p["regime_ma"]).mean()
    atr_series = atr(df, p["atr"])
    atr_now = float(atr_series.iloc[-1])

    # Donchian channel uses prior bars (exclude current) so a fresh close can break it.
    upper = df["high"].rolling(p["donchian"]).max().shift(1)
    lower = df["low"].rolling(p["donchian"]).min().shift(1)

    roc = close.pct_change(p["momentum"])

    votes: dict[str, int] = {}

    # 1. EMA trend
    votes["ema_cross"] = 1 if ema_fast.iloc[-1] > ema_slow.iloc[-1] else -1

    # 2. Long-term regime filter
    reg = regime.iloc[-1]
    votes["regime"] = 0 if np.isnan(reg) else (1 if price > reg else -1)

    # 3. Donchian breakout
    if not np.isnan(upper.iloc[-1]) and price >= upper.iloc[-1]:
        votes["donchian"] = 1
    elif not np.isnan(lower.iloc[-1]) and price <= lower.iloc[-1]:
        votes["donchian"] = -1
    else:
        votes["donchian"] = 0

    # 4. Absolute momentum
    m = roc.iloc[-1]
    votes["momentum"] = 0 if np.isnan(m) else (1 if m > 0 else -1)

    composite = float(np.mean(list(votes.values())))

    # Regime acts as a gate: don't fight the long-term trend.
    if votes["regime"] == 1 and composite < 0:
        composite = 0.0
    elif votes["regime"] == -1 and composite > 0:
        composite = 0.0

    if composite > 0.25:
        direction = "LONG"
    elif composite < -0.25:
        direction = "SHORT"
    else:
        direction = "FLAT"

    notes = []
    if np.isnan(reg):
        notes.append(f"insufficient history for {p['regime_ma']}-bar regime MA")
    atr_pct = (atr_now / price * 100) if price else 0.0

    return Signal(
        instrument=instrument,
        asof=str(df.index[-1]),
        price=price,
        direction=direction,
        composite=round(composite, 3),
        atr=round(atr_now, 4),
        atr_pct=round(atr_pct, 3),
        components=votes,
        notes=notes,
    )


def compute_all(frames: dict[str, pd.DataFrame], p: dict) -> list[Signal]:
    return [compute(name, df, p) for name, df in frames.items()]
