"""
Poll project files and hot-reload settings + Python modules without restarting the bot.

Exchange websocket, steward thread, and ML trainer thread keep running; entry/signal
logic and .env knobs refresh on the next tick.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SKIP_DIRS = frozenset(
    {".venv", "__pycache__", "state", "logs", ".git", "treasury", "node_modules", ".cursor"}
)

# Dependency order: reload leaves after their imports.
_RELOAD_ORDER = (
    "indicators",
    "mission_config",
    "ta_confluence",
    "run_quality",
    "bobs_bots.regime",
    "strategy",
    "scalp_profile",
    "ml.labels",
    "ml.regime_labels",
    "ml.features",
    "ml.matrix_kwargs",
    "ml.edge_gate",
    "ml.predictor",
    "ml.trainer",
    "ml.universe_trainer",
    "pick_engine",
    "winner_gate",
    "signals",
    "conviction",
    "account_guard",
    "symbol_side_guard",
    "scan_orchestrator",
    "scalp_optimizer",
    "optimizer_autocode",
    "optimizer_overrides",
    "universe_rater",
    "trade_blockers",
    "overseer_env",
    "llm_overseer",
    "margin_engine",
    "liquidation_guard",
    "tpsl_guard",
    "blofin_http",
    "exchange_client",
    "position_steward",
    "position_registry",
    "fee_engine",
    "risk",
    "position_rotator",
    "autonomous_engine",
    "self_heal",
    "entry_pacer",
    "cooldowns",
    "journal",
    "universe",
    "markets",
    "growth_optimizer",
    "mission_brain",
    "core_brain",
    "swarm_brain",
    "playbook_loader",
    "markov_regime",
    "fluid_manifold",
    "pnl_curve",
    "drawdown_guard",
    "throughput_brain",
    "leverage_rotation",
    "position_brain",
)


@dataclass
class RuntimeCtx:
    settings: Any
    engine: Any
    steward: Any
    ml_trainer: Any | None
    optimizer: Any
    ml: Any
    healer: Any
    ex: Any | None = None


class LiveReloader:
    def __init__(
        self,
        root: Path,
        *,
        poll_seconds: float = 3.0,
        git_pull: bool = False,
        git_interval_seconds: float = 300.0,
    ) -> None:
        self.root = root.resolve()
        self.poll_seconds = poll_seconds
        self.git_pull = git_pull
        self.git_interval_seconds = git_interval_seconds
        self._mtimes: dict[str, float] = {}
        self._last_poll = 0.0
        self._last_git = 0.0
        self._bootstrapped = False

    def maybe_reload(self, ctx: RuntimeCtx) -> bool:
        now = time.time()
        if now - self._last_poll < self.poll_seconds:
            return False
        self._last_poll = now

        if self.git_pull:
            self._maybe_git_pull(now)

        changed = self._changed_files()
        if not self._bootstrapped:
            self._snapshot_all()
            self._bootstrapped = True
            return False
        if not changed:
            return False

        env_changed = any(p.name == ".env" for p in changed)
        py_changed = [p for p in changed if p.suffix == ".py"]
        bot_changed = any(p.name == "bot.py" for p in py_changed)

        if bot_changed:
            log.warning(
                "live update: bot.py changed — entry/signal modules reloaded; "
                "restart run.ps1 for main-loop / startup changes"
            )

        thread_sources = {
            "position_steward.py",
            "ml/universe_trainer.py",
            "market_stream.py",
            "exchange_client.py",
        }
        rel_paths = {str(p.relative_to(self.root)).replace("\\", "/") for p in py_changed}
        if rel_paths & thread_sources:
            log.info(
                "live update: background-thread sources changed (%s) — "
                "settings apply now; thread code needs restart for full effect",
                ", ".join(sorted(rel_paths & thread_sources)),
            )

        try:
            reloaded: list[str] = []
            if py_changed:
                reloaded = _reload_modules()
                _patch_bot_namespace()
            if env_changed or py_changed:
                ctx.settings = _reload_settings()
                _apply_runtime(ctx)
                if py_changed and ctx.ml is not None:
                    ctx.ml.reload()
            names = [p.relative_to(self.root).as_posix() for p in changed[:8]]
            extra = f" +{len(changed) - 8} more" if len(changed) > 8 else ""
            log.warning(
                "LIVE UPDATE applied | files=%d%s | modules=%d | .env=%s",
                len(changed),
                extra,
                len(reloaded),
                env_changed,
            )
            log.info("live update touched: %s%s", ", ".join(names), extra)
            return True
        except Exception:
            log.exception("live update failed — keeping previous settings/code")
            return False

    def _maybe_git_pull(self, now: float) -> None:
        if now - self._last_git < self.git_interval_seconds:
            return
        self._last_git = now
        try:
            r = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if r.returncode == 0:
                line = (r.stdout or "").strip().splitlines()
                summary = line[-1] if line else "ok"
                if "Already up to date" not in summary:
                    log.info("live update git pull: %s", summary)
            else:
                err = (r.stderr or r.stdout or "").strip()[:200]
                log.warning("live update git pull failed: %s", err or r.returncode)
        except Exception:
            log.exception("live update git pull error")

    def _iter_watch_paths(self) -> list[Path]:
        out: list[Path] = []
        env_path = self.root / ".env"
        if env_path.is_file():
            out.append(env_path)
        for path in self.root.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.name == "live_update.py":
                continue
            out.append(path)
        return out

    def _file_mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return -1.0

    def _snapshot_all(self) -> None:
        for path in self._iter_watch_paths():
            self._mtimes[str(path)] = self._file_mtime(path)

    def _changed_files(self) -> list[Path]:
        changed: list[Path] = []
        for path in self._iter_watch_paths():
            key = str(path)
            mtime = self._file_mtime(path)
            prev = self._mtimes.get(key)
            if prev is None:
                self._mtimes[key] = mtime
                if self._bootstrapped:
                    changed.append(path)
            elif mtime != prev:
                self._mtimes[key] = mtime
                changed.append(path)
        return changed


def _reload_modules() -> list[str]:
    ok: list[str] = []
    for name in _RELOAD_ORDER:
        try:
            mod = sys.modules.get(name) or importlib.import_module(name)
            importlib.reload(mod)
            ok.append(name)
        except ModuleNotFoundError:
            continue
        except Exception as exc:
            log.warning("live update: skip reload %s: %s", name, exc)
    try:
        import config as config_mod

        importlib.reload(config_mod)
        ok.append("config")
    except Exception as exc:
        log.warning("live update: skip reload config: %s", exc)
    return ok


def _reload_settings() -> Any:
    from dotenv import load_dotenv

    from config import ROOT, load_settings

    load_dotenv(ROOT / ".env", override=True)
    return load_settings()


def _patch_bot_namespace() -> None:
    import bot
    import account_guard
    import conviction
    import scalp_optimizer
    import signals
    from ml import features
    from scan_orchestrator import ScanOrchestrator
    from strategy import Signal
    from universe import load_tradeable_markets

    bot.analyze_symbol = signals.analyze_symbol
    bot.rank_setups = conviction.rank_setups
    bot.select_conviction_ties = conviction.select_conviction_ties
    bot.margin_fraction_for_conviction = conviction.margin_fraction_for_conviction
    bot.entry_allowed = account_guard.entry_allowed
    bot.effective_max_open = account_guard.effective_max_open
    bot.UNLIMITED_POSITIONS = account_guard.UNLIMITED_POSITIONS
    bot.same_side_exposure_ok = account_guard.same_side_exposure_ok
    bot.effective_entry_gap = scalp_optimizer.effective_entry_gap
    bot.effective_cooldown_minutes = scalp_optimizer.effective_cooldown_minutes
    bot.build_feature_vector = features.build_feature_vector
    bot.Signal = Signal
    bot.load_tradeable_markets = load_tradeable_markets
    bot._scan_orchestrator = ScanOrchestrator()


def _apply_runtime(ctx: RuntimeCtx) -> None:
    s = ctx.settings
    ctx.engine.bind_settings(s)
    ctx.engine.unrestricted_trading = s.unrestricted_trading
    ctx.engine.entries_never_pause = s.entries_never_pause
    ctx.steward.settings = s
    ctx.optimizer.settings = s
    if ctx.ml_trainer is not None:
        ctx.ml_trainer.settings = s
    # Steward thread holds a BlofinExchange instance; hot-swap so TP/SL repair code updates apply.
    try:
        from exchange_client import BlofinExchange

        old_ex = ctx.steward.ex
        new_ex = BlofinExchange(s)
        new_ex.markets = getattr(old_ex, "markets", {}) or {}
        new_ex.stream = getattr(old_ex, "stream", None)
        new_ex._hedge_mode = getattr(old_ex, "_hedge_mode", new_ex._hedge_mode)
        new_ex._cached_positions = dict(getattr(old_ex, "_cached_positions", {}) or {})
        new_ex._cached_equity = float(getattr(old_ex, "_cached_equity", 0) or 0)
        new_ex._cached_free = float(getattr(old_ex, "_cached_free", 0) or 0)
        ctx.steward.ex = new_ex
        ctx.ex = new_ex
        log.warning("live update: exchange client hot-swapped (TP/SL repair)")
    except Exception:
        log.exception("live update: steward exchange hot-swap failed — restart bot for TP/SL fix")


def create_reloader(settings: Any) -> LiveReloader | None:
    if not getattr(settings, "live_update_enabled", True):
        return None
    return LiveReloader(
        Path(__file__).resolve().parent,
        poll_seconds=float(getattr(settings, "live_update_poll_seconds", 3.0)),
        git_pull=getattr(settings, "live_update_git_pull", False),
        git_interval_seconds=float(
            getattr(settings, "live_update_git_interval_seconds", 300.0)
        ),
    )
