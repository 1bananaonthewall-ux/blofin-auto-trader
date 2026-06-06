"""
Hourly maintenance brain — learned policy + LLM autocode for proactive fixes.

Combines:
  - Rule engine (always available)
  - sklearn policy trained on hourly_agent_log.jsonl outcomes
  - state/hourly_autocode.py (template or LLM-generated)
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

ALLOWED_ACTIONS = frozenset(
    {
        "throughput_guard",
        "ml_health",
        "optimizer_force",
        "optimizer_autocode",
        "clear_pause",
        "ml_refit",
        "ml_bootstrap",
        "cortex_train",
        "repair_tpsl",
        "stack_ensure",
    }
)

AUTOCODE_FILE = "hourly_autocode.py"
BRAIN_STATE = "hourly_brain_state.json"
BRAIN_MODEL = "hourly_brain.joblib"
BRAIN_META = "hourly_brain_meta.json"
TRAIN_JSONL = "hourly_brain_train.jsonl"
MAX_AUTOCODE_CHARS = 4000


@dataclass
class MaintenanceSnapshot:
    ts: float = 0.0
    equity: float = 0.0
    free_margin: float = 0.0
    margin_free_pct: float = 0.0
    open_count: int = 0
    opens_60m: int = 0
    target_opens: int = 6
    wins_60m: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    equity_delta_15m_pct: float = 0.0
    throughput_starved: bool = False
    tph_trend: float = 0.0
    entries_paused: bool = False
    entries_never_pause: bool = True
    tpsl_missing: int = 0
    non_compliant: int = 0
    ml_ok: bool = True
    ml_shards: int = 0
    ml_low_shards: bool = False
    ml_pending_labels: int = 0
    ml_deployed: bool = True
    ml_refit_age_h: float = 0.0
    dominant_skip: str = ""
    optimizer_action: str = ""
    anomalies: list[str] = field(default_factory=list)


@dataclass
class BrainResult:
    snapshot: MaintenanceSnapshot
    decided: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    policy: str = "rules"
    autocode_mode: str = "unchanged"


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def build_snapshot(
    settings: "Settings",
    report: dict[str, Any],
    *,
    root: Path,
) -> MaintenanceSnapshot:
    from hourly_3r import target_min_opens_per_hour
    from throughput_guard import _tph_trend  # noqa: PLC2701

    state_dir = settings.state_dir
    tuning = report.get("tuning") or {}
    equity = float(report.get("equity") or 0)
    free = float(report.get("free_margin") or 0)
    opens = int(tuning.get("trades_last_hour", 0) or 0)
    target = target_min_opens_per_hour(settings)

    ml_health = _load_json(state_dir / "ml_health.json", {})
    throughput = _load_json(state_dir / "throughput_guard.json", {})
    log_watch = _load_json(state_dir / "log_watch.json", {})

    try:
        from runtime_gates import read_entries_pause

        paused, _ = read_entries_pause(state_dir)
    except Exception:
        paused = False

    non_bad = report.get("non_compliant") or []
    snap = MaintenanceSnapshot(
        ts=time.time(),
        equity=equity,
        free_margin=free,
        margin_free_pct=(free / equity * 100.0) if equity > 0 else 0.0,
        open_count=int(report.get("open_count", 0) or 0),
        opens_60m=opens,
        target_opens=target,
        wins_60m=int(tuning.get("wins_last_hour", 0) or 0),
        win_rate=float(tuning.get("win_rate_recent", 0) or 0),
        profit_factor=float(tuning.get("profit_factor_recent", 0) or 0),
        equity_delta_15m_pct=float(tuning.get("equity_delta_15m_pct", 0) or 0),
        throughput_starved=opens < target,
        tph_trend=_tph_trend(state_dir),
        entries_paused=paused,
        entries_never_pause=bool(getattr(settings, "entries_never_pause", True)),
        tpsl_missing=int(log_watch.get("tpsl_missing_max", 0) or 0),
        non_compliant=len(non_bad),
        ml_ok=bool(ml_health.get("ok", True)),
        ml_shards=int(ml_health.get("shards", 0) or 0),
        ml_low_shards=int(ml_health.get("shards", 0) or 0) < int(ml_health.get("shard_min", 3) or 3),
        ml_pending_labels=int(ml_health.get("labels_pending", 0) or 0),
        ml_deployed=bool(ml_health.get("model_deployed", True)),
        ml_refit_age_h=float(ml_health.get("refit_age_hours", 0) or 0),
        dominant_skip=str((throughput.get("last_report") or {}).get("log", {}).get("dominant_skip", "")),
        optimizer_action=str(tuning.get("action", "")),
    )
    if snap.throughput_starved:
        snap.anomalies.append(f"opens={opens}/{target}")
    if not snap.ml_ok:
        snap.anomalies.extend(ml_health.get("issues", [])[:3])
    return snap


def _rule_decide(snap: MaintenanceSnapshot) -> list[str]:
    actions: list[str] = []
    if not snap.ml_ok or snap.ml_low_shards or snap.ml_pending_labels >= 3:
        actions.append("ml_health")
    if snap.ml_low_shards:
        actions.append("ml_bootstrap")
    if snap.ml_pending_labels >= 3 or snap.ml_refit_age_h > 2.0:
        actions.append("ml_refit")
    if snap.throughput_starved or snap.tph_trend < -1:
        actions.append("throughput_guard")
        if snap.equity_delta_15m_pct > -3.5:
            actions.append("optimizer_autocode")
    if snap.entries_paused and snap.entries_never_pause:
        actions.append("clear_pause")
    if snap.tpsl_missing > 0:
        actions.append("repair_tpsl")
    if snap.non_compliant > 0:
        actions.append("stack_ensure")
    actions.append("optimizer_force")
    if snap.ml_ok and snap.opens_60m >= snap.target_opens:
        actions.append("cortex_train")
    return _dedupe(actions)


def _dedupe(actions: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in actions:
        if a in ALLOWED_ACTIONS and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _feature_vector(snap: MaintenanceSnapshot) -> list[float]:
    return [
        float(snap.opens_60m),
        float(snap.target_opens),
        float(snap.wins_60m),
        snap.win_rate,
        snap.profit_factor,
        snap.equity_delta_15m_pct,
        snap.margin_free_pct,
        float(snap.open_count),
        float(snap.ml_shards),
        float(snap.ml_pending_labels),
        1.0 if snap.ml_ok else 0.0,
        1.0 if snap.throughput_starved else 0.0,
        snap.tph_trend,
        float(snap.tpsl_missing),
        float(snap.non_compliant),
    ]


FEATURE_NAMES = [
    "opens_60m",
    "target_opens",
    "wins_60m",
    "win_rate",
    "profit_factor",
    "eq15_pct",
    "margin_free_pct",
    "open_count",
    "ml_shards",
    "ml_pending",
    "ml_ok",
    "starved",
    "tph_trend",
    "tpsl_missing",
    "non_compliant",
]


def _load_learned_policy(state_dir: Path):
    path = state_dir / BRAIN_MODEL
    if not path.is_file():
        return None
    try:
        import joblib

        return joblib.load(path)
    except Exception:
        return None


def _learned_decide(state_dir: Path, snap: MaintenanceSnapshot) -> list[str]:
    model = _load_learned_policy(state_dir)
    if model is None:
        return []
    try:
        import numpy as np

        X = np.array([_feature_vector(snap)], dtype=np.float64)
        if hasattr(model, "predict_proba"):
            proba = model.predict_proba(X)[0]
            classes = list(model.classes_)
            picks = [
                classes[i]
                for i, p in enumerate(proba)
                if p >= 0.45 and classes[i] in ALLOWED_ACTIONS
            ]
            return _dedupe(picks)
        pred = model.predict(X)
        if pred is not None and len(pred):
            val = str(pred[0])
            return [val] if val in ALLOWED_ACTIONS else []
    except Exception:
        log.debug("learned policy predict failed", exc_info=True)
    return []


def _load_autocode_decide(state_dir: Path, snap: MaintenanceSnapshot) -> list[str]:
    path = state_dir / AUTOCODE_FILE
    if not path.is_file():
        return []
    try:
        spec = importlib.util.spec_from_file_location("hourly_autocode", path)
        if spec is None or spec.loader is None:
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "decide"):
            return []
        raw = mod.decide(asdict(snap))
        if not isinstance(raw, list):
            return []
        return _dedupe([str(x) for x in raw])
    except Exception as exc:
        log.debug("hourly_autocode decide failed: %s", exc)
        return []


def decide_actions(
    settings: "Settings",
    snap: MaintenanceSnapshot,
) -> tuple[list[str], str]:
    state_dir = settings.state_dir
    rule = _rule_decide(snap)
    learned = _learned_decide(state_dir, snap)
    autocode = _load_autocode_decide(state_dir, snap)

    merged = _dedupe(learned + autocode + rule)
    policy = "blend"
    if learned and autocode:
        policy = "learned+autocode+rules"
    elif learned:
        policy = "learned+rules"
    elif autocode:
        policy = "autocode+rules"
    else:
        policy = "rules"
    return merged, policy


def _autocode_template(mode: str) -> str:
    if mode == "aggressive_flow":
        return '''"""Hourly maintenance — aggressive flow (autocoded)."""
from __future__ import annotations
from typing import Any

def decide(snapshot: dict[str, Any]) -> list[str]:
    actions = ["ml_health", "throughput_guard", "optimizer_autocode", "optimizer_force"]
    if snapshot.get("ml_low_shards"):
        actions.append("ml_bootstrap")
    if snapshot.get("ml_pending_labels", 0) >= 2:
        actions.append("ml_refit")
    if snapshot.get("tpsl_missing", 0) > 0:
        actions.append("repair_tpsl")
    return actions
'''
    if mode == "ml_focus":
        return '''"""Hourly maintenance — ML recovery (autocoded)."""
from __future__ import annotations
from typing import Any

def decide(snapshot: dict[str, Any]) -> list[str]:
    actions = ["ml_health", "ml_refit", "optimizer_force"]
    if snapshot.get("ml_low_shards"):
        actions.append("ml_bootstrap")
    if not snapshot.get("ml_ok", True):
        actions.extend(["throughput_guard", "cortex_train"])
    return actions
'''
    return Path(__file__).resolve().parent.joinpath("state", "hourly_autocode.py").read_text(encoding="utf-8")


def _validate_autocode(code: str) -> bool:
    if len(code) > MAX_AUTOCODE_CHARS:
        return False
    lower = code.lower()
    banned = (
        "import os",
        "import subprocess",
        "open(",
        "exec(",
        "eval(",
        "__import__",
        "socket",
        "requests",
        "pathlib",
    )
    if any(b in lower for b in banned):
        return False
    try:
        mod = ast.parse(code)
    except SyntaxError:
        return False
    funcs = [n for n in mod.body if isinstance(n, ast.FunctionDef)]
    if not any(f.name == "decide" for f in funcs):
        return False
    # Dry-run import check with restricted decide
    ns: dict[str, Any] = {}
    try:
        exec(compile(mod, "<hourly_autocode>", "exec"), ns)  # noqa: S102
        sample = {
            "opens_60m": 2,
            "target_opens": 6,
            "ml_ok": False,
            "throughput_starved": True,
            "equity_delta_15m_pct": 0.0,
        }
        out = ns["decide"](sample)
        if not isinstance(out, list):
            return False
        return all(str(a) in ALLOWED_ACTIONS for a in out)
    except Exception:
        return False


def _llm_autocode(snap: MaintenanceSnapshot, mode: str, history: list[dict]) -> str | None:
    from local_llm import chat_completion, resolve_provider

    system = (
        "Generate ONLY safe Python for state/hourly_autocode.py. "
        "Function: decide(snapshot: dict) -> list[str]. "
        f"Allowed action strings: {sorted(ALLOWED_ACTIONS)}. "
        "No file I/O, no subprocess, no extra imports except __future__ and typing."
    )
    user = {
        "mode": mode,
        "snapshot": asdict(snap),
        "recent_hourly_runs": history[-6:],
        "constraints": {
            "prefer_ml_health_when_ml_ok_false": True,
            "prefer_throughput_when_starved": True,
            "never_return_empty": True,
        },
    }
    text = None
    base = (os.environ.get("HOURLY_CODEGEN_BASE_URL") or os.environ.get("OPTIMIZER_CODEGEN_BASE_URL") or "").strip().rstrip("/")
    if base:
        try:
            import requests

            r = requests.post(
                f"{base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.environ.get('HOURLY_CODEGEN_API_KEY', 'local')}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.environ.get("HOURLY_CODEGEN_MODEL", "local-model"),
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(user)},
                    ],
                    "temperature": 0.15,
                    "max_tokens": 500,
                },
                timeout=45,
            )
            r.raise_for_status()
            text = str(r.json()["choices"][0]["message"]["content"])
        except Exception as exc:
            log.debug("hourly llm codegen http failed: %s", exc)

    if not text and resolve_provider() != "none":
        text, _ = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user)}],
            max_tokens=500,
            temperature=0.15,
        )
    if not text:
        return None
    if "```" in text:
        parts = text.split("```")
        body = parts[1]
        if body.startswith("python"):
            body = body[6:]
        text = body.strip()
    return text if _validate_autocode(text) else None


def maybe_autocode_policy(
    settings: "Settings",
    snap: MaintenanceSnapshot,
    *,
    cooldown_sec: int = 3600,
) -> str:
    if not getattr(settings, "hourly_autocode_enabled", True):
        return "disabled"

    state_dir = settings.state_dir
    st = _load_json(state_dir / BRAIN_STATE, {})
    now = time.time()
    if now - float(st.get("last_autocode_ts", 0)) < cooldown_sec:
        return "unchanged"

    mode = "balanced"
    if snap.throughput_starved and snap.equity_delta_15m_pct > -3.0:
        mode = "aggressive_flow"
    elif not snap.ml_ok or snap.ml_low_shards:
        mode = "ml_focus"

    history = _load_journal(state_dir, limit=12)
    code = _llm_autocode(snap, mode, history) or _autocode_template(mode)
    if not _validate_autocode(code):
        code = _autocode_template("balanced")
    (state_dir / AUTOCODE_FILE).write_text(code, encoding="utf-8")
    st["last_autocode_ts"] = now
    st["last_autocode_mode"] = mode
    (state_dir / BRAIN_STATE).write_text(json.dumps(st, indent=2), encoding="utf-8")
    log.warning("HOURLY AUTOCODE -> %s (%s)", mode, AUTOCODE_FILE)
    return mode


def _load_journal(state_dir: Path, limit: int = 50) -> list[dict]:
    path = state_dir / "hourly_agent_log.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        return []
    return rows[-limit:]


def record_training_row(state_dir: Path, snap: MaintenanceSnapshot, actions: list[str]) -> None:
    path = state_dir / TRAIN_JSONL
    row = {
        "ts": snap.ts,
        "features": dict(zip(FEATURE_NAMES, _feature_vector(snap))),
        "actions": actions,
        "equity": snap.equity,
        "opens_60m": snap.opens_60m,
        "label": None,
        "reward": None,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def label_previous_runs(state_dir: Path, current_equity: float, current_opens: int) -> None:
    """Label prior training rows with 1h outcome reward."""
    path = state_dir / TRAIN_JSONL
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return
    rows: list[dict] = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    now = time.time()
    changed = False
    for i, row in enumerate(rows):
        if row.get("label") is not None:
            continue
        ts = float(row.get("ts", 0))
        if now - ts < 3300 or now - ts > 7200:
            continue
        prev_eq = float(row.get("equity", 0) or 0)
        prev_opens = int(row.get("opens_60m", 0) or 0)
        eq_delta = ((current_equity - prev_eq) / prev_eq * 100.0) if prev_eq > 0 else 0.0
        opens_delta = current_opens - prev_opens
        reward = eq_delta * 0.5 + opens_delta * 2.0
        good = reward > 0.5 or (opens_delta >= 1 and eq_delta > -1.0)
        acts = row.get("actions") or []
        for act in acts:
            if act not in ALLOWED_ACTIONS:
                continue
            # Multi-label: each action in a successful bundle gets positive label
            if good:
                row.setdefault("positive_actions", []).append(act)
        row["reward"] = round(reward, 3)
        row["label"] = 1 if good else 0
        rows[i] = row
        changed = True
    if changed:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def maybe_train_policy(state_dir: Path, *, min_samples: int = 24) -> bool:
    path = state_dir / TRAIN_JSONL
    if not path.is_file():
        return False
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("label") is None:
            continue
        pos = r.get("positive_actions") or []
        if not pos and r.get("label") == 1 and r.get("actions"):
            pos = r["actions"]
        for act in pos:
            if act in ALLOWED_ACTIONS:
                rows.append((r.get("features", {}), act))
    if len(rows) < min_samples:
        return False

    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        import joblib

        X_list, y_list = [], []
        for feats, act in rows:
            vec = [float(feats.get(k, 0)) for k in FEATURE_NAMES]
            X_list.append(vec)
            y_list.append(act)
        X = np.array(X_list, dtype=np.float64)
        y = np.array(y_list)
        model = RandomForestClassifier(
            n_estimators=80,
            max_depth=8,
            min_samples_leaf=2,
            random_state=42,
            class_weight="balanced",
        )
        model.fit(X, y)
        joblib.dump(model, state_dir / BRAIN_MODEL)
        (state_dir / BRAIN_META).write_text(
            json.dumps(
                {
                    "trained_at": time.time(),
                    "samples": len(rows),
                    "classes": sorted(set(y_list)),
                    "feature_names": FEATURE_NAMES,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        log.warning("HOURLY BRAIN trained on %d labelled action samples", len(rows))
        return True
    except Exception:
        log.exception("hourly brain train failed")
        return False


def apply_action(
    action: str,
    settings: "Settings",
    ex,
    registry,
    snap: MaintenanceSnapshot,
    *,
    root: Path,
) -> str:
    state_dir = settings.state_dir
    if action == "throughput_guard":
        from throughput_guard import tick as throughput_tick

        tg = throughput_tick(
            settings,
            equity=snap.equity,
            free_margin=snap.free_margin,
            opens_60m=snap.opens_60m,
            force=True,
            root=root,
        )
        return f"throughput:{len(tg.get('actions') or [])}"

    if action == "ml_health":
        from ml_health_guard import tick as ml_health_tick

        rep = ml_health_tick(settings, ex=ex, root=root, force=True, hourly=True)
        return f"ml:{';'.join((rep.get('actions') or [])[:3])}"

    if action == "optimizer_force":
        from scalp_optimizer import ScalpOptimizer

        opt = ScalpOptimizer(state_dir, settings)
        rep = opt.maybe_optimize(ex.fetch_equity_usdt(), force=True)
        return rep.summary if rep else "optimizer:skip"

    if action == "optimizer_autocode":
        from optimizer_autocode import maybe_apply_autocode, _template

        mode = maybe_apply_autocode(
            state_dir,
            enabled=True,
            action=snap.optimizer_action or "loosen_throughput",
            win_rate=snap.win_rate,
            profit_factor=snap.profit_factor,
            equity_delta_15m_pct=snap.equity_delta_15m_pct,
            trades_last_hour=snap.opens_60m,
            cooldown_sec=1800,
        )
        if mode in ("unchanged", "disabled"):
            (state_dir / "optimizer_overrides.py").write_text(_template("throughput"), encoding="utf-8")
            mode = "throughput_template"
        return f"optimizer_autocode:{mode}"

    if action == "clear_pause":
        from runtime_gates import clear_entries_pause

        clear_entries_pause(state_dir)
        return "cleared_pause"

    if action == "ml_refit":
        (state_dir / "ml_force_refit.flag").write_text(
            json.dumps({"reason": "hourly_brain"}, indent=2), encoding="utf-8"
        )
        return "ml_refit_flag"

    if action == "ml_bootstrap":
        (state_dir / "ml_bootstrap_due.flag").write_text(
            json.dumps({"reason": "hourly_brain"}, indent=2), encoding="utf-8"
        )
        return "ml_bootstrap_flag"

    if action == "cortex_train":
        from local_cortex import train

        summary = train(state_dir)
        return f"cortex:{summary.get('examples', 0)}"

    if action == "repair_tpsl":
        script = root / "scripts" / "repair_open_tpsl.py"
        if script.is_file():
            subprocess.run([sys.executable, str(script)], cwd=str(root), check=False, timeout=120)
            return "repair_tpsl_ran"
        return "repair_tpsl_missing"

    if action == "stack_ensure":
        ps1 = root / "scripts" / "stack_control.ps1"
        if ps1.is_file():
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Action", "ensure"],
                cwd=str(root),
                check=False,
                timeout=90,
            )
            return "stack_ensure_ran"
        return "stack_ensure_skip"

    return f"unknown:{action}"


def run_hourly_brain(
    settings: "Settings",
    ex,
    registry,
    report: dict[str, Any],
    *,
    root: Path,
    skip_apply: bool = False,
) -> BrainResult:
    """Full hourly intelligence pass: snapshot → decide → autocode → apply → learn."""
    state_dir = settings.state_dir
    snap = build_snapshot(settings, report, root=root)

    label_previous_runs(state_dir, snap.equity, snap.opens_60m)
    maybe_train_policy(state_dir)

    autocode_mode = "unchanged"
    if getattr(settings, "hourly_autocode_enabled", True):
        autocode_mode = maybe_autocode_policy(settings, snap)

    decided, policy = decide_actions(settings, snap)
    result = BrainResult(snapshot=snap, decided=decided, policy=policy, autocode_mode=autocode_mode)

    if skip_apply:
        result.notes.append("apply_skipped")
        record_training_row(state_dir, snap, decided)
        return result

    for action in decided:
        try:
            note = apply_action(action, settings, ex, registry, snap, root=root)
            result.applied.append(f"{action}:{note}")
        except Exception as exc:
            result.applied.append(f"{action}:err:{exc}")
            log.warning("hourly brain action %s failed: %s", action, exc)

    record_training_row(state_dir, snap, decided)
    st = _load_json(state_dir / BRAIN_STATE, {})
    st["last_run"] = {
        "ts": snap.ts,
        "policy": policy,
        "decided": decided,
        "applied": result.applied,
        "anomalies": snap.anomalies,
    }
    (state_dir / BRAIN_STATE).write_text(json.dumps(st, indent=2), encoding="utf-8")

    log.warning(
        "HOURLY BRAIN | policy=%s autocode=%s | decided=%s",
        policy,
        autocode_mode,
        ",".join(decided),
    )
    return result


def run_standalone() -> int:
    from config import load_settings
    from exchange_client import BlofinExchange
    from position_registry import PositionRegistry

    root = Path(__file__).resolve().parent
    settings = load_settings()
    report = _load_json(settings.state_dir / "hourly_report.json", {})
    if not report:
        subprocess.run([sys.executable, str(root / "scripts" / "hourly_health_report.py")], check=False)
        report = _load_json(settings.state_dir / "hourly_report.json", {})
    ex = BlofinExchange(settings)
    ex.load()
    registry = PositionRegistry(settings.state_dir)
    result = run_hourly_brain(settings, ex, registry, report, root=root)
    print(json.dumps(asdict(result), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_standalone())
