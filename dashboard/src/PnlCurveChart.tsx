import { useEffect, useMemo, useRef, useState } from "react";
import { api, type PnlCurveData } from "./api";

const W = 720;
const H = 220;
const PAD = { top: 16, right: 12, bottom: 28, left: 56 };

const RANGE_OPTIONS = [
  { id: "H2", label: "1/12 Day" },
  { id: "H3", label: "1/8 Day" },
  { id: "H6", label: "1/4 Day" },
  { id: "H12", label: "1/2 Day" },
  { id: "1D", label: "1 Day" },
  { id: "3D", label: "3 Day" },
  { id: "1W", label: "1 Week" },
  { id: "1M", label: "1 Month" },
  { id: "3M", label: "3 Month" },
  { id: "6M", label: "6 Month" },
  { id: "ALL", label: "All Time" },
] as const;

type Range = (typeof RANGE_OPTIONS)[number]["id"];

const VALID_RANGES = new Set<string>(RANGE_OPTIONS.map((o) => o.id));

const LIMIT_BY_RANGE: Record<Range, number> = {
  H2: 480,
  H3: 540,
  H6: 720,
  H12: 960,
  "1D": 400,
  "3D": 480,
  "1W": 560,
  "1M": 640,
  "3M": 800,
  "6M": 1000,
  ALL: 1200,
};

const RANGE_LABEL: Record<Range, string> = Object.fromEntries(
  RANGE_OPTIONS.map((o) => [o.id, o.label])
) as Record<Range, string>;

const STORAGE_KEY = "godbot_pnl_curve_range";

function loadSavedRange(): Range {
  try {
    const s = localStorage.getItem(STORAGE_KEY);
    if (s && VALID_RANGES.has(s)) return s as Range;
  } catch {
    /* ignore */
  }
  return "ALL";
}

function fmtUsd(n: number) {
  const sign = n >= 0 ? "+" : "";
  return `${sign}$${n.toFixed(4)}`;
}

function fmtBalance(n: number) {
  return `$${n.toFixed(2)}`;
}

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

type Hover = {
  x: number;
  y: number;
  label: string;
  equity: number;
  ts: number;
};

function streamRangeOf(payload: PnlCurveData | null): Range | null {
  const r = payload?.range?.toUpperCase();
  if (r && VALID_RANGES.has(r)) return r as Range;
  return null;
}

/** Map screen coords to SVG viewBox (handles letterboxing from preserveAspectRatio). */
function clientToSvg(svg: SVGSVGElement, clientX: number, clientY: number): { x: number; y: number } | null {
  const ctm = svg.getScreenCTM();
  if (!ctm) return null;
  const pt = svg.createSVGPoint();
  pt.x = clientX;
  pt.y = clientY;
  const local = pt.matrixTransform(ctm.inverse());
  return { x: local.x, y: local.y };
}

