"""Unattended Opening Range Breakout day-trading bot for Alpaca.

Every entry is a bracket order: the stop-loss and profit target live on
Alpaca's servers, so they trigger even if this bot (or your internet) is down.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest

from report import DailySummary, Fill, append_csv, results_from_fills
from risk import Tier, daily_loss_breached, drawdown_breached, position_size, select_tier
from strategy import Bar, StrategyConfig, TradePlan, find_breakout

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", HERE / "data"))

log = logging.getLogger("bot")


@dataclass
class Config:
    watchlist: list[str]
    strategy: StrategyConfig
    tiers: list[Tier]
    entry_cutoff: str
    flatten_at: str
    max_open_positions: int
    max_trades_per_day: int
    pdt_guard: bool
    poll_seconds: int
    notify_webhook_url: str

    @classmethod
    def load(cls, path: Path) -> Config:
        raw = yaml.safe_load(path.read_text())
        return cls(
            watchlist=[s.upper() for s in raw["watchlist"]],
            strategy=StrategyConfig(**raw["strategy"]),
            tiers=[Tier(**t) for t in raw["tiers"]],
            entry_cutoff=raw["schedule"]["entry_cutoff"],
            flatten_at=raw["schedule"]["flatten_at"],
            max_open_positions=raw["limits"]["max_open_positions"],
            max_trades_per_day=raw["limits"]["max_trades_per_day"],
            pdt_guard=raw["limits"]["pdt_guard"],
            poll_seconds=raw["limits"]["poll_seconds"],
            notify_webhook_url=os.environ.get("NOTIFY_WEBHOOK_URL", raw.get("notify_webhook_url") or ""),
        )


@dataclass
class State:
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    tier: str = ""
    day: str = ""
    start_of_day_equity: float = 0.0
    stopped_for_day: bool = False
    traded_today: list[str] = field(default_factory=list)
    summary_day: str = ""

    @classmethod
    def load(cls, path: Path) -> State:
        return cls(**json.loads(path.read_text())) if path.exists() else cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))


class Bot:
    def __init__(self, cfg: Config, trading: TradingClient, data: StockHistoricalDataClient, state_path: Path):
        self.cfg = cfg
        self.trading = trading
        self.data = data
        self.state_path = state_path
        self.state = State.load(state_path)

    def notify(self, message: str) -> None:
        log.info(message)
        if self.cfg.notify_webhook_url:
            try:
                requests.post(self.cfg.notify_webhook_url, json={"message": message}, timeout=5)
            except requests.RequestException as e:
                log.warning("notification failed: %s", e)

    def run(self) -> None:
        mode = "PAPER" if _paper() else "LIVE"
        self.notify(f"Bot started ({mode}). Watchlist: {', '.join(self.cfg.watchlist)}")
        while True:
            try:
                wait = self.tick()
            except Exception:
                log.exception("tick failed")
                wait = self.cfg.poll_seconds
            time.sleep(wait)

    def tick(self) -> float:
        """One pass of the loop. Returns seconds to sleep before the next pass."""
        clock = self.trading.get_clock()
        if not clock.is_open:
            until_open = (clock.next_open - clock.timestamp).total_seconds()
            return max(self.cfg.poll_seconds, min(until_open - 60, 900))

        now = clock.timestamp.astimezone(ET)
        session_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        # Half days close early, so never flatten later than 5 minutes before the real close.
        flatten_time = min(_at(now, self.cfg.flatten_at), clock.next_close.astimezone(ET) - timedelta(minutes=5))

        account = self.trading.get_account()
        equity = float(account.equity)
        s = self.state

        if s.day != now.date().isoformat():
            s.day = now.date().isoformat()
            s.start_of_day_equity = float(account.last_equity)
            s.stopped_for_day = False
            s.traded_today = []
        s.peak_equity = max(s.peak_equity, equity)

        tier = select_tier(self.cfg.tiers, s.peak_equity)
        if tier.name != s.tier:
            if s.tier:
                self.notify(f"Peak equity ${s.peak_equity:,.0f}: switching risk tier {s.tier} -> {tier.name}")
            s.tier = tier.name
        self.state.save(self.state_path)

        if not s.halted and drawdown_breached(equity, s.peak_equity, tier):
            s.halted = True
            s.halt_reason = (f"equity ${equity:,.2f} is {tier.max_drawdown_from_peak_pct}% below "
                             f"peak ${s.peak_equity:,.2f}")
            self.state.save(self.state_path)
            self.trading.close_all_positions(cancel_orders=True)
            self.notify(f"HALTED: {s.halt_reason}. Closed everything. See README to resume.")
            return self.cfg.poll_seconds

        if now >= flatten_time:
            if self.trading.get_all_positions() or self.trading.get_orders():
                self.trading.close_all_positions(cancel_orders=True)
                self.notify(f"End of day: closed all positions. Equity ${equity:,.2f}")
            elif s.summary_day != s.day:
                # Sent on the first pass after everything is closed, so the closing fills are counted.
                self.send_daily_summary(session_open, equity, tier)
            return self.cfg.poll_seconds

        if s.halted:
            return self.cfg.poll_seconds

        if not s.stopped_for_day and daily_loss_breached(equity, s.start_of_day_equity, tier):
            s.stopped_for_day = True
            self.state.save(self.state_path)
            self.notify(f"Daily loss limit ({tier.daily_loss_limit_pct}%) hit. No new trades today.")
        if s.stopped_for_day:
            return self.cfg.poll_seconds

        range_ready = session_open + timedelta(minutes=self.cfg.strategy.opening_range_minutes)
        if not (range_ready <= now < _at(now, self.cfg.entry_cutoff)):
            return self.cfg.poll_seconds

        self.look_for_entries(now, session_open, account, tier)
        return self.cfg.poll_seconds

    def look_for_entries(self, now: datetime, session_open: datetime, account, tier: Tier) -> None:
        s = self.state
        held = {p.symbol for p in self.trading.get_all_positions()}
        open_slots = self.cfg.max_open_positions - len(held)
        trades_left = self.cfg.max_trades_per_day - len(s.traded_today)

        equity = float(account.equity)
        if self.cfg.pdt_guard and equity < 25_000:
            # Under $25k, a 4th day trade in 5 business days can freeze a margin account.
            # Every position held now will be closed today, so count it as a day trade too.
            trades_left = min(trades_left, 3 - int(account.daytrade_count) - len(held))

        if open_slots <= 0 or trades_left <= 0:
            return

        candidates = [sym for sym in self.cfg.watchlist if sym not in held and sym not in s.traded_today]
        if not candidates:
            return

        bars_by_symbol = self.fetch_bars(candidates, session_open, now)
        for sym in candidates:
            plan = find_breakout(sym, bars_by_symbol.get(sym, []), session_open, self.cfg.strategy)
            if plan is None:
                continue
            qty = position_size(plan, equity, float(account.buying_power), tier)
            if qty < 1:
                log.info("%s breakout but position size is 0 (stop too wide for account)", sym)
                continue
            self.submit(plan, qty)
            s.traded_today.append(sym)
            self.state.save(self.state_path)
            open_slots -= 1
            trades_left -= 1
            if open_slots <= 0 or trades_left <= 0:
                return

    def send_daily_summary(self, session_open: datetime, equity: float, tier: Tier) -> None:
        s = self.state
        orders = self.trading.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, after=session_open, limit=500, nested=False))
        fills = [Fill(o.symbol, o.side.value, float(o.filled_qty), float(o.filled_avg_price))
                 for o in orders if o.filled_avg_price and float(o.filled_qty or 0) > 0]
        summary = DailySummary(
            day=s.day,
            mode="PAPER" if _paper() else "LIVE",
            start_equity=s.start_of_day_equity,
            end_equity=equity,
            peak_equity=s.peak_equity,
            tier=tier.name,
            next_milestone=tier.up_to_peak_equity,
            results=results_from_fills(fills),
            halted=s.halted,
            stopped_for_day=s.stopped_for_day,
        )
        self.notify(summary.message())
        append_csv(self.state_path.parent / "daily_summaries.csv", summary)
        s.summary_day = s.day
        self.state.save(self.state_path)

    def fetch_bars(self, symbols: list[str], session_open: datetime, now: datetime) -> dict[str, list[Bar]]:
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
                               start=session_open, feed=DataFeed.IEX)
        out: dict[str, list[Bar]] = {}
        for sym, bars in self.data.get_stock_bars(req).data.items():
            # Only use bars that have fully closed.
            done = [b for b in bars if b.timestamp + timedelta(minutes=1) <= now]
            out[sym] = [Bar(b.timestamp.astimezone(ET), b.open, b.high, b.low, b.close, b.volume) for b in done]
        return out

    def submit(self, plan: TradePlan, qty: int) -> None:
        # GTC so the stop and target stay active overnight if the end-of-day close ever fails.
        order = MarketOrderRequest(
            symbol=plan.symbol,
            qty=qty,
            side=OrderSide.BUY if plan.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=plan.target),
            stop_loss=StopLossRequest(stop_price=plan.stop),
        )
        self.trading.submit_order(order)
        self.notify(f"{'LONG' if plan.side == 'buy' else 'SHORT'} {qty} {plan.symbol} @ ~{plan.entry} "
                    f"stop {plan.stop} target {plan.target} (risk ${qty * plan.risk_per_share:,.2f})")


def _at(now: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _paper() -> bool:
    return os.environ.get("ALPACA_PAPER", "true").lower() != "false"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(DATA_DIR / "bot.log")],
    )
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (see .env.example)")

    cfg = Config.load(Path(os.environ.get("BOT_CONFIG", HERE / "config.yaml")))
    trading = TradingClient(key, secret, paper=_paper())
    data = StockHistoricalDataClient(key, secret)
    Bot(cfg, trading, data, DATA_DIR / "state.json").run()


if __name__ == "__main__":
    main()
