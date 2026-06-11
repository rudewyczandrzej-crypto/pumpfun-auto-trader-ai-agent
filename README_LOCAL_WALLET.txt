# PumpFun Auto Trader AI — Local Wallet version

This version uses PumpPortal Local Trading API:
- POST https://pumpportal.fun/api/trade-local
- receives unsigned serialized transaction
- signs it locally with SOLANA_PRIVATE_KEY
- sends it via SOLANA_RPC_URL

Important:
- Do NOT use PUMPPORTAL_API_KEY here.
- Do NOT use seed phrase.
- Use only private key exported from a burner wallet.
- Store SOLANA_PRIVATE_KEY only in Railway Variables.
- Never put private key in GitHub or Telegram.

Railway variables for local live:

TRADING_MODE=live
TRADING_CONFIRM=YES_I_UNDERSTAND
SOLANA_PRIVATE_KEY=your_private_key
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
PUMPPORTAL_TRADE_LOCAL_URL=https://pumpportal.fun/api/trade-local

Safer first test:

TRADING_MODE=paper

New command:
/wallet
