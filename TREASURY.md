# Treasury consolidator ($100 loop)

Scans **all configured wallets**, totals USD value, and when stablecoin holdings reach **$100**, plans a sweep to your **Blofin USDT deposit address**. Repeats forever.

Also watches Blofin **deposit history** and moves credited USDT from **funding → futures** (if enabled).

## Setup

1. Copy wallet registry:

```powershell
Copy-Item treasury\wallets.example.json treasury\wallets.json
```

Edit `treasury/wallets.json` with your real addresses (EVM, Tron, BTC, Solana, …).

2. Add to `.env`:

```env
# $100 batch target (change if you want)
TREASURY_SWEEP_USD=100
TREASURY_POLL_SECONDS=300
TREASURY_DRY_RUN=true

# From Blofin UI: Assets → Deposit → USDT → TRC20 (API cannot fetch this)
BLOFIN_USDT_DEPOSIT_ADDRESS=YourDepositAddressHere
BLOFIN_DEPOSIT_CHAIN=TRC20

# After deposits land in funding, push to futures for the trading bot
TREASURY_AUTO_FUTURES_TRANSFER=true

# Optional — only when you enable live on-chain sends (not required for scan/monitor)
# TREASURY_EVM_PRIVATE_KEY=
# TREASURY_TRON_PRIVATE_KEY=
```

3. Blofin API key needs **READ** + **TRANSFER** (for funding→futures). On-chain sends need keys only when you wire live sweeps.

## Run

```powershell
.\run_treasury.ps1
```

One-shot test:

```powershell
.\.venv\Scripts\python treasury_loop.py --once
```

Logs: `logs/treasury.log` · State: `treasury/state/treasury.json`

## What each cycle does

1. **Scan** every wallet in `wallets.json` (balances via public RPC/APIs).
2. **Sum** USD (stables = $1; others via CoinGecko).
3. If **USDT/USDC total ≥ $100**, build a sweep plan (dry-run logs sources until live keys exist).
4. **Poll** Blofin deposit history; on new completed deposits, **transfer** to futures.
5. **Sleep** `TREASURY_POLL_SECONDS` and repeat.

## Limits (important)

| Topic | Reality |
|--------|---------|
| **Faucets / demo funds** | Cannot be consolidated or deposited as real USDT. |
| **Non-stable coins** | v1 sweeps **USDT/USDC only**. Swap-to-USDT per chain is a separate step (DEX/CEX). |
| **Deposit address** | Blofin API does **not** expose it — paste from the website. |
| **Live sends** | Default is **DRY_RUN**. On-chain transfer code is stubbed until you add signing keys. |
| **Fees** | Network fees + Blofin min deposit (~0.01 USDT on TRC20) eat into small batches. |

## Growing to endless $100 cycles

You need **real USDT/USDC** arriving in configured wallets (deposits, earnings, manual transfers). The loop handles **detection, batching, and Blofin crediting** — not creating money from faucets.

## Crypto faucets

See **[FAUCETS.md](FAUCETS.md)** for how faucet collecting works and realistic payouts.

```powershell
Copy-Item treasury\faucets.example.json treasury\faucets.json
# Add real sites, set enabled: true, link payout_wallet_id to wallets.json

.\.venv\Scripts\python faucet_tracker.py              # what to claim now
.\.venv\Scripts\python faucet_tracker.py --project      # rough $/month estimate
.\.venv\Scripts\python faucet_tracker.py --claimed my-faucet-id
```
