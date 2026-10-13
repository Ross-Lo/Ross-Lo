"""Position sizing, risk tiers and kill switches. Pure logic, no broker calls."""
from __future__ import annotations

import math
from dataclasses import dataclass

from strategy import TradePlan


@dataclass(frozen=True)
class Tier:
    name: str
    up_to_peak_equity: float | None  # tier applies while peak equity is below this; None = no ceiling
    risk_per_trade_pct: float  # % of capital lost if the stop is hit
    max_position_pct: float  # largest position as % of capital
    daily_loss_limit_pct: float  # stop opening trades for the day after losing this much
    max_drawdown_from_peak_pct: float  # halt the bot entirely after falling this far from the peak
    sizing_capital_cap: float | None = None  # size trades as if the account held at most this much


def select_tier(tiers: list[Tier], peak_equity: float) -> Tier:
    """Tiers ratchet on peak equity, so a dip never moves the bot back to a riskier tier."""
    for tier in tiers:
        if tier.up_to_peak_equity is None or peak_equity < tier.up_to_peak_equity:
            return tier
    return tiers[-1]


def position_size(plan: TradePlan, equity: float, buying_power: float, tier: Tier) -> int:
    capital = min(equity, tier.sizing_capital_cap) if tier.sizing_capital_cap else equity
    risk_dollars = capital * tier.risk_per_trade_pct / 100
    qty_by_risk = math.floor(risk_dollars / plan.risk_per_share)
    max_notional = min(buying_power, capital * tier.max_position_pct / 100)
    qty_by_cash = math.floor(max_notional / plan.entry)
    return max(0, min(qty_by_risk, qty_by_cash))


def drawdown_breached(equity: float, peak_equity: float, tier: Tier) -> bool:
    return equity <= peak_equity * (1 - tier.max_drawdown_from_peak_pct / 100)


def daily_loss_breached(equity: float, start_of_day_equity: float, tier: Tier) -> bool:
    return equity <= start_of_day_equity * (1 - tier.daily_loss_limit_pct / 100)
