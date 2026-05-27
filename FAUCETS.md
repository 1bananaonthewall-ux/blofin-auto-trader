# Crypto faucets and collecting

## What a faucet is

A **crypto faucet** drips small amounts of coin or tokens when you:

- Solve a captcha or click "claim"
- Watch ads / complete offers
- Play a mini-game or survey
- (Developers only) Request **testnet** funds for building apps

Most sites hold balance in an **internal account** until you hit a **minimum withdrawal**, then send to your on-chain wallet.

## Three kinds (only one funds Blofin)

| Type | Value | Can send to Blofin? |
|------|--------|---------------------|
| **Testnet faucet** (Sepolia, Holesky, etc.) | $0 — play money for devs | No |
| **Mainnet micro-faucet** (FreeBitco, Cointiply, etc.) | Tiny real payouts | Only after withdraw to **your** wallet |
| **Exchange demo** (Blofin demo 50k USDT) | Paper trading | No — not withdrawable |

Your treasury loop only sees money after it lands as **real USDT/BTC/ETH** in `treasury/wallets.json`.

## Realistic economics

- Typical mainnet faucet: **$0.001 – $0.10 per claim**, often once per hour/day.
- Minimum withdraw: often **$3 – $50** equivalent — can take **weeks or months** of daily claims.
- **$100 from faucets alone** usually means many sites × long time, or it never adds up after fees.
- Gas can eat small withdrawals (withdrawing $5 of ETH on Ethereum mainnet can cost more than $5).

**Faucets are a slow side channel**, not a reliable path to $100 batches. They work best as:

1. Extra dust that accumulates across many sites
2. Combined with swaps → USDT → your Blofin deposit address
3. Picked up automatically by `treasury_loop.py` once on-chain

## Safe collecting habits

- Use a **dedicated wallet** per risk level (don't mix with large holdings).
- Avoid sites that ask for **seed phrases** or "validation" payments — scams.
- Prefer faucets that pay on **cheap chains** (Tron TRC20, BSC, Polygon) for withdrawals.
- Track **claim timers** so you don't miss daily rolls (see `faucet_tracker.py` below).
- Expect **KYC / anti-bot** on anything that pays real money at scale.

## Pipeline into Blofin ($100 loop)

```
[Faucet sites]  --manual claim-->  [Internal balance]
        |
        v (min withdraw)
[Your on-chain wallets]  --treasury scan-->
[Convert stables to USDT if needed]  --$100 batch-->
[Blofin deposit address]  --detect-->
[Funding -> Futures]  --bot trades-->
```

1. Register faucets with payout address = wallet id from `treasury/wallets.json`
2. Run `faucet_tracker.py` to see what is due to claim
3. Run `treasury_loop.py` (or `run_treasury.ps1`) to sweep when ≥ $100 USDT/USDC on-chain

## What we do / don't automate

| Automated | Manual (you) |
|-----------|----------------|
| Remind when a faucet claim is due | Captcha, login, roll button |
| Scan on-chain balances | Withdraw from faucet internal wallet |
| Batch $100 to Blofin deposit (when configured) | Pick legitimate faucets |
| Move new Blofin deposits to futures | Swap altcoins to USDT |

We do **not** auto-click captchas or run multi-account "farming" — that breaks most terms of service and triggers bans.
