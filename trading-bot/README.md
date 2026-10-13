# Trading bot

An unattended day-trading bot for [Alpaca](https://alpaca.markets) (commission-free US stocks/ETFs, has an API).

**What it does every trading day**

1. 9:30–9:45 ET: records the high and low of the first 15 minutes (the "opening range") for each symbol on the watchlist.
2. 9:45–11:30 ET: when a symbol closes a 1-minute bar above that range, it buys.
   Every entry is a **bracket order**: the stop-loss (bottom of the range) and profit target (2× the risk)
   are placed **on Alpaca's servers at the same moment**. They trigger even if the bot, your computer or
   your internet is down.
3. 3:55 PM ET (earlier on half days): closes anything still open. Nothing is held overnight.

**Protections that would have saved most of the $47K**

| Guard | Aggressive tier default |
|---|---|
| Max loss per trade (stop always attached) | 10% of account |
| Daily loss limit: no new trades for the rest of the day | -25% from yesterday's close |
| Kill switch: close everything and stop the bot | -40% from all-time peak (peak $47K → stops at $28.2K) |
| Day-trade count guard while under $25K | 3 per 5 business days |

**Risk tiers** ratchet on your peak equity and never move back to a riskier tier:

| Tier | Peak equity | Risk/trade | Sizes trades as if account is |
|---|---|---|---|
| aggressive | $0 → $100K | 10% | full account |
| steady | $100K → $600K | 2% | $50K (the rest just sits there) |
| preserve | $600K+ | 1% | $50K |

All numbers live in `config.yaml`. Change them there, not in the code.

## Setup

1. **Open an Alpaca account** at <https://app.alpaca.markets>. Every account also gets a paper (fake money) account.
2. **Generate API keys** for the paper account (dashboard → API Keys).
3. `cp .env.example .env` and paste the keys in. Keep `ALPACA_PAPER=true` for now.
4. **Run it** on something that's always on. Your Home Assistant box, a Raspberry Pi or any Linux machine with Docker works:
   ```bash
   docker compose up -d --build
   docker compose logs -f        # watch it
   ```
   Without Docker: `pip install -r requirements.txt && python bot.py`.
5. **Let it run on paper for at least 2 weeks.** Check `data/bot.log` and the Alpaca paper dashboard.
6. **Go live**: fund the live account, create live API keys, put them in `.env`, set `ALPACA_PAPER=false`,
   then `docker compose up -d`.

## Phone notifications (optional, via Home Assistant)

Create an automation with a **Webhook** trigger (ID `trading_bot`) and a notify action using
`{{ trigger.json.message }}`. Then set `NOTIFY_WEBHOOK_URL=http://homeassistant.local:8123/api/webhook/trading_bot`
in `.env`. You'll get a push for every trade, the end-of-day close, a daily-limit stop or a halt.

## If the kill switch fires

The bot closes everything and stays stopped, even across restarts. To resume after you've looked at what happened:

```bash
docker compose stop
# edit data/state.json: set "halted": false and "peak_equity" to your current equity
docker compose start
```

## Things the bot can't do

- **Withdraw profits.** When you hit a milestone, transfer money out in the Alpaca app yourself.
  The steady tier already only trades $50K of it.
- **Guarantee a stop price.** A stop becomes a market order when hit. If a stock gaps hard, you can get filled worse than the stop.
- **Know whether the strategy works.** Opening-range breakout is a well-known, simple strategy, not a secret edge.
  Expect plenty of losing days. Paper trading is how you find out before it costs real money.

## Before going live

- **This repository is public** (it's your GitHub profile repo). `.env` is git-ignored, so never commit your keys.
  Consider moving this folder to a private repo.
- In the US, margin accounts under $25K have been limited to 3 day trades per 5 business days.
  FINRA has been revising that rule. If your broker no longer enforces it, set `pdt_guard: false`.
- Shorting is off by default. It needs a margin account with $2,000 or more.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest
```
