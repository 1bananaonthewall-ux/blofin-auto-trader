import { useEffect, useRef, useState } from "react";
import {
  api,
  ClosedTrade,
  PnlCurveData,
  Position,
  ScanPlan,
  ScannerFeed,
  Signal,
  Status,
} from "./api";
import { PnlCurveChart } from "./PnlCurveChart";
import { useLiveStream } from "./useLiveStream";

type Tab = "dashboard" | "scanner" | "logs" | "settings" | "chat";

function fmtUsd(n: number) {
  const sign = n >= 0 ? "+" : "";
  return `${sign}$${n.toFixed(2)}`;
}

function fmtPct(n: number) {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}%`;
}

function fmtTradeTime(ts?: number, closedAt?: string | null) {
  if (closedAt) {
    try {
      return new Date(closedAt).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      /* fall through */
    }
  }
  if (ts && ts > 0) {
    return new Date(ts * 1000).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return "—";
}

function symShort(s: string) {
  return s.split("/")[0] || s;
}

function DashboardView({
  status,
  pnlCurve,
  pnlCurveError,
  positions,
  activeSignals,
  developing,
  closed,
  search,
}: {
  status: Status | null;
  pnlCurve: PnlCurveData | null;
  pnlCurveError: string | null;
  positions: Position[];
  activeSignals: Signal[];
  developing: Signal[];
  closed: ClosedTrade[];
  search: string;
}) {
  const q = search.toUpperCase();
  const filterPos = q
    ? positions.filter((p) => p.symbol_short.toUpperCase().includes(q))
    : positions;
  const topSignal = activeSignals[0];

  return (
    <>
      {status && (
        <div className="mission-bar">
          <div className="stat">
            <span className="stat-label">Mission</span>
            <span className="stat-value">{status.mission}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Equity</span>
            <span className="stat-value">${status.equity.toFixed(4)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Free margin</span>
            <span className="stat-value">${status.free_margin.toFixed(4)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Unrealized</span>
            <span className={`stat-value ${(status.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}`}>
              {status.unrealized_pnl != null ? fmtUsd(status.unrealized_pnl) : "—"}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Open</span>
            <span className="stat-value">
              {positions.length > 0 ? positions.length : status.open_count}
            </span>
          </div>
        </div>
      )}

      <PnlCurveChart
        data={pnlCurve}
        error={pnlCurveError}
        liveEquity={status?.equity}
      />

      <section className="trades-stack">
        <h2 className="section-title">Live Trades</h2>
        <p className="disclaimer">Live Blofin positions · exchange + snapshot · auto-streamed every 2.5s</p>
        <div className="trades-grid">
          {filterPos.length === 0 && (
            <div className="trade-card"><span>No open positions</span></div>
          )}
          {filterPos.map((p) => (
            <div
              key={p.position_key || `${p.symbol}-${p.side}`}
              className={`trade-card ${topSignal && symShort(topSignal.symbol) === p.symbol_short ? "highlight" : ""}`}
            >
              <div className="hold-badge">HOLD</div>
              <div>
                <div className="sym-row">
                  <span className="sym">{p.symbol_short}</span>
                  <span className={p.side === "long" ? "side-long" : "side-short"}>
                    {p.side.toUpperCase()} {p.leverage}x
                  </span>
                </div>
                <div className={`pnl ${p.pnl_pct >= 0 ? "positive" : "negative"}`}>
                  ROE {fmtPct(p.pnl_pct)} / {fmtUsd(p.pnl_usd)}
                </div>
                <div style={{ fontSize: 10, color: "var(--muted)" }}>
                  entry {p.entry} · mark {p.mark}
                </div>
              </div>
            </div>
          ))}
        </div>

        <h2 className="section-title trades-stack-recent">Recent Trades</h2>
        <p className="disclaimer">Live closes from exchange outcomes + profitability · auto-streamed</p>
        <div className="trades-grid">
          {closed.length === 0 && (
            <div className="trade-card">
              <span>No closed trades yet — closes appear here automatically.</span>
            </div>
          )}
          {closed.slice(0, 12).map((t, i) => (
            <div key={`${t.symbol_short}-${t.ts ?? i}`} className="trade-card">
              <div className="hold-badge">DONE</div>
              <div>
                <div className="sym-row">
                  <span className="sym">{t.symbol_short}</span>
                  <span className={t.side === "long" ? "side-long" : "side-short"}>
                    {t.side?.toUpperCase()}
                    {t.leverage ? ` ${t.leverage}x` : ""}
                  </span>
                </div>
                <div className={`pnl ${(t.roe_pct ?? t.pnl_usd) >= 0 ? "positive" : "negative"}`}>
                  {t.roe_pct != null ? (
                    <>
                      ROE {fmtPct(t.roe_pct)} / {fmtUsd(t.pnl_usd)}
                    </>
                  ) : (
                    <>
                      {t.event ? `${t.event} · ` : ""}
                      {fmtUsd(t.pnl_usd)}
                      {t.pnl_pct != null ? ` (${fmtPct(t.pnl_pct)})` : ""}
                    </>
                  )}
                </div>
                <div style={{ fontSize: 10, color: "var(--muted)" }}>
                  {fmtTradeTime(t.ts, t.closed_at)}
                  {t.event ? ` · ${t.event}` : ""}
                  {t.entry != null ? ` · in ${t.entry}` : ""}
                  {t.exit != null ? ` → ${t.exit}` : ""}
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      <div className="grid-2">
        <div>
          <h2 className="section-title">Active Setups</h2>
          <p className="disclaimer">Top scan picks from bot.log · excludes open positions · auto-streamed every 2.5s</p>
          <div className="cards-row">
            {activeSignals.length === 0 && (
              <div className="signal-card">No recent scan picks — start God Bot or wait for next cycle.</div>
            )}
            {activeSignals.map((s, i) => (
              <div key={`${s.symbol}-${i}`} className={`signal-card ${i === 0 ? "featured" : ""}`}>
                <div className="sym">{symShort(s.symbol)}</div>
                <div className={`side-${s.side}`}>{s.side.toUpperCase()}</div>
                <div>Score {s.score.toFixed(0)} · conf {(s.confidence * 100).toFixed(0)}%</div>
                <div>{s.leverage ? `${s.leverage}x` : "—"} · {s.conviction ? `conv ${(s.conviction * 100).toFixed(0)}%` : "scanning"}</div>
              </div>
            ))}
          </div>

          <h2 className="section-title" style={{ marginTop: "1.25rem" }}>Developing Setups</h2>
          <p className="disclaimer">Next-tier scan candidates (not in top 6 active) · pick + confluence · auto-streamed every 2.5s</p>
          <div className="cards-row">
            {developing.length === 0 && (
              <div className="signal-card">
                No developing setups — wait for scan cycle or lower gates in optimizer.
              </div>
            )}
            {developing.slice(0, 8).map((s, i) => (
              <div key={`dev-${s.symbol}-${i}`} className="signal-card">
                <div className="sym">{symShort(s.symbol)}</div>
                <div className={`side-${s.side}`}>{s.side.toUpperCase()}</div>
                <div>
                  Pick {s.score.toFixed(0)}
                  {s.confluence_pct != null ? ` · CF ${s.confluence_pct.toFixed(0)}%` : ""}
                  {s.confidence ? ` · conf ${(s.confidence * 100).toFixed(0)}%` : ""}
                </div>
                <div style={{ fontSize: 10, color: "var(--muted)" }}>
                  {s.tier ? `tier ${s.tier}` : "scanning"}
                  {s.leverage ? ` · ${s.leverage}x` : ""}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div>
          <h2 className="section-title">BTC Trades</h2>
          <div className="panel sidebar-feed">
            {closed
              .filter((t) => t.symbol_short.startsWith("BTC"))
              .slice(0, 8)
              .map((t, i) => (
                <div key={i} className="feed-item">
                  <span className="sym">BTC</span> {t.side} {fmtUsd(t.pnl_usd)}
                </div>
              ))}
            {positions
              .filter((p) => p.symbol_short.startsWith("BTC"))
              .map((p) => (
                <div key={p.symbol} className="feed-item">
                  <span className="sym">BTC</span> {p.side} {fmtPct(p.pnl_pct)}
                </div>
              ))}
            {!closed.some((t) => t.symbol_short.startsWith("BTC")) &&
              !positions.some((p) => p.symbol_short.startsWith("BTC")) && (
                <div className="feed-item">No BTC activity in recent trades.</div>
              )}
          </div>

          <h2 className="section-title">Engine Intel</h2>
          <div className="panel sidebar-feed">
            {status?.hourly && Object.keys(status.hourly).length > 0 ? (
              Object.entries(status.hourly).map(([k, v]) => (
                <div key={k} className="feed-item">
                  {k}: {String(v)}
                </div>
              ))
            ) : (
              <div className="feed-item">Hourly optimizer report pending.</div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function ScanPlanBar({ plan }: { plan: ScanPlan | null }) {
  if (!plan) {
    return (
      <p className="disclaimer">
        Waiting for scan plan from God Bot — ensure the bot is running and logging to logs/bot.log.
      </p>
    );
  }
  return (
    <p className="disclaimer">
      Scan depth {plan.depth}/{plan.universe_n} · momentum {plan.momentum_slots} · rotation @
      {plan.rotation_offset} · stream {plan.stream_fresh ? "fresh" : "stale"} · coverage{" "}
      {plan.ticker_coverage_pct.toFixed(0)}%
    </p>
  );
}

function ScannerView({
  search,
  feed,
  live,
  error,
}: {
  search: string;
  feed: ScannerFeed | null;
  live: boolean;
  error: string | null;
}) {
  const picks = feed?.picks ?? [];
  const total = feed?.count ?? 0;
  const scanPlan = feed?.scan_plan ?? null;
  const updatedAt = feed?.updated_at ?? null;

  const q = search.toUpperCase();
  const rows = q
    ? picks.filter(
        (p) =>
          p.symbol_short.toUpperCase().includes(q) || p.symbol.toUpperCase().includes(q)
      )
    : picks;

  return (
    <>
      <h2 className="section-title">Live Scan Feed</h2>
      <ScanPlanBar plan={scanPlan} />
      <p className="disclaimer">
        {total} recent picks from bot.log · showing {rows.length}
        {updatedAt ? ` · updated ${new Date(updatedAt).toLocaleTimeString()}` : ""}
        {" · "}{live ? "WebSocket stream" : "connecting…"}
      </p>
      {error && <p className="disclaimer scanner-error">{error}</p>}
      <div className="panel panel-scroll-x">
        <table className="scanner-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Score</th>
              <th>Conf</th>
              <th>CF%</th>
              <th>Tier</th>
              <th>Lev</th>
              <th>Zone</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={8} style={{ color: "var(--muted)" }}>
                  No PICK/CONFLUENCE lines yet — start God Bot or wait for the next scan cycle.
                </td>
              </tr>
            )}
            {rows.map((p) => (
              <tr key={p.symbol}>
                <td className="sym-cell">{p.symbol_short}</td>
                <td className={p.side === "long" ? "side-long" : "side-short"}>
                  {p.side.toUpperCase()}
                </td>
                <td>
                  {(p.pick_pct ?? p.score ?? (p.pick_score != null ? p.pick_score * 100 : 0)).toFixed(0)}
                </td>
                <td>
                  {p.confidence != null ? `${(p.confidence * 100).toFixed(0)}%` : "—"}
                </td>
                <td>
                  {p.confluence_pct != null ? `${p.confluence_pct.toFixed(0)}%` : "—"}
                </td>
                <td>{p.tier || "—"}</td>
                <td>{p.leverage != null ? `${p.leverage}x` : "—"}</td>
                <td className="zone-cell" title={p.zone}>
                  {p.zone ? p.zone.slice(0, 42) + (p.zone.length > 42 ? "…" : "") : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function LogsView({
  lines,
  live,
  error,
}: {
  lines: string[];
  live: boolean;
  error: string | null;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickBottomRef = useRef(true);

  const scrollToBottom = () => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };

  useEffect(() => {
    if (stickBottomRef.current) {
      requestAnimationFrame(scrollToBottom);
    }
  }, [lines.length]);

  return (
    <div className="view-fill">
      <h2 className="section-title">God Bot Live Log</h2>
      <p className="disclaimer">
        Tailing logs/bot.log · {lines.length} lines · {live ? "WebSocket stream" : "connecting…"}
      </p>
      {error && <p className="disclaimer scanner-error">{error}</p>}
      <div className="log-view" ref={scrollRef} onScroll={onScroll}>
        {lines.length === 0 && (
          <div className="log-line muted">Waiting for log output…</div>
        )}
        {lines.map((line, i) => {
          let cls = "log-line";
          if (/PICK|CONFLUENCE|scan plan/i.test(line)) cls += " scan";
          else if (/ERROR|CRITICAL/i.test(line)) cls += " err";
          else if (/WARNING|WARN/i.test(line)) cls += " warn";
          else if (/INFO/i.test(line)) cls += " info";
          return (
            <div key={`${i}-${line.slice(0, 24)}`} className={cls}>
              {line}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SettingsView({
  onStack,
  stackBusy,
}: {
  onStack: (action: "start" | "stop" | "restart") => void;
  stackBusy: boolean;
}) {
  const [settings, setSettings] = useState<Record<string, unknown>>({});

  useEffect(() => {
    api.settings().then(setSettings).catch(console.error);
  }, []);

  return (
    <>
      <h2 className="section-title">Engine Settings</h2>
      <p className="disclaimer">Read-only snapshot · edit .env and hot-reload applies</p>
      <div className="settings-grid">
        {Object.entries(settings).map(([k, v]) => (
          <div key={k} className="setting-item">
            <div className="key">{k}</div>
            <div className="val">{String(v)}</div>
          </div>
        ))}
      </div>
      <div style={{ marginTop: "1rem", display: "flex", gap: "0.5rem" }}>
        <button className="btn btn-primary" onClick={() => onStack("start")} disabled={stackBusy}>
          Start Bot
        </button>
        <button className="btn" onClick={() => onStack("stop")} disabled={stackBusy}>
          Stop Bot
        </button>
        <button className="btn" onClick={() => onStack("restart")} disabled={stackBusy}>
          Restart
        </button>
      </div>
    </>
  );
}

const CHAT_SCROLL_BOTTOM_PX = 56;

function ChatView() {
  const [messages, setMessages] = useState<{ role: "user" | "bot"; text: string }[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [llmStatus, setLlmStatus] = useState<string>("");
  const messagesScrollRef = useRef<HTMLDivElement>(null);
  const chatBottomRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  const scrollChatToBottom = (behavior: ScrollBehavior = "smooth") => {
    const el = messagesScrollRef.current;
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior });
      return;
    }
    chatBottomRef.current?.scrollIntoView({ behavior, block: "end" });
  };

  const onChatScroll = () => {
    const el = messagesScrollRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distFromBottom <= CHAT_SCROLL_BOTTOM_PX;
  };

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const behavior: ScrollBehavior = loading ? "auto" : "smooth";
    const id = requestAnimationFrame(() => scrollChatToBottom(behavior));
    return () => cancelAnimationFrame(id);
  }, [messages, loading, historyLoaded]);

  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      api
        .getChatLlmStatus()
        .then((s) => {
          if (cancelled) return;
          if (s.status === "ready") {
            setLlmStatus("");
          } else if (s.status === "warming") {
            setLlmStatus("Warming local LLM for Copilot (first load can take 1–2 min)…");
          } else if (s.status === "none") {
            setLlmStatus("No local LLM configured — Copilot uses snapshot fallbacks only.");
          } else if (s.status === "error") {
            setLlmStatus(`LLM warmup error: ${s.last_error || s.detail}`);
          } else {
            setLlmStatus("Starting Copilot LLM…");
          }
        })
        .catch(() => {});
    };
    poll();
    const id = window.setInterval(poll, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .getChatHistory()
      .then(({ messages: rows }) => {
        if (cancelled) return;
        setMessages(
          rows.map((r) => ({
            role: r.role === "assistant" ? "bot" : "user",
            text: r.content,
          }))
        );
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setHistoryLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const send = async () => {
    if (!input.trim() || loading) return;
    const msg = input.trim();
    setInput("");
    stickToBottomRef.current = true;
    setMessages((m) => [...m, { role: "user", text: msg }]);
    setLoading(true);
    try {
      const reply = await api.chat(msg);
      setMessages((m) => [...m, { role: "bot", text: reply }]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: "bot", text: `Error: ${e instanceof Error ? e.message : e}` },
      ]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="view-fill">
      <h2 className="section-title">God Bot Copilot</h2>
      <p className="disclaimer">
        Local LLM · explains scans and positions · can edit bot code on your request (live reload).
        Does not place trades — the engine owns all entries and exits.
        {llmStatus ? ` ${llmStatus}` : ""}
      </p>
      <div className="chat-panel">
        <div className="chat-messages" ref={messagesScrollRef} onScroll={onChatScroll}>
          {historyLoaded && messages.length === 0 && (
            <div className="chat-msg bot">
              Hey — I&apos;m your dashboard copilot. Ask how we&apos;re doing, why a position is
              open, what the scanner is picking, or describe a code tweak and I&apos;ll patch it
              (no live orders from here).
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`chat-msg ${m.role}`}>
              {m.text}
            </div>
          ))}
          {loading && (
            <div className="chat-msg bot">
              {llmStatus && llmStatus.startsWith("Warming") ? llmStatus : "Thinking…"}
            </div>
          )}
          <div ref={chatBottomRef} className="chat-scroll-anchor" aria-hidden />
        </div>
        <div className="chat-input-row">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder="Ask God Bot…"
            disabled={loading}
          />
          <button className="btn btn-primary" onClick={send} disabled={loading}>
            {loading ? "…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [search, setSearch] = useState("");
  const [stackBusy, setStackBusy] = useState(false);
  const [stackMsg, setStackMsg] = useState<string | null>(null);
  const live = useLiveStream();

  useEffect(() => {
    let cancelled = false;
    const warmStackAfterReboot = async () => {
      try {
        const health = await fetch("/api/health");
        if (!health.ok) return;
        const data = (await health.json()) as { bot_running?: boolean };
        if (data.bot_running) return;
        if (cancelled) return;
        setStackMsg("Starting God Bot after reboot…");
        await fetch("/api/boot", { method: "POST" });
        const deadline = Date.now() + 120_000;
        while (!cancelled && Date.now() < deadline) {
          await new Promise((resolve) => window.setTimeout(resolve, 3000));
          try {
            const h = await fetch("/api/health");
            if (!h.ok) continue;
            const j = (await h.json()) as { bot_running?: boolean };
            if (j.bot_running) {
              setStackMsg("God Bot live — streaming market data");
              window.setTimeout(() => setStackMsg(null), 5000);
              return;
            }
          } catch {
            /* still warming */
          }
        }
        if (!cancelled) {
          setStackMsg("Bot warming up (local LLM load). Refresh again in ~1 min.");
        }
      } catch {
        /* health unavailable */
      }
    };
    void warmStackAfterReboot();
    return () => {
      cancelled = true;
    };
  }, []);

  const stopFullStack = async () => {
    setStackBusy(true);
    setStackMsg("Ctrl+F6 — stopping bot and dashboard…");
    try {
      const r = await api.stopStack();
      setStackMsg(r.message || "Stack stopping — this page will go offline.");
    } catch (e) {
      setStackMsg(e instanceof Error ? e.message : "Stop failed");
      setStackBusy(false);
    }
  };

  const cueAgentRepair = async () => {
    setStackBusy(true);
    setStackMsg("Ctrl+F7 — coding agent repair queued…");
    try {
      const r = await api.agentRepair();
      setStackMsg(
        r.message ||
          (r.already_running
            ? "Repair already running — will not stop until stack is online."
            : "Agent repair started.")
      );
      const deadline = Date.now() + 600_000;
      const poll = async () => {
        while (Date.now() < deadline) {
          await new Promise((resolve) => window.setTimeout(resolve, 5000));
          try {
            const st = await api.repairStatus();
            if (st.repair?.status === "done" || st.stack_ready) {
              setStackMsg("Stack online — repair complete. Reloading…");
              window.setTimeout(() => window.location.reload(), 1500);
              return;
            }
            const attempt = st.repair?.attempt;
            const err = st.repair?.last_error;
            setStackMsg(
              `Agent repair running${attempt != null ? ` (attempt ${attempt})` : ""}` +
                (err ? ` — ${err}` : " — will not stop until online")
            );
          } catch {
            /* dashboard may be down mid-repair */
          }
        }
        setStackMsg(
          "Repair still running in background. Reopen http://127.0.0.1:5050 when ready."
        );
        setStackBusy(false);
      };
      void poll();
    } catch (e) {
      setStackMsg(e instanceof Error ? e.message : "Agent repair failed");
      setStackBusy(false);
    }
  };

  const bootStack = async () => {
    setStackMsg("Ctrl+F5 — booting stack…");
    try {
      await fetch("/api/boot", { method: "POST" });
      const deadline = Date.now() + 120_000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
        const h = await fetch("/api/health");
        if (!h.ok) continue;
        const j = (await h.json()) as { bot_running?: boolean };
        if (j.bot_running) {
          setStackMsg("Stack booted — live");
          window.setTimeout(() => setStackMsg(null), 4000);
          return;
        }
      }
      setStackMsg("Boot triggered — HF warmup may take up to 2 min.");
    } catch {
      setStackMsg("Boot request failed");
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.ctrlKey || e.repeat) return;
      if (e.key === "F5") {
        e.preventDefault();
        void bootStack();
      } else if (e.key === "F6") {
        e.preventDefault();
        void stopFullStack();
      } else if (e.key === "F7") {
        e.preventDefault();
        void cueAgentRepair();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const runStack = async (action: "start" | "stop" | "restart" | "ensure") => {
    setStackBusy(true);
    setStackMsg(null);
    try {
      const r = await api.stack(action);
      if (r.async && action === "restart") {
        setStackMsg(
          r.output ||
            "Stack restart launched — waiting for bot + dashboard (up to 90s)…"
        );
        const deadline = Date.now() + 90_000;
        const poll = async () => {
          while (Date.now() < deadline) {
            await new Promise((resolve) => window.setTimeout(resolve, 3000));
            try {
              const h = await fetch("/api/health");
              if (!h.ok) continue;
              const data = (await h.json()) as { bot_running?: boolean };
              if (data.bot_running) {
                setStackMsg("Stack is up — reloading dashboard…");
                window.location.reload();
                return;
              }
            } catch {
              /* dashboard down briefly during restart */
            }
          }
          setStackMsg(
            "Restart was triggered but stack did not come back in 90s. From home run: " +
              "powershell -File C:\\Users\\mknig\\blofin-auto-trader\\scripts\\stack_control.ps1 -Action restart"
          );
          setStackBusy(false);
        };
        void poll();
        return;
      }
      const line =
        r.output
          ?.split("\n")
          .map((s) => s.trim())
          .filter(Boolean)
          .slice(-2)
          .join(" · ") || `${action} ok`;
      setStackMsg(line);
    } catch (e) {
      setStackMsg(e instanceof Error ? e.message : `${action} failed`);
    } finally {
      setStackBusy(false);
    }
  };

  const tabs: { id: Tab; label: string }[] = [
    { id: "dashboard", label: "Terminal" },
    { id: "scanner", label: "Scanner" },
    { id: "logs", label: "Logs" },
    { id: "settings", label: "Settings" },
    { id: "chat", label: "Copilot" },
  ];

  const botLive = live.status?.bot_running ?? false;
  const streamOk = live.connected;

  return (
    <div className="app">
      <header className="header">
        <div className="logo">
          GOD<span>BOT</span>
        </div>
        <nav className="nav">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={tab === t.id ? "active" : ""}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="header-right">
          <input
            className="search"
            placeholder="Search symbol…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="live-pill" title={streamOk ? "WebSocket connected" : "Reconnecting…"}>
            <span className={`live-dot ${streamOk && botLive ? "" : "off"}`} />
            {streamOk ? (botLive ? "streaming" : "bot offline") : "connecting"}
          </div>
          <button
            className="btn btn-primary"
            onClick={() => runStack("restart")}
            disabled={stackBusy}
            title="Kill all bot.py instances, restart dashboard + single fresh bot"
          >
            {stackBusy ? "Restarting…" : "Restart Bot"}
          </button>
        </div>
      </header>

      {stackMsg && (
        <div className={`stream-banner ${stackMsg.includes("failed") || stackMsg.includes("FAIL") ? "warn" : ""}`}>
          {stackMsg}
        </div>
      )}

      {live.streamError && (
        <div className="stream-banner">{live.streamError}</div>
      )}

      <main className={`main${tab === "logs" || tab === "chat" ? " main--fill" : ""}`}>
        {tab === "dashboard" && (
          <DashboardView
            status={live.status}
            pnlCurve={live.pnlCurve}
            pnlCurveError={live.pnlCurveError}
            positions={live.positions}
            activeSignals={live.activeSignals}
            developing={live.developing}
            closed={live.closed}
            search={search}
          />
        )}
        {tab === "scanner" && (
          <ScannerView
            search={search}
            feed={live.scanner}
            live={live.connected}
            error={live.streamError}
          />
        )}
        {tab === "logs" && (
          <LogsView lines={live.logLines} live={live.connected} error={live.streamError} />
        )}
        {tab === "settings" && <SettingsView onStack={runStack} stackBusy={stackBusy} />}
        {tab === "chat" && <ChatView />}
      </main>

      <footer className="footer">
        © {new Date().getFullYear()} God Bot · Blofin Auto Trader · {streamOk ? "ws://live" : "offline"}
        {" · "}
        <span className="footer-hotkeys">Ctrl+F5 boot · Ctrl+F6 stop stack · Ctrl+F7 agent repair</span>
      </footer>
    </div>
  );
}
