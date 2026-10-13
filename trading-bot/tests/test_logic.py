from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from risk import Tier, daily_loss_breached, drawdown_breached, position_size, select_tier
from strategy import Bar, StrategyConfig, TradePlan, find_breakout, opening_range

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 13, 9, 30, tzinfo=ET)

AGGRESSIVE = Tier("aggressive", 100_000, 10, 100, 25, 40)
STEADY = Tier("steady", 600_000, 2, 50, 6, 15, 50_000)
PRESERVE = Tier("preserve", None, 1, 25, 3, 10, 50_000)
TIERS = [AGGRESSIVE, STEADY, PRESERVE]


def bars(closes, high_pad=0.5, low_pad=0.5):
    return [Bar(OPEN + timedelta(minutes=i), c, c + high_pad, c - low_pad, c, 1000) for i, c in enumerate(closes)]


def test_opening_range_waits_for_window_to_close():
    assert opening_range(bars([100] * 15), OPEN, 15) is None
    rng = opening_range(bars([100] * 15 + [100]), OPEN, 15)
    assert (rng.high, rng.low) == (100.5, 99.5)


def test_long_breakout_plan():
    history = bars([100] * 15 + [100.7])
    plan = find_breakout("TQQQ", history, OPEN, StrategyConfig())
    assert plan == TradePlan("TQQQ", "buy", entry=100.7, stop=99.5, target=103.1)


def test_no_chasing_extended_moves():
    history = bars([100] * 15 + [102])
    assert find_breakout("TQQQ", history, OPEN, StrategyConfig()) is None


def test_shorts_only_when_enabled():
    history = bars([100] * 15 + [99.3])
    assert find_breakout("TQQQ", history, OPEN, StrategyConfig()) is None
    plan = find_breakout("TQQQ", history, OPEN, StrategyConfig(allow_shorts=True))
    assert plan.side == "sell" and plan.stop == 100.5 and plan.target < plan.entry


def test_range_too_tight_is_skipped():
    history = bars([100] * 15 + [100.2], high_pad=0.01, low_pad=0.01)
    assert find_breakout("TQQQ", history, OPEN, StrategyConfig()) is None


def test_size_limited_by_risk():
    plan = TradePlan("X", "buy", entry=50, stop=45, target=60)
    # $10k * 10% = $1,000 risk / $5 per share = 200 shares, cash allows 200
    assert position_size(plan, equity=10_000, buying_power=10_000, tier=AGGRESSIVE) == 200


def test_size_limited_by_cash():
    plan = TradePlan("X", "buy", entry=100, stop=99.5, target=101)
    # risk allows 100 shares, but $500 only buys 5
    assert position_size(plan, equity=500, buying_power=500, tier=AGGRESSIVE) == 5


def test_steady_tier_sizes_off_50k_cap():
    plan = TradePlan("X", "buy", entry=100, stop=98, target=104)
    # capital capped at $50k: 2% = $1,000 / $2 = 500 shares; 50% of $50k = $25k / $100 = 250
    assert position_size(plan, equity=150_000, buying_power=300_000, tier=STEADY) == 250


def test_tiers_ratchet_on_peak():
    assert select_tier(TIERS, 500) is AGGRESSIVE
    assert select_tier(TIERS, 100_000) is STEADY
    assert select_tier(TIERS, 600_000) is PRESERVE


def test_kill_switches():
    assert not drawdown_breached(30_000, 47_000, AGGRESSIVE)
    assert drawdown_breached(28_200, 47_000, AGGRESSIVE)
    assert daily_loss_breached(750, 1_000, AGGRESSIVE)
    assert not daily_loss_breached(760, 1_000, AGGRESSIVE)


# --- Bot loop against a fake broker ---------------------------------------
from types import SimpleNamespace
from unittest.mock import MagicMock

from bot import Bot, Config


def make_bot(tmp_path, now, equity=500.0, positions=(), last_equity=None):
    cfg = Config.load(__import__("pathlib").Path(__file__).parent.parent / "config.yaml")
    cfg.watchlist = ["TQQQ"]
    trading = MagicMock()
    trading.get_clock.return_value = SimpleNamespace(
        is_open=True, timestamp=now, next_open=now + timedelta(days=1),
        next_close=now.replace(hour=16, minute=0))
    trading.get_account.return_value = SimpleNamespace(
        equity=str(equity), last_equity=str(last_equity or equity), buying_power=str(equity), daytrade_count="0")
    trading.get_all_positions.return_value = [SimpleNamespace(symbol=s) for s in positions]
    trading.get_orders.return_value = []
    raw = [SimpleNamespace(timestamp=b.timestamp, open=b.open, high=b.high, low=b.low, close=b.close,
                           volume=b.volume) for b in bars([30] * 15 + [30.6])]
    data = MagicMock()
    data.get_stock_bars.return_value = SimpleNamespace(data={"TQQQ": raw})
    return Bot(cfg, trading, data, tmp_path / "state.json"), trading


