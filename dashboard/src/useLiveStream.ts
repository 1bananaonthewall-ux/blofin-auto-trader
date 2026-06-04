import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ClosedTrade,
  PnlCurveData,
  Position,
  ScannerFeed,
  Signal,
  Status,
} from "./api";

export interface LiveSnapshot {
  stream_ts: string;
  status: Status;
  positions: Position[];
  active_setups: Signal[];
  developing_setups: Signal[];
  closed: ClosedTrade[];
  closed_updated_at?: string;
  trades_version?: number;
  scanner: ScannerFeed;
  pnl_curve: PnlCurveData;
  log_lines: string[];
  log_tail: string[];
  log_offset: number;
  errors?: Record<string, string>;
}

function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/live`;
}

export function useLiveStream() {
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [activeSignals, setActiveSignals] = useState<Signal[]>([]);
  const [developing, setDeveloping] = useState<Signal[]>([]);
  const [closed, setClosed] = useState<ClosedTrade[]>([]);
  const [scanner, setScanner] = useState<ScannerFeed | null>(null);
  const [pnlCurve, setPnlCurve] = useState<PnlCurveData | null>(null);
  const [pnlCurveError, setPnlCurveError] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  const logOffsetRef = useRef(0);
  const tradesVersionRef = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const applySnapshot = useCallback((snap: LiveSnapshot) => {
    if (snap.status) setStatus(snap.status);
    setPositions(snap.positions || []);
    setActiveSignals(snap.active_setups || []);
    setDeveloping(snap.developing_setups || []);
    setClosed(snap.closed ?? []);
    if (typeof snap.trades_version === "number" && snap.trades_version > 0) {
      tradesVersionRef.current = snap.trades_version;
    }
    if (snap.scanner) setScanner(snap.scanner);
    if (snap.pnl_curve) {
      setPnlCurve(snap.pnl_curve);
      const hasCurve = (snap.pnl_curve.equity?.length ?? 0) >= 2;
      if (hasCurve) {
        setPnlCurveError(null);
      }
    }
    const errs = snap.errors || {};
    const hasCurve = (snap.pnl_curve?.equity?.length ?? 0) >= 2;
    if (!hasCurve && (errs.equity || errs.positions || errs.api)) {
      setPnlCurveError(errs.equity || errs.positions || errs.api);
    } else if (errs.api && hasCurve) {
      setPnlCurveError(errs.api);
    } else if (hasCurve && !errs.api) {
      setPnlCurveError(null);
    }
    if (snap.log_tail?.length) {
      setLogLines(snap.log_tail);
      if (typeof snap.log_offset === "number") {
        logOffsetRef.current = snap.log_offset;
      }
    } else if (snap.log_lines?.length) {
      setLogLines((prev) => {
        const merged = [...prev, ...snap.log_lines];
        return merged.length > 2500 ? merged.slice(-2500) : merged;
      });
    }
    setStreamError(null);
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    try {
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        if (!mountedRef.current) return;
        setConnected(true);
        setStreamError(null);
      };
      ws.onmessage = (ev) => {
        if (!mountedRef.current) return;
        try {
          const msg = JSON.parse(ev.data as string) as {
            type: string;
            data?: LiveSnapshot;
          };
          if (
            (msg.type === "hello" || msg.type === "update" || msg.type === "heartbeat") &&
            msg.data
          ) {
            applySnapshot(msg.data);
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (!mountedRef.current) return;
        setConnected(false);
        wsRef.current = null;
        retryRef.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => {
        if (!mountedRef.current) return;
        setStreamError("WebSocket disconnected — reconnecting…");
        ws.close();
      };
    } catch (e) {
      setStreamError(e instanceof Error ? e.message : "WebSocket failed");
      retryRef.current = setTimeout(connect, 5000);
    }
  }, [applySnapshot]);

  useEffect(() => {
    const pollTrades = () => {
      fetch("/api/trades/closed?limit=24")
        .then((r) => (r.ok ? r.json() : null))
        .then(
          (data: {
            trades?: ClosedTrade[];
            trades_version?: number;
          } | null) => {
            if (!mountedRef.current || !data?.trades) return;
            const ver = data.trades_version ?? 0;
            if (ver > 0) {
              if (ver === tradesVersionRef.current) return;
              tradesVersionRef.current = ver;
            }
            setClosed(data.trades);
          }
        )
        .catch(() => {});
    };
    pollTrades();
    const tradesTimer = setInterval(pollTrades, 4000);
    return () => clearInterval(tradesTimer);
  }, []);

  useEffect(() => {
    const pollLogs = () => {
      const since = logOffsetRef.current;
      const q = since > 0 ? `since=${since}` : "n=400";
      fetch(`/api/logs?${q}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data: { lines?: string[]; offset?: number } | null) => {
          if (!data || !mountedRef.current) return;
          if (typeof data.offset === "number") logOffsetRef.current = data.offset;
          if (data.lines?.length) {
            setLogLines((prev) => {
              const merged = since > 0 ? [...prev, ...data.lines!] : data.lines!;
              return merged.length > 2500 ? merged.slice(-2500) : merged;
            });
          }
        })
        .catch(() => {});
    };
    pollLogs();
    const logTimer = setInterval(pollLogs, 2000);
    return () => clearInterval(logTimer);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    Promise.all([
      fetch("/api/live/snapshot").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/pnl-curve?range=ALL&limit=800").then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([snap, curve]) => {
        if (!mountedRef.current) return;
        if (snap) applySnapshot(snap as LiveSnapshot);
        if (curve?.equity?.length >= 2) {
          setPnlCurve(curve as PnlCurveData);
          setPnlCurveError(null);
        }
      })
      .catch(() => {});
    connect();
    return () => {
      mountedRef.current = false;
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return {
    connected,
    status,
    positions,
    activeSignals,
    developing,
    closed,
    scanner,
    pnlCurve,
    pnlCurveError,
    logLines,
    streamError,
  };
}
