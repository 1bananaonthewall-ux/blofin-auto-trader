"""
Train and serve domain knowledge for the local WhatsApp LLM (Option A).

Builds RAG + few-shot examples from trade outcomes, playbooks, hourly logs,
and bot doctrine so the model answers like this stack — not generic ChatGPT.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
CORTEX_DIR = ROOT / "state" / "cortex"
KNOWLEDGE_MD = CORTEX_DIR / "knowledge.md"
TRAIN_JSONL = CORTEX_DIR / "train.jsonl"
STATS_JSON = CORTEX_DIR / "stats.json"
META_JSON = CORTEX_DIR / "meta.json"


@dataclass
class CortexStats:
    entries: int = 0
    closes: int = 0
    wins: int = 0
    losses: int = 0
    symbol_wins: dict[str, int] | None = None
    symbol_losses: dict[str, int] | None = None
    markov_wins: dict[str, int] | None = None
    markov_losses: dict[str, int] | None = None
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0

    def win_rate(self) -> float:
        n = self.wins + self.losses
        return (self.wins / n * 100) if n else 0.0


def _read_jsonl(path: Path, limit: int = 50_000) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(rows) >= limit:
            break
    return rows


def _load_playbooks() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((ROOT / "playbooks").glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            log.debug("playbook %s: %s", p.name, exc)
    return out


def _analyze_outcomes(path: Path) -> CortexStats:
    rows = _read_jsonl(path)
    st = CortexStats(
        symbol_wins=Counter(),
        symbol_losses=Counter(),
        markov_wins=Counter(),
        markov_losses=Counter(),
    )
    win_pcts: list[float] = []
    loss_pcts: list[float] = []
    runner_wins = runner_losses = choppy_wins = choppy_losses = 0

    for r in rows:
        ev = r.get("event", "")
        if ev == "entry":
            st.entries += 1
            continue
        if ev not in ("close", "outcome"):
            continue
        st.closes += 1
        sym = str(r.get("symbol", "?")).split("/")[0]
        mk = str(r.get("markov_state") or "unknown")
        outcome = str(r.get("outcome") or "")
        pnl = float(r.get("pnl_pct") or r.get("net_pnl_pct") or 0)
        won = bool(r.get("win")) or outcome == "win" or pnl > 0
        if outcome == "loss" or (r.get("win") == 0 and outcome):
            won = False
        rl = str(r.get("run_label") or "")
        if rl == "runner":
            if won:
                runner_wins += 1
            else:
                runner_losses += 1
        elif rl == "choppy":
            if won:
                choppy_wins += 1
            else:
                choppy_losses += 1
        if won:
            st.wins += 1
            st.symbol_wins[sym] += 1
            st.markov_wins[mk] += 1
            win_pcts.append(pnl)
        else:
            st.losses += 1
            st.symbol_losses[sym] += 1
            st.markov_losses[mk] += 1
            loss_pcts.append(abs(pnl))

    if win_pcts:
        st.avg_win_pct = sum(win_pcts) / len(win_pcts)
    if loss_pcts:
        st.avg_loss_pct = sum(loss_pcts) / len(loss_pcts)
    st.runner_wins = runner_wins  # type: ignore[attr-defined]
    st.runner_losses = runner_losses  # type: ignore[attr-defined]
    st.choppy_wins = choppy_wins  # type: ignore[attr-defined]
    st.choppy_losses = choppy_losses  # type: ignore[attr-defined]
    return st


def _doctrine_lines() -> list[str]:
    return [
        "Mission: steepen dashboard ACCOUNT CURVE — +10%/day floor, take profit, stay vertical.",
        "Prefer steady directional runners; skip choppy up/down symbols.",
        "50x leverage where exchange allows, 3R reward:risk (SCALP_3R_MODE).",
        "core_brain is the only book manager; never_close_on_signal_flip — steward harvests winners.",
        "Stops are EXCHANGE TP/SL orders (repair_position_tpsl). NO exchange SL = position will not auto-stop.",
        "Markov regime filter throttles entries in stress; swarm_brain adds free local votes (no cloud LLM).",
        "symbol_quality down-weights symbols with bad live fill/slippage history.",
        "Hourly: close positions below 50x inst/eff unless symbol cap < 50x; run scalp_optimizer.",
        "WhatsApp commands: status, positions, slcheck, restart, stop, start, stats, help.",
    ]


def _build_knowledge_md(stats: CortexStats, playbooks: list[dict]) -> str:
    lines = [
        "# Blofin auto-trader cortex (trained from live state)",
        f"Trained UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
        "",
        "## Doctrine",
    ]
    lines.extend(f"- {d}" for d in _doctrine_lines())
    lines.extend(
        [
            "",
            "## Live performance (from trade_outcomes.jsonl)",
            f"- Closed trades: {stats.closes} (entries logged: {stats.entries})",
            f"- Win rate: {stats.win_rate():.1f}% ({stats.wins}W / {stats.losses}L)",
            f"- Avg win %: {stats.avg_win_pct:.3f}  Avg loss %: {stats.avg_loss_pct:.3f}",
        ]
    )
    if stats.symbol_wins or stats.symbol_losses:
        lines.append("")
        lines.append("### Symbol edge (wins vs losses)")
        syms = set((stats.symbol_wins or {}).keys()) | set((stats.symbol_losses or {}).keys())
        ranked = []
        for s in syms:
            w = (stats.symbol_wins or {}).get(s, 0)
            l = (stats.symbol_losses or {}).get(s, 0)
            n = w + l
            if n >= 3:
                ranked.append((w / n, s, w, l, n))
        for _, s, w, l, n in sorted(ranked, reverse=True)[:12]:
            lines.append(f"- {s}: {w}W/{l}L ({w/n*100:.0f}% over {n})")
        for _, s, w, l, n in sorted(ranked)[:6]:
            if w / n < 0.35:
                lines.append(f"- AVOID lean: {s} ({w}W/{l}L)")

    rw = int(getattr(stats, "runner_wins", 0) or 0)
    rl = int(getattr(stats, "runner_losses", 0) or 0)
    cw = int(getattr(stats, "choppy_wins", 0) or 0)
    cl = int(getattr(stats, "choppy_losses", 0) or 0)
    if rw + rl + cw + cl > 0:
        lines.append("")
        lines.append("### Runner vs choppy (account-curve picks)")
        if rw + rl:
            lines.append(f"- runner: {rw}W/{rl}L ({rw/(rw+rl)*100:.0f}% WR)" if rw + rl else "")
        if cw + cl:
            lines.append(f"- choppy: {cw}W/{cl}L ({cw/(cw+cl)*100:.0f}% WR)" if cw + cl else "")

    if stats.markov_wins or stats.markov_losses:
        lines.append("")
        lines.append("### Markov regime at entry")
        regs = set((stats.markov_wins or {}).keys()) | set((stats.markov_losses or {}).keys())
        for reg in sorted(regs):
            w = (stats.markov_wins or {}).get(reg, 0)
            l = (stats.markov_losses or {}).get(reg, 0)
            n = w + l
            if n:
                lines.append(f"- {reg}: {w}W/{l}L")

    lines.append("")
    lines.append("## Playbooks")
    for pb in playbooks:
        src = pb.get("source", pb.get("version", "playbook"))
        lines.append(f"### {src}")
        for c in pb.get("concepts") or []:
            lines.append(f"- {c}")
        for d in pb.get("doctrines") or []:
            title = d.get("title", d.get("id", ""))
            summary = d.get("summary", "")
            lines.append(f"- **{title}**: {summary}")
        mapping = pb.get("bot_mapping") or {}
        for k, v in mapping.items():
            lines.append(f"- regime {k}: {v}")

    lines.append("")
    lines.append("## SL troubleshooting")
    lines.extend(
        [
            "- pending_tpsl empty → call slcheck; bot may have failed repair after fill.",
            "- mark past SL but open → trigger may be last price; or no SL order exists.",
            "- Do not expect Python to close on mark; exchange must fill SL.",
        ]
    )
    return "\n".join(lines)


def _synth_qa_pairs(stats: CortexStats, playbooks: list[dict]) -> list[dict[str, str]]:
    """Instruction-tuning style examples for few-shot injection."""
    pairs: list[dict[str, str]] = []

    pairs.append(
        {
            "user": "Why is my position still open past stop loss?",
            "assistant": (
                "Check slcheck: Blofin needs an exchange SL order. If exchange_sltp=NO, "
                "nothing auto-closes. If SL exists, triggers often use last price not mark. "
                "This bot does not close on signal flip."
            ),
        }
    )
    pairs.append(
        {
            "user": "What leverage does this bot use?",
            "assistant": (
                "Mission is 50x where the instrument allows (SCALP_LEVERAGE_MAX). "
                "Per-symbol caps apply (e.g. some alts max 40x). Hourly maintenance closes "
                "positions below target leverage."
            ),
        }
    )
    pairs.append(
        {
            "user": "How do I restart the trading bot?",
            "assistant": "WhatsApp: text *restart*. Or run: .\\scripts\\stack_control.ps1 -Action restart",
        }
    )
    pairs.append(
        {
            "user": f"What is our live win rate?",
            "assistant": (
                f"From {stats.closes} closed trades: {stats.win_rate():.1f}% wins "
                f"({stats.wins}W / {stats.losses}L). Avg win {stats.avg_win_pct:.2f}% "
                f"vs avg loss {stats.avg_loss_pct:.2f}%."
            ),
        }
    )

    syms = set((stats.symbol_wins or {}).keys()) | set((stats.symbol_losses or {}).keys())
    qualified = [
        s for s in syms if (stats.symbol_wins or {}).get(s, 0) + (stats.symbol_losses or {}).get(s, 0) >= 5
    ]
    if qualified:
        best = max(
            qualified,
            key=lambda s: (stats.symbol_wins or {}).get(s, 0)
            / max(1, (stats.symbol_wins or {}).get(s, 0) + (stats.symbol_losses or {}).get(s, 0)),
        )
        w = (stats.symbol_wins or {}).get(best, 0)
        l = (stats.symbol_losses or {}).get(best, 0)
        pairs.append(
            {
                "user": "Which symbol has been best for us?",
                "assistant": (
                    f"{best} shows {w}W/{l}L in live outcomes — use normal size, "
                    "still respect Markov stress gate."
                ),
            }
        )

    for pb in playbooks:
        for d in (pb.get("doctrines") or [])[:2]:
            pairs.append(
                {
                    "user": f"What is the {d.get('title', 'rule')}?",
                    "assistant": d.get("summary", ""),
                }
            )

    return pairs


def _entry_score_insights(path: Path) -> list[str]:
    rows = [r for r in _read_jsonl(path) if r.get("event") == "entry"]
    if not rows:
        return []
    scores = [float(r.get("signal_score") or 0) for r in rows]
    avg = sum(scores) / len(scores)
    high = sum(1 for s in scores if s >= 90)
    return [
        f"- Logged entries: {len(rows)}",
        f"- Avg ML signal_score at entry: {avg:.1f}",
        f"- Entries with score>=90: {high} ({high/len(rows)*100:.0f}%)",
    ]


def train(state_dir: Path | None = None, *, force: bool = True) -> dict[str, Any]:
    """Rebuild cortex knowledge from disk. Returns summary dict."""
    state_dir = state_dir or ROOT / "state"
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)

    outcomes = state_dir / "trade_outcomes.jsonl"
    stats = _analyze_outcomes(outcomes)
    playbooks = _load_playbooks()

    knowledge = _build_knowledge_md(stats, playbooks)
    insights = _entry_score_insights(outcomes)
    if insights:
        knowledge += "\n\n## Entry quality (ML scores)\n" + "\n".join(insights)
    hr = state_dir / "hourly_report.json"
    if hr.is_file():
        try:
            rep = json.loads(hr.read_text(encoding="utf-8"))
            knowledge += (
                f"\n\n## Latest hourly\n"
                f"- equity={rep.get('equity')} opens={rep.get('open_count')} "
                f"optimizer={((rep.get('tuning') or {}).get('action'))}\n"
            )
        except Exception:
            pass
    KNOWLEDGE_MD.write_text(knowledge, encoding="utf-8")

    pairs = _synth_qa_pairs(stats, playbooks)
    with TRAIN_JSONL.open("w", encoding="utf-8") as fh:
        for p in pairs:
            fh.write(
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": p["user"]},
                            {"role": "assistant", "content": p["assistant"]},
                        ]
                    }
                )
                + "\n"
            )

    stats_blob = {
        "entries": stats.entries,
        "closes": stats.closes,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate_pct": round(stats.win_rate(), 2),
        "avg_win_pct": round(stats.avg_win_pct, 4),
        "avg_loss_pct": round(stats.avg_loss_pct, 4),
        "top_symbols": dict(Counter(stats.symbol_wins or {}).most_common(8)),
    }
    STATS_JSON.write_text(json.dumps(stats_blob, indent=2), encoding="utf-8")
    meta = {
        "trained_at": time.time(),
        "trained_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "examples": len(pairs),
        "knowledge_chars": len(knowledge),
        "outcomes_path": str(outcomes),
    }
    META_JSON.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info(
        "cortex trained: %s closes, %s examples, %s chars",
        stats.closes,
        len(pairs),
        len(knowledge),
    )
    return {**meta, **stats_blob}


def _enabled() -> bool:
    return os.environ.get("LOCAL_CORTEX_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def knowledge_block(max_chars: int | None = None) -> str:
    if not _enabled() or not KNOWLEDGE_MD.is_file():
        return ""
    raw = os.environ.get("LOCAL_CORTEX_MAX_KNOWLEDGE_CHARS", "5500")
    limit = max_chars or int(raw)
    text = KNOWLEDGE_MD.read_text(encoding="utf-8")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(cortex trimmed)"


def few_shot_messages(user_msg: str, max_examples: int | None = None) -> list[dict[str, str]]:
    """Pick training examples similar to the user question."""
    if not _enabled() or not TRAIN_JSONL.is_file():
        return []
    cap = max_examples or int(os.environ.get("LOCAL_CORTEX_MAX_EXAMPLES", "4"))
    rows = _read_jsonl(TRAIN_JSONL, limit=200)
    if not rows:
        return []

    q = user_msg.lower()
    tokens = set(re.findall(r"[a-z0-9]{3,}", q))

    def score(row: dict) -> int:
        msgs = row.get("messages") or []
        if len(msgs) < 2:
            return 0
        u = str(msgs[0].get("content", "")).lower()
        s = 0
        for t in tokens:
            if t in u:
                s += 2
        if "sl" in q or "stop" in q:
            if "stop" in u or "sl" in u:
                s += 5
        if "win" in q or "rate" in q:
            if "win" in u:
                s += 5
        if "restart" in q and "restart" in u:
            s += 8
        return s

    ranked = sorted(rows, key=score, reverse=True)
    out: list[dict[str, str]] = []
    for row in ranked[:cap]:
        msgs = row.get("messages") or []
        if len(msgs) >= 2:
            out.append({"role": "user", "content": str(msgs[0]["content"])})
            out.append({"role": "assistant", "content": str(msgs[1]["content"])})
    return out


def augmented_messages(
    base_system: str,
    live_snapshot: str,
    history: list[dict[str, str]],
    user_msg: str,
    *,
    profile: str = "full",
) -> list[dict[str, str]]:
    """Full message list with cortex knowledge + few-shot."""
    prof = profile.strip().lower()
    chat = prof == "chat"
    policy = prof == "policy"
    if policy:
        kb_limit = int(os.environ.get("LOCAL_CORTEX_POLICY_MAX_KNOWLEDGE_CHARS", "1400"))
        ex_cap = 0
        snap_cap = 2000
    elif chat:
        kb_limit = int(os.environ.get("LOCAL_CORTEX_CHAT_MAX_KNOWLEDGE_CHARS", "2200"))
        ex_cap = int(os.environ.get("LOCAL_CORTEX_CHAT_MAX_EXAMPLES", "2"))
        snap_cap = 4500
    else:
        kb_limit = int(os.environ.get("LOCAL_CORTEX_MAX_KNOWLEDGE_CHARS", "5500"))
        ex_cap = int(os.environ.get("LOCAL_CORTEX_MAX_EXAMPLES", "4"))
        snap_cap = 8000
    snap = live_snapshot
    if len(snap) > snap_cap:
        snap = snap[:snap_cap] + "\n...(snapshot trimmed)"

    messages: list[dict[str, str]] = [{"role": "system", "content": base_system}]
    kb = knowledge_block(kb_limit)
    if kb:
        messages.append({"role": "system", "content": f"TRAINED_CORTEX_KNOWLEDGE:\n{kb}"})
    messages.append({"role": "system", "content": f"LIVE_SNAPSHOT:\n{snap}"})
    messages.extend(few_shot_messages(user_msg, max_examples=ex_cap))
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    return messages
