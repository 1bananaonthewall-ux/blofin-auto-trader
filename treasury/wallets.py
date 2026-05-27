from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WalletConfig:
    id: str
    type: str
    address: str
    chain: str = ""
    rpc_url: str = ""
    tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class WalletRegistry:
    wallets: list[WalletConfig]
    swap_targets: dict[str, float]


def load_wallet_registry(path: Path) -> WalletRegistry:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Copy treasury/wallets.example.json to treasury/wallets.json "
            "and add your wallet addresses."
        )
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    wallets: list[WalletConfig] = []
    for row in raw.get("wallets", []):
        wallets.append(
            WalletConfig(
                id=str(row.get("id", "")),
                type=str(row.get("type", "")).lower(),
                address=str(row.get("address", "")).strip(),
                chain=str(row.get("chain", "")).lower(),
                rpc_url=str(row.get("rpc_url", "")).strip(),
                tokens=tuple(str(t).upper() for t in row.get("tokens", [])),
            )
        )
    swap_targets = {
        str(k).upper(): float(v) for k, v in (raw.get("swap_targets") or {}).items()
    }
    if not swap_targets:
        swap_targets = {"USDT": 1.0, "USDC": 1.0, "USD": 1.0}
    return WalletRegistry(wallets=wallets, swap_targets=swap_targets)
