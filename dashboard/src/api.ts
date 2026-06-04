export interface Status {
  mission: string;
  equity: number;
  free_margin: number;
  used_margin: number;
  exposure_usdt?: number;
  unrealized_pnl?: number;
  daily_pnl?: number | null;
  monthly_pnl?: number | null;
  session_pnl?: number | null;
  open_count: number;
  bot_running: boolean;
  live: boolean;
  mode: string;
  dry_run: boolean;
  curve_phase?: string;
  verticality?: number;
  progress_log_pct: number;
  progress_today_pct?: number;
  progress_acceleration_pct?: number;
  today_growth_pct?: number;
  target_daily_growth_pct?: number;
  hourly?: Record<string, unknown>;
  updated_at: string;
}

export interface Position {
  position_key?: string;
  symbol: string;
  symbol_short: string;
  side: string;
  entry: number;
  mark: number;
  leverage: number;
  pnl_pct: number;
  pnl_usd: number;
  margin_usdt?: number;
  notional_usdt?: number;
  liquidation_price?: number;
  conviction?: number;
  sl_price?: number;
  tp_price?: number;
  status: string;
}

export interface Signal {
  symbol: string;
  side: string;
  score: number;
  confidence: number;
  leverage?: number;
  conviction?: number;
  confluence_pct?: number;
  tier?: string;
}

export interface ClosedTrade {
  symbol?: string;
  symbol_short: string;
  side: string;
  pnl_usd: number;
  pnl_pct?: number | null;
  roe_pct?: number | null;
  event?: string;
  entry?: number | null;
  exit?: number | null;
  leverage?: number | null;
  ts?: number;
  closed_at?: string | null;
  source?: string;
}

export interface Ticker {
  symbol: string;
  symbol_short: string;
  last: number;
  change_24h_pct: number;
  volume_24h: number;
}

export interface ScanPick {
  symbol: string;
  symbol_short: string;
  side: string;
  score?: number;
  pick_score?: number;
  fast_score?: number;
  confidence?: number;
  confluence_pct?: number | null;
  tier?: string;
  zone?: string;
  agree?: number;
  oppose?: number;
  leverage?: number | null;
  status?: string;
}

export interface ScanPlan {
  depth: number;
  universe_n: number;
  momentum_slots: number;
  rotation_offset: number;
  stream_fresh: boolean;
  ticker_coverage_pct: number;
}

export interface ScannerFeed {
  picks: ScanPick[];
  count: number;
  scan_plan: ScanPlan | null;
  source: string;
  updated_at: string;
}

export interface LogTail {
  lines: string[];
  count: number;
  offset: number;
  path: string;
}

export interface PnlCurvePoint {
  ts: number;
  equity: number;
}

export interface RealizedPoint {
  ts: number;
  cumulative_pnl: number;
}

export interface PnlCurveData {
  equity: PnlCurvePoint[];
  range?: string;
  realized: RealizedPoint[];
  baselines: {
    day_equity: number | null;
    session_equity: number | null;
    peak_equity: number | null;
  };
  summary: {
    current_equity: number | null;
    total_realized_pnl: number;
    pnl_vs_day: number | null;
    pnl_vs_session: number | null;
    pnl_vs_range: number | null;
    range_start_equity: number | null;
    drawdown_from_peak_pct: number | null;
    curve_phase?: string;
    verticality?: number;
    point_count: number;
    tick_count_raw: number;
    trade_count: number;
  };
  updated_at: string;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { error?: string }).error || res.statusText);
  }
  return res.json();
}

export const api = {
  status: () => get<Status>("/api/status"),
  pnlCurve: (limit?: number, range = "ALL", liveEquity?: number) => {
    const q = new URLSearchParams({ range });
    if (limit != null) q.set("limit", String(limit));
    if (liveEquity != null && liveEquity > 0) {
      q.set("live_equity", String(liveEquity));
    }
    return get<PnlCurveData>(`/api/pnl-curve?${q}`);
  },
  positions: () => get<{ positions: Position[] }>("/api/positions"),
  signals: () =>
    get<{ active_setups: Signal[]; developing_setups: Signal[] }>("/api/signals"),
  closedTrades: (limit = 24, hours = 0) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (hours > 0) q.set("hours", String(hours));
    return get<{
      trades: ClosedTrade[];
      count: number;
      trades_version?: number;
      updated_at?: string;
    }>(`/api/trades/closed?${q}`);
  },
  tickers: (q = "", sort = "change", limit = 500, offset = 0) =>
    get<{ tickers: Ticker[]; total: number; universe_total: number }>(
      `/api/tickers?q=${encodeURIComponent(q)}&sort=${sort}&limit=${limit}&offset=${offset}`
    ),
  scanner: (limit = 48) => get<ScannerFeed>(`/api/scanner?limit=${limit}`),
  logs: (n = 120, since?: number) => {
    const q = since != null ? `since=${since}` : `n=${n}`;
    return get<LogTail>(`/api/logs?${q}`);
  },
  settings: () => get<Record<string, unknown>>("/api/settings"),
  getChatHistory: () =>
    get<{ messages: { role: string; content: string }[] }>("/api/chat/history"),
  getChatLlmStatus: () =>
    get<{
      provider: string;
      status: string;
      detail: string;
      timeout_sec: number;
      last_error?: string;
    }>("/api/chat/llm"),
  chat: async (message: string, history?: { role: string; content: string }[]) => {
    const controller = new AbortController();
    // Server default 180s generation + warmup; keep client slightly above.
    const timer = setTimeout(() => controller.abort(), 210_000);
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
        signal: controller.signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "chat failed");
      return data.reply as string;
    } finally {
      clearTimeout(timer);
    }
  },
  stack: async (action: "start" | "stop" | "restart" | "status") => {
    const res = await fetch(`/api/stack/${action}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      throw new Error((data as { error?: string }).error || `stack ${action} failed`);
    }
    const out = data as {
      ok?: boolean;
      async?: boolean;
      output?: string;
      error?: string;
      bot_running?: boolean;
    };
    if (out.ok === false) {
      throw new Error(out.error || out.output || `stack ${action} failed`);
    }
    return out;
  },
};