def test_bot_enters_with_bracket_order(tmp_path):
    bot, trading = make_bot(tmp_path, OPEN + timedelta(minutes=17))
    bot.tick()
    order = trading.submit_order.call_args.args[0]
    assert order.symbol == "TQQQ" and order.order_class == "bracket"
    assert order.stop_loss.stop_price == 29.5 and order.take_profit.limit_price == 32.8
    assert order.qty == 16  # $500 cash / $30.60 (risk alone would allow 45)
    bot.tick()  # same symbol is not traded twice in a day
    assert trading.submit_order.call_count == 1


def test_bot_flattens_before_close(tmp_path):
    bot, trading = make_bot(tmp_path, OPEN.replace(hour=15, minute=56), positions=["TQQQ"])
    bot.tick()
    trading.close_all_positions.assert_called_once_with(cancel_orders=True)
    trading.submit_order.assert_not_called()


def test_bot_halts_on_drawdown_from_peak(tmp_path):
    bot, trading = make_bot(tmp_path, OPEN + timedelta(minutes=17), equity=28_000)
    bot.state.peak_equity = 47_000
    bot.tick()
    assert bot.state.halted
    trading.close_all_positions.assert_called_once_with(cancel_orders=True)
    bot.tick()
    trading.submit_order.assert_not_called()


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    bot, trading = make_bot(tmp_path, OPEN + timedelta(minutes=17), equity=370, last_equity=500)
    bot.tick()
    assert bot.state.stopped_for_day
    trading.submit_order.assert_not_called()


# --- Daily summary ----------------------------------------------------------
from report import DailySummary, Fill, SymbolResult, results_from_fills


def test_results_from_fills():
    fills = [Fill("TQQQ", "buy", 10, 30.0), Fill("TQQQ", "sell", 10, 32.0),
             Fill("NVDA", "buy", 2, 100.0), Fill("NVDA", "sell", 2, 98.5),
             Fill("AMD", "buy", 5, 150.0)]
    assert results_from_fills(fills) == [
        SymbolResult("TQQQ", 20.0, False), SymbolResult("NVDA", -3.0, False), SymbolResult("AMD", -750.0, True)]


def test_summary_message():
    summary = DailySummary("2026-10-13", "PAPER", 500, 517, 520, "aggressive", 100_000,
                           [SymbolResult("TQQQ", 20.0, False), SymbolResult("NVDA", -3.0, False)])
    msg = summary.message()
    assert "Balance: $517.00 (+$17.00, +3.4%)" in msg
    assert "Trades: 2 | Wins: 1 | Losses: 1" in msg
    assert "TQQQ: +$20.00" in msg and "NVDA: -$3.00" in msg
    assert "Next milestone $100,000: 0.5% there" in msg


def test_bot_sends_one_summary_after_close(tmp_path):
    bot, trading = make_bot(tmp_path, OPEN.replace(hour=15, minute=56), equity=520, last_equity=500)
    trading.get_orders.side_effect = lambda req=None: [] if req is None else [
        SimpleNamespace(symbol="TQQQ", side=SimpleNamespace(value="buy"), filled_qty="16", filled_avg_price="30.6"),
        SimpleNamespace(symbol="TQQQ", side=SimpleNamespace(value="sell"), filled_qty="16", filled_avg_price="31.85"),
    ]
    sent = []
    bot.notify = sent.append
    bot.tick()
    bot.tick()
    assert len(sent) == 1
    assert "TQQQ: +$20.00" in sent[0] and "+$20.00, +4.0%" in sent[0]
    assert (tmp_path / "daily_summaries.csv").read_text().count("\n") == 2  # header + one day


def test_summary_waits_until_positions_are_closed(tmp_path):
    bot, trading = make_bot(tmp_path, OPEN.replace(hour=15, minute=56), positions=["TQQQ"])
    sent = []
    bot.notify = sent.append
    bot.tick()
    assert sent == ["End of day: closed all positions. Equity $500.00"]
