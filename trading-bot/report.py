"""End-of-day summary. Pure logic, no broker calls."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

FINAL_GOAL = 600_000


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str  # "buy" or "sell"
    qty: float
    price: float


@dataclass(frozen=True)
class SymbolResult:
    symbol: str
    pnl: float
    still_open: bool


@dataclass(frozen=True)
class DailySummary:
    day: str
    mode: str
    start_equity: float
    end_equity: float
    peak_equity: float
    tier: str
    next_milestone: float | None
    results: list[SymbolResult]
    halted: bool = False
    stopped_for_day: bool = False

    @property
    def change(self) -> float:
        return self.end_equity - self.start_equity

    @property
    def change_pct(self) -> float:
        return self.change / self.start_equity * 100 if self.start_equity else 0.0

    @property
    def wins(self) -> int:
        return sum(1 for r in self.results if not r.still_open and r.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for r in self.results if not r.still_open and r.pnl <= 0)

    def message(self) -> str:
        sign = "+" if self.change >= 0 else "-"
        lines = [
            f"Daily summary {self.day} ({self.mode})",
            f"Balance: ${self.end_equity:,.2f} ({sign}${abs(self.change):,.2f}, {sign}{abs(self.change_pct):.1f}%)",
        ]
        if self.results:
            lines.append(f"Trades: {len(self.results)} | Wins: {self.wins} | Losses: {self.losses}")
            for r in self.results:
                status = "STILL OPEN" if r.still_open else f"{'+' if r.pnl >= 0 else '-'}${abs(r.pnl):,.2f}"
                lines.append(f"  {r.symbol}: {status}")
        else:
            lines.append("Trades: none (no breakout met the rules)")
        if self.halted:
            lines.append("STATUS: HALTED by kill switch. Bot will not trade until you reset it.")
        elif self.stopped_for_day:
            lines.append("Status: daily loss limit hit, stopped early.")
        drawdown = (1 - self.end_equity / self.peak_equity) * 100 if self.peak_equity else 0.0
        lines.append(f"Tier: {self.tier} | Peak: ${self.peak_equity:,.2f} | Below peak: {drawdown:.1f}%")
        if self.next_milestone:
            lines.append(f"Next milestone ${self.next_milestone:,.0f}: {self.end_equity / self.next_milestone * 100:.1f}% there")
        lines.append(f"House goal ${FINAL_GOAL:,.0f}: {self.end_equity / FINAL_GOAL * 100:.2f}% there")
        return "\n".join(lines)


def results_from_fills(fills: list[Fill]) -> list[SymbolResult]:
    """Realized P&L per symbol: money in from sells minus money out on buys."""
    cash: dict[str, float] = {}
    shares: dict[str, float] = {}
    for f in fills:
        sign = 1 if f.side == "sell" else -1
        cash[f.symbol] = cash.get(f.symbol, 0.0) + sign * f.qty * f.price
        shares[f.symbol] = shares.get(f.symbol, 0.0) - sign * f.qty
    return [SymbolResult(sym, round(cash[sym], 2), abs(shares[sym]) > 1e-9) for sym in cash]


def append_csv(path: Path, summary: DailySummary) -> None:
    """One row per day, so results can be reviewed in a spreadsheet later."""
    new_file = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["date", "mode", "start_equity", "end_equity", "change", "change_pct",
                        "trades", "wins", "losses", "tier", "peak_equity", "symbols"])
        w.writerow([summary.day, summary.mode, f"{summary.start_equity:.2f}", f"{summary.end_equity:.2f}",
                    f"{summary.change:.2f}", f"{summary.change_pct:.2f}", len(summary.results),
                    summary.wins, summary.losses, summary.tier, f"{summary.peak_equity:.2f}",
                    " ".join(f"{r.symbol}:{r.pnl:+.2f}" for r in summary.results)])
