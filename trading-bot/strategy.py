"""Opening Range Breakout (ORB) signals. Pure logic, no broker calls."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Bar:
    timestamp: datetime  # bar start time, timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def pct(self) -> float:
        return (self.high - self.low) / self.low * 100


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: str  # "buy" (long) or "sell" (short)
    entry: float
    stop: float
    target: float

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)


@dataclass(frozen=True)
class StrategyConfig:
    opening_range_minutes: int = 15
    reward_multiple: float = 2.0
    stop_mode: str = "range_low"  # "range_low" or "range_mid"
    min_range_pct: float = 0.3
    max_range_pct: float = 5.0
    max_chase_pct: float = 0.5
    allow_shorts: bool = False


def opening_range(bars: list[Bar], session_open: datetime, minutes: int) -> OpeningRange | None:
    """High/low of the first `minutes` of the session, once that window has closed."""
    end = session_open + timedelta(minutes=minutes)
    window = [b for b in bars if session_open <= b.timestamp < end]
    if not window or not any(b.timestamp >= end for b in bars):
        return None
    return OpeningRange(high=max(b.high for b in window), low=min(b.low for b in window))


def find_breakout(symbol: str, bars: list[Bar], session_open: datetime, cfg: StrategyConfig) -> TradePlan | None:
    """Return a trade plan if the latest completed bar closed outside the opening range."""
    rng = opening_range(bars, session_open, cfg.opening_range_minutes)
    if rng is None or not (cfg.min_range_pct <= rng.pct <= cfg.max_range_pct):
        return None

    last = bars[-1]
    price = last.close

    if price > rng.high and price <= rng.high * (1 + cfg.max_chase_pct / 100):
        stop = rng.low if cfg.stop_mode == "range_low" else rng.mid
        return _plan(symbol, "buy", price, stop, cfg.reward_multiple)

    if cfg.allow_shorts and price < rng.low and price >= rng.low * (1 - cfg.max_chase_pct / 100):
        stop = rng.high if cfg.stop_mode == "range_low" else rng.mid
        return _plan(symbol, "sell", price, stop, cfg.reward_multiple)

    return None


def _plan(symbol: str, side: str, entry: float, stop: float, reward_multiple: float) -> TradePlan | None:
    risk = abs(entry - stop)
    target = entry + reward_multiple * risk if side == "buy" else entry - reward_multiple * risk
    entry, stop, target = round(entry, 2), round(stop, 2), round(target, 2)
    # Broker requires the stop and target to be at least a cent away from the entry.
    if abs(entry - stop) < 0.01 or abs(target - entry) < 0.01:
        return None
    return TradePlan(symbol=symbol, side=side, entry=entry, stop=stop, target=target)
