import { useState } from "react";
import { Bot, BacktestResultRow, BacktestRun, fetchBacktestPine, runBacktest } from "./api";
import { TradingViewChart } from "./TradingViewChart";

function MiniEquity({ curve }: { curve: { ts: number; equity: number }[] }) {
  if (!curve.length) return null;
  const vals = curve.map((c) => c.equity);
  const max = Math.max(...vals);
  const min = Math.min(...vals);
  const range = max - min || 1;
  const w = 120;
  const h = 36;
  const pts = curve.map((c, i) => {
    const x = (i / (curve.length - 1)) * w;
    const y = h - ((c.equity - min) / range) * h;
    return `${x},${y}`;
  });
  return (
    <svg width={w} height={h} style={{ display: "block" }}>
      <polyline fill="none" stroke="#3dd6c6" strokeWidth="2" points={pts.join(" ")} />
    </svg>
  );
}

type Props = {
  bots: Bot[];
  defaultBotSlug?: string;
};

function isoDate(d: Date) {
  return d.toISOString().slice(0, 10);
}

const TODAY = isoDate(new Date());
const DEFAULT_START = isoDate(new Date(Date.now() - 365 * 24 * 60 * 60 * 1000));
const MIN_START = isoDate(new Date(Date.now() - 730 * 24 * 60 * 60 * 1000));

function daysBetween(start: string, end: string) {
  const a = new Date(start + "T00:00:00Z").getTime();
  const b = new Date(end + "T00:00:00Z").getTime();
  return Math.round((b - a) / (24 * 60 * 60 * 1000));
}

