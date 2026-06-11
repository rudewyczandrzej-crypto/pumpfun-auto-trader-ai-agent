# PumpFun Auto Trader AI

Experimental Pump.fun auto-trading agent.

Modes:
- watch: alerts only
- paper: fake buy/sell tracking
- live: real tiny trades via PumpPortal Local Trading API

Live requires BOTH:
```env
TRADING_MODE=live
TRADING_CONFIRM=YES_I_UNDERSTAND
SOLANA_PRIVATE_KEY=...
```

There is no per-trade Telegram confirmation in live mode.
The confirmation is a one-time Railway variable safety lock.

## Commands

```text
/start
/source_add name rss_url
/sources
/source_delete ID
/scan_x
/watch_mint CA
/open_trades
/close_all
/stats
/trading_status
/alerts_on
/alerts_off
/help
```

## Setup

1. Run `database.sql` in Supabase.
2. Upload files to GitHub.
3. Deploy on Railway.
4. Add variables from `.env.example`.

## Recommended first test

Start with:

```env
TRADING_MODE=paper
TRADE_AMOUNT_SOL=0.005
MAX_TRADES_PER_HOUR=2
MAX_DAILY_TRADES=5
MAX_OPEN_TRADES=2
```

After you confirm paper behavior, for real tiny trades:

```env
TRADING_MODE=live
TRADING_CONFIRM=YES_I_UNDERSTAND
SOLANA_PRIVATE_KEY=your_key
```

## Important

Use only a burner wallet / API key with tiny funds.
No main wallet.
No financial advice.
Pump.fun tokens are extremely risky.


## Extra safety commands

- `/close_all` — manually closes all open trades for your Telegram user. In live mode it tries to sell 100%. In paper/watch it closes as paper.
- `/stats` — shows rough test stats.


## Local wallet version

This version does not use PumpPortal Lightning API key.

Live trading requires:

```env
TRADING_MODE=live
TRADING_CONFIRM=YES_I_UNDERSTAND
SOLANA_PRIVATE_KEY=your_private_key
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
PUMPPORTAL_TRADE_LOCAL_URL=https://pumpportal.fun/api/trade-local
```

Use only a burner wallet with tiny funds.

Do not paste seed phrase or private key into Telegram/GitHub.
Store the private key only in Railway Variables.

Useful command:

```text
/wallet
```

It shows only public key and RPC. It never prints private key.
