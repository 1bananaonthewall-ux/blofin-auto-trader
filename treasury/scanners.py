from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from treasury.pricer import usd_price
from treasury.wallets import WalletConfig

log = logging.getLogger(__name__)

# Common USDT contract addresses per EVM chain
EVM_USDT: dict[str, str] = {
    "ethereum": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "polygon": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    "bsc": "0x55d398326f99059fF775485246999027B3197955",
    "arbitrum": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    "base": "0xfde4C96c859adBE2028595c0416408a07D3B4F3d",
    "optimism": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
    "avalanche": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
}

EVM_USDC: dict[str, str] = {
    "ethereum": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "polygon": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "arbitrum": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@dataclass(frozen=True)
class Holding:
    wallet_id: str
    chain: str
    symbol: str
    amount: float
    usd_value: float


def scan_wallet(wallet: WalletConfig) -> list[Holding]:
    t = wallet.type
    if t == "evm":
        return _scan_evm(wallet)
    if t == "tron":
        return _scan_tron(wallet)
    if t == "bitcoin":
        return _scan_btc(wallet)
    if t == "solana":
        return _scan_sol(wallet)
    log.warning("unsupported wallet type %s (%s)", t, wallet.id)
    return []


def _scan_evm(wallet: WalletConfig) -> list[Holding]:
    if not wallet.rpc_url or not wallet.address:
        return []
    holdings: list[Holding] = []
    chain = wallet.chain or "ethereum"
    for sym in wallet.tokens or ("USDT", "ETH"):
        sym = sym.upper()
        if sym == "USDT":
            contract = EVM_USDT.get(chain)
            if contract:
                amt = _erc20_balance(wallet.rpc_url, contract, wallet.address)
                if amt > 0:
                    holdings.append(_holding(wallet, chain, "USDT", amt))
        elif sym == "USDC":
            contract = EVM_USDC.get(chain)
            if contract:
                amt = _erc20_balance(wallet.rpc_url, contract, wallet.address)
                if amt > 0:
                    holdings.append(_holding(wallet, chain, "USDC", amt))
        elif sym in {"ETH", "BNB", "MATIC", "POL", "AVAX"}:
            amt = _native_balance(wallet.rpc_url, wallet.address)
            if amt > 0:
                holdings.append(_holding(wallet, chain, sym, amt))
    return holdings


def _scan_tron(wallet: WalletConfig) -> list[Holding]:
    holdings: list[Holding] = []
    addr = wallet.address
    if not addr:
        return holdings
    for sym in wallet.tokens or ("USDT", "TRX"):
        sym = sym.upper()
        if sym == "USDT":
            amt = _tron_trc20_balance(addr, TRON_USDT)
            if amt > 0:
                holdings.append(_holding(wallet, "tron", "USDT", amt))
        elif sym == "TRX":
            amt = _tron_trx_balance(addr)
            if amt > 0:
                holdings.append(_holding(wallet, "tron", "TRX", amt))
    return holdings


def _scan_btc(wallet: WalletConfig) -> list[Holding]:
    if not wallet.address:
        return []
    try:
        r = requests.get(
            f"https://mempool.space/api/address/{wallet.address}",
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        sats = int(data.get("chain_stats", {}).get("funded_txo_sum", 0)) - int(
            data.get("chain_stats", {}).get("spent_txo_sum", 0)
        )
        amt = max(0.0, sats / 1e8)
        if amt > 0:
            return [_holding(wallet, "bitcoin", "BTC", amt)]
    except Exception as e:
        log.debug("btc scan %s: %s", wallet.id, e)
    return []


def _scan_sol(wallet: WalletConfig) -> list[Holding]:
    if not wallet.address:
        return []
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [wallet.address],
        }
        r = requests.post("https://api.mainnet-beta.solana.com", json=payload, timeout=20)
        r.raise_for_status()
        lamports = int(r.json().get("result", {}).get("value", 0))
        amt = lamports / 1e9
        if amt > 0:
            return [_holding(wallet, "solana", "SOL", amt)]
    except Exception as e:
        log.debug("sol scan %s: %s", wallet.id, e)
    return []


def _holding(wallet: WalletConfig, chain: str, symbol: str, amount: float) -> Holding:
    px = usd_price(symbol)
    return Holding(
        wallet_id=wallet.id,
        chain=chain,
        symbol=symbol,
        amount=amount,
        usd_value=amount * px,
    )


def _erc20_balance(rpc_url: str, token: str, address: str) -> float:
    addr = address.lower().replace("0x", "").zfill(64)
    data = "0x70a08231" + addr
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": token, "data": data}, "latest"],
        "id": 1,
    }
    r = requests.post(rpc_url, json=payload, timeout=20)
    r.raise_for_status()
    raw = r.json().get("result", "0x0")
    value = int(raw, 16)
    decimals = _erc20_decimals(rpc_url, token)
    return value / (10**decimals)


def _erc20_decimals(rpc_url: str, token: str) -> int:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": token, "data": "0x313ce567"}, "latest"],
        "id": 1,
    }
    try:
        r = requests.post(rpc_url, json=payload, timeout=15)
        r.raise_for_status()
        return int(r.json().get("result", "0x6"), 16)
    except Exception:
        return 6 if "USDT" in token.upper() or "USDC" in token.upper() else 18


def _native_balance(rpc_url: str, address: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    }
    r = requests.post(rpc_url, json=payload, timeout=20)
    r.raise_for_status()
    wei = int(r.json().get("result", "0x0"), 16)
    return wei / 1e18


def _tron_trx_balance(address: str) -> float:
    r = requests.get(
        "https://api.trongrid.io/v1/accounts/" + address,
        timeout=20,
    )
    if r.status_code == 404:
        return 0.0
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        return 0.0
    sun = int(data[0].get("balance", 0))
    return sun / 1e6


def _tron_trc20_balance(address: str, contract: str) -> float:
    r = requests.get(
        "https://api.trongrid.io/v1/accounts/" + address,
        timeout=20,
    )
    if r.status_code == 404:
        return 0.0
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        return 0.0
    for row in data[0].get("trc20") or []:
        if isinstance(row, dict) and contract in row:
            try:
                return int(row[contract]) / 1e6
            except (TypeError, ValueError):
                return 0.0
    return 0.0
