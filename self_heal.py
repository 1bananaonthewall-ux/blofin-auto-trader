"""
Self-heal — automatic recovery without manual restart.

Detects and fixes:
  - Stale peak equity locking drawdown / fluid pause
  - Config drift (UNRESTRICTED_TRADING not applied until restart)
  - ML model load failures and retrain storms
  - Entry pause streaks (recovery mode boost)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autonomous_engine import AutonomousGrowthEngine, RuntimeKnobs
    from config import Settings
    from exchange_client import BlofinExchange
    from ml.predictor import MLPredictor
    from ml.universe_trainer import ContinuousMlTrainer

log = logging.getLogger(__name__)

MIN_PEAK_RESET_INTERVAL = 1800.0
MIN_REFIT_REQUEST_INTERVAL = 3600.0
MIN_SUBPROCESS_TRAIN_INTERVAL = 7200.0
MIN_TPSL_HEAL_INTERVAL = 30.0
PAUSE_STREAK_FOR_RECOVERY = 3
DRAWDOWN_PEAK_RESET_PCT = 25.0
RECOVERY_DURATION_SEC = 3600.0
LOW_INTENSITY_THRESHOLD = 0.12


@dataclass
class HealPersisted:
    pause_streak: int = 0
    last_peak_reset: float = 0.0
    last_full_refit_request: float = 0.0
    last_subprocess_train: float = 0.0
    last_heal_log: float = 0.0
    last_tpsl_heal: float = 0.0
    recent_actions: list[str] = field(default_factory=list)


class SelfHealer:
    def __init__(self, state_dir: Path, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._path = state_dir / "self_heal.json"
        self._state = HealPersisted()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._state = HealPersisted(
                pause_streak=int(raw.get("pause_streak", 0)),
                last_peak_reset=float(raw.get("last_peak_reset", 0)),
                last_full_refit_request=float(raw.get("last_full_refit_request", 0)),
                last_subprocess_train=float(raw.get("last_subprocess_train", 0)),
                last_heal_log=float(raw.get("last_heal_log", 0)),
                last_tpsl_heal=float(raw.get("last_tpsl_heal", 0)),
                recent_actions=list(raw.get("recent_actions", []))[-20:],
            )
        except Exception:
            pass

    def _persist(self, actions: list[str]) -> None:
        self._state.recent_actions = (actions + self._state.recent_actions)[:20]
        self._state.last_heal_log = time.time()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "pause_streak": self._state.pause_streak,
                    "last_peak_reset": self._state.last_peak_reset,
                    "last_full_refit_request": self._state.last_full_refit_request,
                    "last_subprocess_train": self._state.last_subprocess_train,
                    "last_heal_log": self._state.last_heal_log,
                    "last_tpsl_heal": self._state.last_tpsl_heal,
                    "recent_actions": self._state.recent_actions,
                    "updated_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def sync_engine_config(engine: AutonomousGrowthEngine, settings: Settings) -> None:
        """Re-apply .env flags each tick so restart is not required for config changes."""
        engine.unrestricted_trading = settings.unrestricted_trading
        engine.entries_never_pause = settings.entries_never_pause

    @staticmethod
    def reset_peaks(engine: AutonomousGrowthEngine, equity: float) -> None:
        eq = max(float(equity), 0.01)
        engine.manifold.reset_peaks(eq)
        engine.pnl.reset_peak(eq)
        engine.manifold.clear_force_retrain()

    def tick(
        self,
        engine: AutonomousGrowthEngine,
        settings: Settings,
        equity: float,
        free_margin: float,
        knobs: RuntimeKnobs,
        ml: MLPredictor | None,
        ml_trainer: ContinuousMlTrainer | None,
    ) -> list[str]:
        if not self.enabled:
            return []

        self.sync_engine_config(engine, settings)
        actions: list[str] = []
        now = time.time()

        if not knobs.allow_new_entries and equity > 1.0:
            self._state.pause_streak += 1
        else:
            self._state.pause_streak = 0

        dd = knobs.drawdown_pct
        stale_peak = (
            dd >= DRAWDOWN_PEAK_RESET_PCT
            and now - self._state.last_peak_reset >= MIN_PEAK_RESET_INTERVAL
        )
        low_intensity = knobs.action_intensity < LOW_INTENSITY_THRESHOLD
        if stale_peak and (self._state.pause_streak >= 2 or low_intensity):
            self.reset_peaks(engine, equity)
            self._state.last_peak_reset = now
            engine.activate_recovery(RECOVERY_DURATION_SEC)
            actions.append(f"reset_peak_equity dd={dd:.1f}%")

        if self._state.pause_streak >= PAUSE_STREAK_FOR_RECOVERY:
            engine.activate_recovery(RECOVERY_DURATION_SEC)
            if low_intensity and now - self._state.last_peak_reset >= 600:
                self.reset_peaks(engine, equity)
                self._state.last_peak_reset = now
                actions.append("recovery_mode_peak_reset")
            else:
                actions.append("recovery_mode_on")

        if ml is not None:
            actions.extend(self._heal_ml(settings, ml, ml_trainer))

        try:
            from throughput_guard import tick as throughput_tick

            tg = throughput_tick(settings, equity=equity, free_margin=free_margin)
            if tg.get("actions"):
                actions.extend([f"flow:{a}" for a in tg["actions"][:4]])
        except Exception:
            log.debug("throughput_guard tick failed", exc_info=True)

        engine.manifold.clear_force_retrain()

        if actions:
            log.warning("SELF-HEAL: %s", " | ".join(actions))
            self._persist(actions)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._state.pause_streak or self._state.last_peak_reset:
                self._persist([])

        return actions

    def heal_open_tpsl(
        self,
        ex: "BlofinExchange",
        settings: Settings,
        positions: dict,
    ) -> list[str]:
        """Re-attach missing SL/TP without canceling winners; clears repair cooldown when naked."""
        if not self.enabled:
            return []
        import api_backoff

        if api_backoff.is_paused():
            return []
        now = time.time()
        from markets import symbol_to_inst_id
        from tpsl_guard import pending_is_adequate

        actions: list[str] = []
        any_naked = False
        for symbol, pos in positions.items():
            if "#" in symbol and not pos.get("position_key"):
                continue
            trade_sym = str(pos.get("symbol") or symbol).split("#", 1)[0]
            side = str(pos.get("side") or "")
            entry = float(pos.get("entry_price") or 0)
            contracts = float(pos.get("contracts") or 0)
            if not side or entry <= 0 or contracts <= 0:
                continue
            inst_id = symbol_to_inst_id(trade_sym)
            position_side = ex._position_side_for_order(side, pos)
            _, pending = ex._pending_tpsl(
                inst_id,
                side,
                entry,
                position_side=position_side,
                allow_registry_fallback=False,
            )
            if pending.live_rows > 0 and pending_is_adequate(side, entry, pending):
                continue
            any_naked = True
            ex._clear_tpsl_trust(trade_sym)
            ex._tpsl_repair_at.pop(ex._canonical_symbol(trade_sym), None)
            meta_take = float(pos.get("take_pct") or 0.022)
            lev = int(pos.get("effective_leverage") or settings.scalp_leverage_max)
            ok, _, _ = ex.repair_position_tpsl(
                trade_sym,
                side,
                contracts,
                take_pct=meta_take,
                configured_leverage=lev,
                dry_run=settings.dry_run,
                cancel_existing=False,
            )
            tag = symbol.split("/")[0]
            live_after = ex.live_exchange_tpsl(trade_sym, side, entry, pos=pos)
            if ok and live_after:
                actions.append(f"tpsl_healed_{tag}")
            else:
                actions.append(f"tpsl_naked_{tag}")

        if not any_naked and now - self._state.last_tpsl_heal < MIN_TPSL_HEAL_INTERVAL:
            return []
        if actions:
            self._state.last_tpsl_heal = now
            log.warning("SELF-HEAL TPSL: %s", " | ".join(actions))
            self._persist(actions)
        elif any_naked:
            log.warning("SELF-HEAL TPSL: naked positions but repair returned no success")
        return actions

    def _ml_flag_actions(
        self,
        settings: Settings,
        ml: MLPredictor,
        ml_trainer: ContinuousMlTrainer | None,
        now: float,
    ) -> list[str]:
        actions: list[str] = []
        state_dir = settings.state_dir

        reload_flag = state_dir / "ml_reload.flag"
        if reload_flag.is_file():
            try:
                reload_flag.unlink(missing_ok=True)
            except OSError:
                pass
            if ml.reload():
                actions.append("ml_model_reloaded_flag")

        instant_flag = state_dir / "ml_instant_refit.flag"
        if instant_flag.is_file():
            try:
                instant_flag.unlink(missing_ok=True)
            except OSError:
                pass
            if ml_trainer and now - self._state.last_full_refit_request >= 90:
                ml_trainer.request_full_refit(force=True)
                self._state.last_full_refit_request = now
                actions.append("ml_instant_lesson_refit")

        bootstrap_flag = state_dir / "ml_bootstrap_due.flag"
        if bootstrap_flag.is_file():
            try:
                bootstrap_flag.unlink(missing_ok=True)
            except OSError:
                pass
            if ml_trainer:
                ml_trainer._bootstrapped = False
                ml_trainer.request_full_refit(force=True)
                self._state.last_full_refit_request = now
                actions.append("ml_bootstrap_due")

        flag = state_dir / "ml_force_refit.flag"
        if flag.is_file():
            try:
                flag.unlink(missing_ok=True)
            except OSError:
                pass
            if ml_trainer:
                ml_trainer.request_full_refit(force=True)
                self._state.last_full_refit_request = now
                actions.append("ml_force_refit_flag")

        return actions

    def _heal_ml(
        self,
        settings: Settings,
        ml: MLPredictor,
        ml_trainer: ContinuousMlTrainer | None,
    ) -> list[str]:
        if settings.signal_mode != "ml":
            return []

        now = time.time()
        actions = self._ml_flag_actions(settings, ml, ml_trainer, now)

        if ml.is_ready():
            if ml_trainer and now - self._state.last_full_refit_request >= 600:
                ml_trainer.request_full_refit()
                self._state.last_full_refit_request = now
                actions.append("background_ml_refit")
            return actions

        if ml.reload():
            actions.append("ml_model_reloaded")
            return actions

        corrupt = ml.model_path
        if corrupt.exists():
            try:
                from ml.trainer import SignalModel

                SignalModel.load(ml.model_path, ml.meta_path)
            except Exception:
                backup = corrupt.with_suffix(".joblib.bad")
                if backup.exists():
                    backup.unlink()
                corrupt.rename(backup)
                actions.append("quarantined_corrupt_model")

        return actions

    def should_request_refit(
        self,
        engine: AutonomousGrowthEngine,
        settings: Settings,
        knobs: RuntimeKnobs,
    ) -> bool:
        if settings.signal_mode != "ml":
            return False
        wants = knobs.should_retrain_ml or engine.fluid_wants_retrain()
        if not wants:
            return False
        now = time.time()
        if now - self._state.last_full_refit_request < MIN_REFIT_REQUEST_INTERVAL:
            return False
        self._state.last_full_refit_request = now
        return True

    def allow_subprocess_train(self) -> bool:
        return time.time() - self._state.last_subprocess_train >= MIN_SUBPROCESS_TRAIN_INTERVAL

    def mark_subprocess_train(self) -> None:
        self._state.last_subprocess_train = time.time()

    def mark_refit_requested(self) -> None:
        self._state.last_full_refit_request = time.time()

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "pause_streak": self._state.pause_streak,
            "recent_actions": self._state.recent_actions[:5],
        }
