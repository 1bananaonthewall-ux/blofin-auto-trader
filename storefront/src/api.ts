const API = "";

export type Bot = {
  id: string;
  slug: string;
  name: string;
  tagline: string;
  tier: string;
  rank: number;
  price_usd: number;
  monthly_usd?: number;
  difficulty: string;
  install_minutes: number;
  highlights: string[];
  backtest: Record<string, number | string>;
};

export type Package = {
  id: string;
  name: string;
  price_usd: number;
  original_usd: number;
  bot_ids: string[];
  includes: string[];
  badge?: string;
};

export type TaStack = {
  summary: string;
  core_ta?: { label: string; methods: string[]; confluence?: string };
  tier_extras?: Record<string, string[]>;
  backtest_lab_note?: string;
};

export type LegalContent = {
  operator: string;
  brand: string;
  short_disclaimer: string;
  footer_line: string;
  sections: { title: string; body: string }[];
};

export type Catalog = {
  brand: string;
  operator?: string;
  tagline?: string;
  disclaimer: string;
  bots: Bot[];
  packages: Package[];
  deals: { code: string; pct_off: number; label: string }[];
  rankings: RankingRow[];
  ta_stack?: TaStack;
  legal?: LegalContent;
};

export type RankingRow = {
  rank: number;
  slug: string;
  name: string;
  tier: string;
  total_return_pct: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  win_rate_pct: number;
  profit_factor: number;
  risk_scale: number;
  profit_scale: number;
  price_usd: number;
  difficulty: string;
};

export async function fetchCatalog(): Promise<Catalog> {
  const r = await fetch(`${API}/api/catalog`);
  if (!r.ok) throw new Error("catalog failed");
  return r.json();
}

export async function fetchBot(slug: string) {
  const r = await fetch(`${API}/api/bots/${slug}`);
  if (!r.ok) throw new Error("bot not found");
  return r.json() as Promise<{ bot: Bot; equity_curve: { i: number; equity: number; benchmark_btc: number }[] }>;
}

export async function quote(body: { bot_slugs?: string[]; package_id?: string; promo_code?: string }) {
  const r = await fetch(`${API}/api/quote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function checkout(body: Record<string, unknown>) {
  const r = await fetch(`${API}/api/checkout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function concierge(message: string, sessionId: string, email?: string) {
  const r = await fetch(`${API}/api/concierge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId, email }),
  });
  return r.json() as Promise<{ reply: string; actions: unknown[]; session_id: string }>;
}

export async function requestRefund(orderId: string, reason: string) {
  const r = await fetch(`${API}/api/refunds`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order_id: orderId, reason }),
  });
  return r.json();
}

export async function fetchOrder(orderId: string, token?: string) {
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  const r = await fetch(`${API}/api/orders/${orderId}${q}`);
  return r.json();
}

export type BacktestAsset = {
  inst_id: string;
  symbol: string;
  base: string;
  tradingview: string;
  vol24h: number;
};

export type BacktestResultRow = {
  rank: number;
  inst_id: string;
  symbol: string;
  base: string;
  tradingview: string;
  tradingview_url: string;
  starting_pot: number;
  ending_equity: number;
  return_pct: number;
  max_drawdown_pct: number;
  trades: number;
  win_rate_pct: number;
  equity_curve: { ts: number; equity: number }[];
};

export type BacktestRun = {
  bot_slug: string;
  starting_pot: number;
  lookback_days: number;
  start_date: string;
  end_date: string;
  bar: string;
  period_start: string;
  period_end: string;
  assets_tested: number;
  results: BacktestResultRow[];
  from_cache?: boolean;
  disclaimer: string;
  error?: string;
};

export async function fetchBacktestAssets() {
  const r = await fetch(`${API}/api/backtest/assets`);
  return r.json() as Promise<{ count: number; assets: BacktestAsset[]; max_lookback_days: number }>;
}

export async function runBacktest(body: {
  bot_slug: string;
  starting_pot: number;
  start_date: string;
  end_date: string;
  bar: string;
  max_assets?: number;
}) {
  const r = await fetch(`${API}/api/backtest/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json() as Promise<BacktestRun & { error?: string }>;
}

export async function fetchBacktestPine(botSlug: string, startingPot: number) {
  const q = new URLSearchParams({
    bot_slug: botSlug,
    starting_pot: String(startingPot),
  });
  const r = await fetch(`${API}/api/backtest/pine?${q}`);
  return r.json() as Promise<{ pine: string; instructions: string }>;
}
