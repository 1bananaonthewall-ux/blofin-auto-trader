from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TreasurySettings:
    api_key: str
    secret: str
    passphrase: str
    sweep_target_usd: float
    sweep_reserve_usd: float
    poll_seconds: int
    dry_run: bool
    target_currency: str
    deposit_chain: str
    deposit_address: str
    deposit_memo: str
    auto_futures_transfer: bool
    wallets_path: Path
    state_dir: Path
    log_path: Path
    # Optional signing keys (env only — never commit)
    evm_private_key: str
    tron_private_key: str


def load_treasury_settings() -> TreasurySettings:
    return TreasurySettings(
        api_key=os.getenv("BLOFIN_API_KEY", "").strip(),
        secret=os.getenv("BLOFIN_SECRET", "").strip(),
        passphrase=os.getenv("BLOFIN_PASSPHRASE", "").strip(),
        sweep_target_usd=float(os.getenv("TREASURY_SWEEP_USD", "100")),
        sweep_reserve_usd=float(os.getenv("TREASURY_SWEEP_RESERVE_USD", "2")),
        poll_seconds=int(os.getenv("TREASURY_POLL_SECONDS", "300")),
        dry_run=_env_bool("TREASURY_DRY_RUN", True),
        target_currency=os.getenv("TREASURY_TARGET_CURRENCY", "USDT").strip().upper(),
        deposit_chain=os.getenv("BLOFIN_DEPOSIT_CHAIN", "TRC20").strip(),
        deposit_address=os.getenv("BLOFIN_USDT_DEPOSIT_ADDRESS", "").strip(),
        deposit_memo=os.getenv("BLOFIN_DEPOSIT_MEMO", "").strip(),
        auto_futures_transfer=_env_bool("TREASURY_AUTO_FUTURES_TRANSFER", True),
        wallets_path=Path(
            os.getenv("TREASURY_WALLETS_JSON", str(ROOT / "treasury" / "wallets.json"))
        ),
        state_dir=ROOT / "treasury" / "state",
        log_path=ROOT / "logs" / "treasury.log",
        evm_private_key=os.getenv("TREASURY_EVM_PRIVATE_KEY", "").strip(),
        tron_private_key=os.getenv("TREASURY_TRON_PRIVATE_KEY", "").strip(),
    )
