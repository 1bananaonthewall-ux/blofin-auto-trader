# Autonomous hustles (zero touch from you)

## What I cannot do — ever

- Create money with no accounts, no capital, and no platform access
- Open bank/exchange/email accounts in your name
- Pass KYC, captchas, or phone verification for you
- Cold-call or email strangers without your business identity and compliance setup
- Guarantee profit on live trading (you have ~$40 live; bot can **lose** it)

## What runs without you (using existing `.env` only)

| Hustle | Runs | Needs you? | Pays? |
|--------|------|------------|-------|
| **Live trading bot** (`bot.py`) | 24/7 | No | Only if markets cooperate — **risk of loss** |
| **Treasury daemon** | Every 5m | No* | When USDT hits your wallets / Blofin |
| **Deposit → futures** | On new deposit | No | Moves credited USDT to futures |
| **Equity / PnL logging** | Every cycle | No | Data only |

\* Treasury sweeps need `BLOFIN_USDT_DEPOSIT_ADDRESS` + real wallet addresses to do outbound $100 batches.

## Not started (requires you once)

- Blofin Rewards / Task Center (browser login)
- Faucet claims (captcha)
- New exchange accounts, API keys, bank links
- Real-estate wholesaling (human contracts)

## Start / stop

```powershell
.\run_autonomous.ps1          # bot + hustle daemon
.\run_autonomous.ps1 -Stop    # stop both
```

Logs: `logs/hustle_daemon.log`, `logs/bot.log`, `state/hustle_report.jsonl`