export function PnlCurveChart({
  data: streamData,
  error,
  liveEquity,
}: {
  data: PnlCurveData | null;
  error?: string | null;
  liveEquity?: number | null;
}) {
  const [hover, setHover] = useState<Hover | null>(null);
  const [range, setRange] = useState<Range>(loadSavedRange);
  const [data, setData] = useState<PnlCurveData | null>(null);
  const [rangeError, setRangeError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const liveEquityRef = useRef(liveEquity);
  liveEquityRef.current = liveEquity;

  const selectRange = (r: Range) => {
    setRange(r);
    try {
      localStorage.setItem(STORAGE_KEY, r);
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    if (!streamData?.equity?.length) return;
    const streamRange = streamRangeOf(streamData);
    if (streamRange != null && streamRange !== range) return;
    setData(streamData);
    if (streamData.equity.length >= 2) {
      setLoading(false);
      setRangeError(null);
    }
  }, [streamData, range]);

  useEffect(() => {
    let cancelled = false;
    setRangeError(null);
    setLoading(true);
    const limit = LIMIT_BY_RANGE[range];
    api
      .pnlCurve(limit, range, liveEquityRef.current ?? undefined)
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setRangeError(e instanceof Error ? e.message : "curve fetch failed");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range]);

  useEffect(() => {
    const limit = LIMIT_BY_RANGE[range];
    const pollMs =
      range === "H2" || range === "H3" || range === "H6" || range === "H12"
        ? 4000
        : range === "1D"
          ? 8000
          : 15000;
    const t = setInterval(() => {
      api
        .pnlCurve(limit, range, liveEquityRef.current ?? undefined)
        .then((payload) => {
          if (payload.equity?.length >= 2) setData(payload);
        })
        .catch(() => {});
    }, pollMs);
    return () => clearInterval(t);
  }, [range]);

  const chart = useMemo(() => {
    if (!data?.equity?.length) return null;

    const points = data.equity;
    const minTs = points[0].ts;
    const maxTs = points[points.length - 1].ts;
    const tsSpan = maxTs - minTs || 1;

    const eqVals = points.map((p) => p.equity);
    let yMin = Math.min(...eqVals);
    let yMax = Math.max(...eqVals);
    const pad = Math.max((yMax - yMin) * 0.12, 0.02);
    yMin -= pad;
    yMax += pad;
    const ySpan = yMax - yMin || 1;

    const plotW = W - PAD.left - PAD.right;
    const plotH = H - PAD.top - PAD.bottom;

    const toX = (ts: number) => PAD.left + ((ts - minTs) / tsSpan) * plotW;
    const toY = (v: number) => PAD.top + plotH - ((v - yMin) / ySpan) * plotH;

    const equityPath = points
      .map((p, i) => `${i === 0 ? "M" : "L"} ${toX(p.ts).toFixed(2)} ${toY(p.equity).toFixed(2)}`)
      .join(" ");

    const areaPath = `${equityPath} L ${toX(points[points.length - 1].ts).toFixed(2)} ${(PAD.top + plotH).toFixed(2)} L ${toX(points[0].ts).toFixed(2)} ${(PAD.top + plotH).toFixed(2)} Z`;

    const baselines: { key: string; value: number; color: string; dash: string }[] = [];
    const addBaseline = (key: string, value: number | null | undefined, color: string, dash: string) => {
      if (value == null || !Number.isFinite(value)) return;
      baselines.push({ key, value, color, dash });
    };
    addBaseline("session", data.baselines.session_equity, "#6b7280", "4 4");
    addBaseline("day", data.baselines.day_equity, "#a855f7", "2 6");
    addBaseline("peak", data.baselines.peak_equity, "#ff8c00", "6 3");
    if (data.summary.range_start_equity != null) {
      addBaseline("range", data.summary.range_start_equity, "#22d3ee", "2 4");
    }

    const yTicks = 4;
    const yLabels = Array.from({ length: yTicks + 1 }, (_, i) => {
      const v = yMin + (ySpan * i) / yTicks;
      return { v, y: toY(v) };
    });

    const rangeBase =
      data.summary.range_start_equity ?? data.baselines.session_equity ?? points[0].equity;
    const realizedPoints = data.realized.filter((r) => r.ts >= minTs && r.ts <= maxTs);
    const realizedPath =
      realizedPoints.length > 0
        ? realizedPoints
            .map((r, i) => {
              const y = rangeBase + r.cumulative_pnl;
              return `${i === 0 ? "M" : "L"} ${toX(r.ts).toFixed(2)} ${toY(y).toFixed(2)}`;
            })
            .join(" ")
        : null;

    return {
      points,
      equityPath,
      areaPath,
      baselines,
      yLabels,
      realizedPath,
      toX,
      toY,
      plotW,
      plotH,
      yMin,
      yMax,
    };
  }, [data]);

  const blockingError =
    error && (!data?.equity?.length || data.equity.length < 2) ? error : null;

  if (blockingError) {
    return (
      <div className="pnl-chart panel">
        <div className="pnl-chart-empty">
          Account balance unavailable ({blockingError}). Restart dashboard:{" "}
          <code>powershell -File scripts\run_dashboard.ps1</code>
        </div>
      </div>
    );
  }

  if (loading && !chart) {
    return (
      <div className="pnl-chart panel">
        <div className="pnl-chart-head">
          <div className="pnl-range-tabs pnl-range-tabs--wrap">
            {RANGE_OPTIONS.map((o) => (
              <button
                key={o.id}
                type="button"
                className={`pnl-range-btn${range === o.id ? " active" : ""}`}
                onClick={() => selectRange(o.id)}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
        <div className="pnl-chart-empty">Loading {RANGE_LABEL[range]} balance…</div>
      </div>
    );
  }

  if (!chart) {
    const ticks = data?.summary.tick_count_raw ?? 0;
    return (
      <div className="pnl-chart panel">
        <div className="pnl-chart-head">
          <div className="pnl-range-tabs pnl-range-tabs--wrap">
            {RANGE_OPTIONS.map((o) => (
              <button
                key={o.id}
                type="button"
                className={`pnl-range-btn${range === o.id ? " active" : ""}`}
                onClick={() => selectRange(o.id)}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
        <div className="pnl-chart-empty">
          {ticks < 2
            ? `Not enough balance history for ${RANGE_LABEL[range]} yet (${ticks} tick${ticks === 1 ? "" : "s"}). Keep God Bot running to build the curve.`
            : "No equity ticks in this window — try a longer range or wait for more history."}
        </div>
      </div>
    );
  }

  const summary = data!.summary;
  const rangePnl = summary.pnl_vs_range ?? summary.pnl_vs_session;

  const plotLeft = PAD.left;
  const plotRight = W - PAD.right;

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = e.currentTarget;
    const local = clientToSvg(svg, e.clientX, e.clientY);
    if (!local) return;
    if (local.x < plotLeft - 12 || local.x > plotRight + 12) {
      setHover(null);
      return;
    }
    let best = chart.points[0];
    let bestDist = Infinity;
    for (const p of chart.points) {
      const px = chart.toX(p.ts);
      const d = Math.abs(px - local.x);
      if (d < bestDist) {
        bestDist = d;
        best = p;
      }
    }
    setHover({
      x: chart.toX(best.ts),
      y: chart.toY(best.equity),
      label: fmtTime(best.ts),
      equity: best.equity,
      ts: best.ts,
    });
  };

  return (
    <div className="pnl-chart panel">
      <div className="pnl-chart-head">
        <div>
          <h2 className="section-title" style={{ marginBottom: 4 }}>
            Account Curve
          </h2>
          <p className="disclaimer" style={{ margin: 0 }}>
            Live balance — bot maximizes this curve · {RANGE_LABEL[range]}
          </p>
        </div>
        <div className="pnl-range-tabs pnl-range-tabs--wrap">
          {RANGE_OPTIONS.map((o) => (
            <button
              key={o.id}
              type="button"
              className={`pnl-range-btn${range === o.id ? " active" : ""}`}
              onClick={() => selectRange(o.id)}
            >
              {o.label}
            </button>
          ))}
        </div>
        <div className="pnl-chart-balance-block">
          <div
            className={`pnl-chart-balance-caption${hover ? " at-cursor" : ""}`}
            title={hover ? "Equity at cursor" : "Live account equity"}
          >
            {hover ? hover.label : "live"}
          </div>
          <div className="pnl-chart-balance">
            {hover
              ? fmtBalance(hover.equity)
              : summary.current_equity != null
                ? fmtBalance(summary.current_equity)
                : liveEquity != null
                  ? fmtBalance(liveEquity)
                  : "—"}
          </div>
          <div className="pnl-chart-stats">
            <span className={rangePnl != null && rangePnl >= 0 ? "positive" : "negative"}>
              {RANGE_LABEL[range]}{" "}
              {rangePnl != null ? fmtUsd(rangePnl) : "—"}
            </span>
            <span
              className={
                summary.pnl_vs_session != null && summary.pnl_vs_session >= 0
                  ? "positive"
                  : "negative"
              }
            >
              24h {summary.pnl_vs_session != null ? fmtUsd(summary.pnl_vs_session) : "—"}
            </span>
            <span className={summary.total_realized_pnl >= 0 ? "positive" : "negative"}>
              realized {fmtUsd(summary.total_realized_pnl)}
            </span>
            {summary.drawdown_from_peak_pct != null && (
              <span className="negative">dd {summary.drawdown_from_peak_pct.toFixed(1)}%</span>
            )}
          </div>
        </div>
      </div>

      {(error || rangeError) && (
        <p className="disclaimer pnl-chart-warn">
          {error || rangeError} — showing cached tick history.
        </p>
      )}

      <div className="pnl-chart-svg-wrap">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height="100%"
          preserveAspectRatio="xMidYMid meet"
          className="pnl-chart-svg"
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        >
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#00e5ff" stopOpacity="0.25" />
            <stop offset="100%" stopColor="#00e5ff" stopOpacity="0" />
          </linearGradient>
        </defs>

        {chart.yLabels.map((t) => (
          <g key={t.v}>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={t.y}
              y2={t.y}
              stroke="#1a1a2e"
              strokeWidth={1}
            />
            <text x={PAD.left - 6} y={t.y + 4} textAnchor="end" className="pnl-axis-label">
              ${t.v.toFixed(2)}
            </text>
          </g>
        ))}

        {chart.baselines.map((b) => {
          const y = chart.toY(b.value);
          const inView = y >= PAD.top && y <= PAD.top + chart.plotH;
          return (
            <g key={b.key} opacity={inView ? 0.75 : 0.35}>
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={y}
                y2={y}
                stroke={b.color}
                strokeWidth={1}
                strokeDasharray={b.dash}
              />
            </g>
          );
        })}

        <path d={chart.areaPath} fill="url(#equityFill)" />
        <path d={chart.equityPath} fill="none" stroke="#00e5ff" strokeWidth={2} />

        {chart.realizedPath && (
          <path
            d={chart.realizedPath}
            fill="none"
            stroke="#00ff88"
            strokeWidth={1.5}
            strokeDasharray="3 2"
            opacity={0.9}
          />
        )}

        {hover && (
          <>
            <line
              x1={hover.x}
              x2={hover.x}
              y1={PAD.top}
              y2={PAD.top + chart.plotH}
              stroke="#00e5ff55"
              strokeWidth={1}
            />
            <circle cx={hover.x} cy={hover.y} r={4} fill="#00e5ff" stroke="#030308" strokeWidth={2} />
          </>
        )}
      </svg>
      </div>

      <div className="pnl-chart-legend">
        <span>
          <i className="leg cyan" /> account curve ({RANGE_LABEL[range]})
        </span>
        <span>
          <i className="leg green" /> realized (window)
        </span>
        <span>
          <i className="leg muted dash" /> 24h baseline
        </span>
        <span>
          <i className="leg purple dash" /> day baseline
        </span>
        <span>
          <i className="leg orange dash" /> peak (reference)
        </span>
        <span className="pnl-chart-meta">
          {summary.point_count} pts · {summary.tick_count_raw} ticks · {summary.trade_count} closes ·{" "}
          {summary.curve_phase || "—"}
        </span>
      </div>
    </div>
  );
}