export function BacktestLab({ bots, defaultBotSlug }: Props) {
  const [botSlug, setBotSlug] = useState(defaultBotSlug || bots[0]?.slug || "god-bot-scalper-pro");
  const [startingPot, setStartingPot] = useState(1000);
  const [startDate, setStartDate] = useState(DEFAULT_START);
  const [endDate, setEndDate] = useState(TODAY);
  const bar = "5m"; // confluence engine: 5m signals + 1H HTF
  const [maxAssets, setMaxAssets] = useState(40);
  const [loading, setLoading] = useState(false);
  const [run, setRun] = useState<BacktestRun | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<BacktestResultRow | null>(null);
  const [pine, setPine] = useState("");
  const [showPine, setShowPine] = useState(false);

  const tvInterval = "5";

  const spanDays = daysBetween(startDate, endDate);

  const validateDates = () => {
    if (startDate >= endDate) return "Start date must be before end date.";
    if (spanDays > 730) return "Max range is 730 days (2 years).";
    if (spanDays < 7) return "Min range is 7 days.";
    if (startDate < MIN_START) return `Earliest start is ${MIN_START}.`;
    if (endDate > TODAY) return "End date cannot be in the future.";
    return "";
  };

  const execute = async () => {
    const dateErr = validateDates();
    if (dateErr) {
      setError(dateErr);
      return;
    }
    setLoading(true);
    setError("");
    setSelected(null);
    try {
      const r = await runBacktest({
        bot_slug: botSlug,
        starting_pot: startingPot,
        start_date: startDate,
        end_date: endDate,
        bar,
        max_assets: maxAssets,
      });
      if (r.error) {
        setError(r.error);
        setRun(null);
      } else {
        setRun(r);
        if (r.results?.length) setSelected(r.results[0]);
      }
    } catch {
      setError("Backtest failed — check API is running and network is up.");
    } finally {
      setLoading(false);
    }
  };

  const loadPine = async () => {
    const r = await fetchBacktestPine(botSlug, startingPot);
    setPine(r.pine);
    setShowPine(true);
  };

  return (
    <section className="section backtest-lab">
      <h2>Backtest Lab</h2>
      <p className="sub">
        Run Bob&apos;s Bots strategy across all Blofin USDT perps. Set <strong>start &amp; end dates</strong> (up to 2 years apart), your starting pot, and bot profile,
        then inspect any asset in TradingView. Bot dropdowns tune <strong>risk &amp; filters</strong> (stop/take, size, runner threshold) —
        each bot uses real <code>ta_confluence.py</code> (15+ votes on 5m/1H) with different gates. God Bot matches live adaptive strict mode.
      </p>

      <div className="backtest-controls card">
        <div className="backtest-controls-grid">
          <div>
            <label>Bot strategy</label>
            <select value={botSlug} onChange={(e) => setBotSlug(e.target.value)}>
              {bots.map((b) => (
                <option key={b.slug} value={b.slug}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label>Starting pot ($)</label>
            <input
              type="number"
              min={10}
              max={1000000}
              step={50}
              value={startingPot}
              onChange={(e) => setStartingPot(Number(e.target.value))}
            />
          </div>
          <div>
            <label>Start date</label>
            <input
              type="date"
              min={MIN_START}
              max={endDate}
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
            />
          </div>
          <div>
            <label>End date</label>
            <input
              type="date"
              min={startDate}
              max={TODAY}
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
            />
            <small style={{ color: "var(--muted)", fontSize: "0.75rem" }}>
              {spanDays} days selected (7–730)
            </small>
          </div>
          <div>
            <label>Engine</label>
            <input readOnly value="5m confluence + 1H HTF" style={{ opacity: 0.85 }} />
          </div>
          <div>
            <label>Assets (top by volume)</label>
            <select value={maxAssets} onChange={(e) => setMaxAssets(Number(e.target.value))}>
              <option value={20}>20</option>
              <option value={40}>40</option>
              <option value={60}>60</option>
              <option value={80}>80</option>
            </select>
          </div>
        </div>
        <div className="hero-cta" style={{ marginTop: "1rem", justifyContent: "flex-start" }}>
          <button className="btn btn-primary" onClick={execute} disabled={loading}>
            {loading ? "Running backtest…" : "Run on all assets"}
          </button>
          <button className="btn btn-ghost" onClick={loadPine} type="button">
            Get TradingView Pine script
          </button>
        </div>
        {error && <p style={{ color: "var(--danger)", marginTop: "0.75rem" }}>{error}</p>}
        {run && (
          <p style={{ marginTop: "0.75rem", fontSize: "0.85rem", color: "var(--muted)" }}>
            {run.period_start} → {run.period_end} · {run.assets_tested} assets · ${run.starting_pot} start
            {run.from_cache ? " · cached" : ""}
          </p>
        )}
      </div>

      {showPine && pine && (
        <div className="card" style={{ marginTop: "1rem" }}>
          <h3>TradingView Pine (Strategy Tester)</h3>
          <p className="tagline">Paste in Pine Editor, add to chart, open Strategy Tester with your date range and starting capital.</p>
          <textarea readOnly rows={12} value={pine} style={{ fontFamily: "var(--mono)", fontSize: "0.75rem" }} />
          <button className="btn btn-ghost" style={{ marginTop: "0.5rem" }} onClick={() => navigator.clipboard.writeText(pine)}>
            Copy script
          </button>
        </div>
      )}

      {run && run.results.length > 0 && (
        <>
          <div className="table-wrap" style={{ marginTop: "1.5rem" }}>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Asset</th>
                  <th>Return</th>
                  <th>End equity</th>
                  <th>Max DD</th>
                  <th>Trades</th>
                  <th>Win%</th>
                  <th>Curve</th>
                  <th>TV</th>
                </tr>
              </thead>
              <tbody>
                {run.results.map((r) => (
                  <tr
                    key={r.inst_id}
                    className={selected?.inst_id === r.inst_id ? "row-selected" : ""}
                    onClick={() => setSelected(r)}
                    style={{ cursor: "pointer" }}
                  >
                    <td className="rank-num">{r.rank}</td>
                    <td>
                      <strong>{r.base}</strong>
                      <br />
                      <small style={{ color: "var(--muted)" }}>{r.symbol}</small>
                    </td>
                    <td style={{ color: r.return_pct >= 0 ? "var(--ok)" : "var(--danger)" }}>
                      {r.return_pct >= 0 ? "+" : ""}
                      {r.return_pct}%
                    </td>
                    <td>${r.ending_equity.toLocaleString()}</td>
                    <td>{r.max_drawdown_pct}%</td>
                    <td>{r.trades}</td>
                    <td>{r.win_rate_pct}%</td>
                    <td>
                      <MiniEquity curve={r.equity_curve} />
                    </td>
                    <td>
                      <a href={r.tradingview_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                        Open
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {selected && (
            <div className="card" style={{ marginTop: "1.5rem" }}>
              <h3>
                {selected.base} — ${startingPot} → ${selected.ending_equity.toLocaleString()} ({selected.return_pct}%)
              </h3>
              <p className="tagline">
                TradingView chart · {bar} bars · click row to switch asset
              </p>
              <TradingViewChart symbol={selected.tradingview} interval={tvInterval} height={440} />
              <div style={{ marginTop: "1rem" }}>
                <MiniEquity curve={selected.equity_curve} />
                <span style={{ fontSize: "0.8rem", color: "var(--muted)", marginLeft: "0.5rem" }}>Simulated equity (Bob&apos;s Bots engine)</span>
              </div>
            </div>
          )}
        </>
      )}

      <p className="disclaimer" style={{ marginTop: "1.5rem" }}>
        {run?.disclaimer ||
          "Historical simulation on Blofin OHLCV — not financial advice. Operated by Matthew Anthony Knight. Past backtests do not guarantee live results. You may lose money trading leveraged crypto."}
      </p>
    </section>
  );
}
