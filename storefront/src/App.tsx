import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bot,
  Catalog,
  Package,
  RankingRow,
  checkout,
  concierge,
  fetchBot,
  fetchCatalog,
  fetchOrder,
  quote,
  requestRefund,
} from "./api";
import { BacktestLab } from "./BacktestLab";
import { DEFAULT_LEGAL, DisclaimerBanner, DisclaimerFooter, LegalPage } from "./Legal";

type View = "home" | "rankings" | "bots" | "bot" | "packages" | "backtest" | "checkout" | "support" | "legal" | "order";

type ChatMsg = { role: "user" | "bot"; text: string };

function EquityChart({ data }: { data: { equity: number; benchmark_btc: number }[] }) {
  if (!data.length) return null;
  const max = Math.max(...data.map((d) => Math.max(d.equity, d.benchmark_btc)));
  const min = Math.min(...data.map((d) => Math.min(d.equity, d.benchmark_btc)));
  const range = max - min || 1;
  const w = 400;
  const h = 160;
  const pts = data.map((d, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((d.equity - min) / range) * h;
    return `${x},${y}`;
  });
  const btc = data.map((d, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((d.benchmark_btc - min) / range) * h;
    return `${x},${y}`;
  });
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="chart" preserveAspectRatio="none">
      <polyline fill="none" stroke="rgba(139,155,180,0.5)" strokeWidth="2" points={btc.join(" ")} />
      <polyline fill="none" stroke="#3dd6c6" strokeWidth="2.5" points={pts.join(" ")} />
    </svg>
  );
}

function BotCard({ bot, onSelect }: { bot: Bot; onSelect: (slug: string) => void }) {
  const bt = bot.backtest;
  return (
    <article className="card">
      <span className={`badge tier-${bot.tier.toLowerCase()}`}>Tier {bot.tier}</span>
      <h3>{bot.name}</h3>
      <p className="tagline">{bot.tagline}</p>
      <div className="metrics">
        <div className="metric">
          <span>Backtest return</span>
          <strong>+{bt.total_return_pct}%</strong>
        </div>
        <div className="metric">
          <span>Max DD</span>
          <strong>{bt.max_drawdown_pct}%</strong>
        </div>
        <div className="metric">
          <span>Sharpe</span>
          <strong>{bt.sharpe}</strong>
        </div>
      </div>
      <div className="scales">
        <div style={{ flex: 1 }}>
          <div className="scale-bar">
            <div className="scale-fill" style={{ width: `${(bt.profit_scale as number) * 10}%` }} />
          </div>
          <div className="scale-label">Profit scale {bt.profit_scale}/10</div>
        </div>
        <div style={{ flex: 1 }}>
          <div className="scale-bar">
            <div className="scale-fill" style={{ width: `${(bt.risk_scale as number) * 10}%`, background: "var(--accent2)" }} />
          </div>
          <div className="scale-label">Risk scale {bt.risk_scale}/10</div>
        </div>
      </div>
      <p className="price" style={{ marginTop: "1rem" }}>
        ${bot.price_usd}
      </p>
      <button className="btn btn-primary" style={{ marginTop: "0.75rem", width: "100%" }} onClick={() => onSelect(bot.slug)}>
        View backtest & buy
      </button>
    </article>
  );
}

function RankingsTable({ rows }: { rows: RankingRow[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Bot</th>
            <th>Return</th>
            <th>CAGR</th>
            <th>Sharpe</th>
            <th>Win%</th>
            <th>Profit</th>
            <th>Risk</th>
            <th>Price</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.slug}>
              <td className="rank-num">{r.rank}</td>
              <td>
                <strong>{r.name}</strong>
                <br />
                <small style={{ color: "var(--muted)" }}>Tier {r.tier}</small>
              </td>
              <td style={{ color: "var(--ok)" }}>+{r.total_return_pct}%</td>
              <td>{r.cagr_pct}%</td>
              <td>{r.sharpe}</td>
              <td>{r.win_rate_pct}%</td>
              <td>{r.profit_scale}/10</td>
              <td>{r.risk_scale}/10</td>
              <td>${r.price_usd}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState<View>("home");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [botDetail, setBotDetail] = useState<{ bot: Bot; equity_curve: { equity: number; benchmark_btc: number }[] } | null>(null);
  const [checkoutSlugs, setCheckoutSlugs] = useState<string[]>([]);
  const [checkoutPackage, setCheckoutPackage] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [promo, setPromo] = useState("LAUNCH30");
  const [payMethod, setPayMethod] = useState<"card" | "crypto">("card");
  const [cryptoAsset, setCryptoAsset] = useState("USDT");
  const [quoteData, setQuoteData] = useState<{ total: number; subtotal: number; discount: number } | null>(null);
  const [orderResult, setOrderResult] = useState<Record<string, unknown> | null>(null);
  const [refundOrderId, setRefundOrderId] = useState("");
  const [refundReason, setRefundReason] = useState("");
  const [refundMsg, setRefundMsg] = useState("");
  const [chatOpen, setChatOpen] = useState(false);
  const [chatInput, setChatInput] = useState("");
  const [chatMsgs, setChatMsgs] = useState<ChatMsg[]>([
    { role: "bot", text: "Hi — I'm Bob's Bots Concierge. I can quote deals, process refunds, and walk you through install. Ask anything!" },
  ]);
  const sessionId = useMemo(() => `web-${Math.random().toString(36).slice(2, 10)}`, []);

  const go = useCallback((v: View) => {
    setView(v);
    window.scrollTo(0, 0);
  }, []);

  const openBot = useCallback(async (slug: string) => {
    setSelectedSlug(slug);
    setBotDetail(null);
    go("bot");
    try {
      const d = await fetchBot(slug);
      setBotDetail(d);
    } catch {
      setBotDetail(null);
    }
  }, [go]);

  const startCheckout = useCallback((slugs: string[], pkg?: string) => {
    setCheckoutSlugs(slugs);
    setCheckoutPackage(pkg || null);
    setOrderResult(null);
    go("checkout");
  }, [go]);

  useEffect(() => {
    fetchCatalog().then(setCatalog).catch(() => setCatalog(null));
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const orderId = params.get("order");
    if (orderId) {
      fetchOrder(orderId).then((r) => {
        if (r.order) {
          setOrderResult(r);
          go("order");
        }
      });
    }
  }, [go]);

  useEffect(() => {
    if (view !== "checkout") return;
    const body = checkoutPackage
      ? { package_id: checkoutPackage, promo_code: promo || undefined }
      : { bot_slugs: checkoutSlugs, promo_code: promo || undefined };
    if (!checkoutPackage && !checkoutSlugs.length) return;
    quote(body).then((q) => {
      if (q.total != null) setQuoteData(q);
    });
  }, [view, checkoutSlugs, checkoutPackage, promo]);

  const sendChat = async () => {
    const msg = chatInput.trim();
    if (!msg) return;
    setChatInput("");
    setChatMsgs((m) => [...m, { role: "user", text: msg }]);
    try {
      const r = await concierge(msg, sessionId, email || undefined);
      setChatMsgs((m) => [...m, { role: "bot", text: r.reply }]);
    } catch {
      setChatMsgs((m) => [...m, { role: "bot", text: "Concierge offline — email support@bobsbots.example or try again." }]);
    }
  };

  const doCheckout = async (demo = false) => {
    const body: Record<string, unknown> = {
      email,
      payment_method: payMethod,
      promo_code: promo || undefined,
      demo_confirm: demo,
    };
    if (checkoutPackage) body.package_id = checkoutPackage;
    else body.bot_slugs = checkoutSlugs;
    if (payMethod === "crypto") body.crypto_asset = cryptoAsset;

    const r = await checkout(body);
    if (r.error) {
      alert(r.error);
      return;
    }
    if (r.payment?.checkout_url && r.payment.mode === "stripe") {
      window.location.href = r.payment.checkout_url;
      return;
    }
    setOrderResult(r);
    go("order");
  };

  const submitRefund = async () => {
    const r = await requestRefund(refundOrderId, refundReason);
    setRefundMsg(r.message || r.error || "Submitted.");
  };

  const Nav = () => (
    <nav className="nav">
      <a href="#" className="logo" onClick={(e) => { e.preventDefault(); go("home"); }}>
        Bob<span>'s</span> Bots
      </a>
      <div className="nav-links">
        {(["home", "rankings", "backtest", "bots", "packages", "support", "legal"] as View[]).map((v) => (
          <button key={v} className={view === v ? "active" : ""} onClick={() => go(v)}>
            {v === "home"
              ? "Home"
              : v === "backtest"
                ? "Backtest Lab"
                : v === "legal"
                  ? "Legal"
                  : v.charAt(0).toUpperCase() + v.slice(1)}
          </button>
        ))}
      </div>
    </nav>
  );

  if (!catalog) {
    return (
      <div className="shell">
        <Nav />
        <p style={{ textAlign: "center", color: "var(--muted)" }}>Loading Bob&apos;s Bots…</p>
      </div>
    );
  }

  const legal = catalog.legal ?? DEFAULT_LEGAL;

  return (
    <div className="shell">
      <Nav />
      {view !== "legal" && <DisclaimerBanner legal={legal} />}

      {view === "home" && (
        <>
          <section className="hero">
            <h1>
              Trading bots ranked by <em>backtest</em>, not hype
            </h1>
            <p>{catalog.tagline || "Install in minutes. Pay with card or crypto. Concierge handles refunds & deals."}</p>
            <div className="hero-cta">
              <button className="btn btn-primary" onClick={() => go("backtest")}>
                Backtest all assets
              </button>
              <button className="btn btn-ghost" onClick={() => go("rankings")}>
                See rankings
              </button>
              <button className="btn btn-ghost" onClick={() => setChatOpen(true)}>
                Talk to concierge
              </button>
            </div>
          </section>

          <section className="section">
            <h2>Top 3 by backtest profit scale</h2>
            <p className="sub">Stacked profitability — benchmarks vs BTC buy & hold included on each bot page.</p>
            <div className="grid grid-3">
              {catalog.bots.slice(0, 3).map((b) => (
                <BotCard key={b.id} bot={b} onSelect={openBot} />
              ))}
            </div>
          </section>

          <section className="section">
            <h2>One TA core — tiers are add-ons</h2>
            <p className="sub">
              {catalog.ta_stack?.summary ||
                "Same confluence entry brain on every tier. You are not buying three different indicator systems."}
            </p>
            <div className="grid grid-3">
              <div className="card">
                <h3>Shared entry TA</h3>
                <p className="tagline">
                  EMA, RSI, MACD, BB, VWAP, ADX, MFI/CMF, volume, structure, funding, runner/chop filter — voted together (~15 methods, 1m + 5m).
                </p>
              </div>
              <div className="card">
                <h3>What changes per tier</h3>
                <p className="tagline">
                  Risk %, 3R pacing, optimizer aggressiveness, ML/cortex/LLM overlays, micro safety caps — not a different chart recipe.
                </p>
              </div>
              <div className="card">
                <h3>Universe Scanner</h3>
                <p className="tagline">
                  Pure add-on: ranks which symbols to scan. Does not replace or fork the entry TA stack.
                </p>
              </div>
            </div>
          </section>

          <section className="section">
            <h2>Why Bob&apos;s Bots?</h2>
            <div className="grid grid-3">
              <div className="card">
                <h3>📊 Backtest-first sales</h3>
                <p className="tagline">We publish CAGR, Sharpe, drawdown, and trade counts — no fake live PnL screenshots.</p>
              </div>
              <div className="card">
                <h3>⚡ 8-minute install</h3>
                <p className="tagline">One PowerShell bootstrap. Demo mode by default. Your keys never leave your machine.</p>
              </div>
              <div className="card">
                <h3>🤝 Concierge with teeth</h3>
                <p className="tagline">Our LLM can issue refunds, custom deals, and creative dispute resolutions — not just FAQ scripts.</p>
              </div>
            </div>
          </section>
        </>
      )}

      {view === "rankings" && (
        <section className="section">
          <h2>Profitability leaderboard</h2>
          <p className="sub">Sorted by backtest profit scale. Risk scale shows aggressiveness (higher = more volatile).</p>
          <RankingsTable rows={catalog.rankings} />
        </section>
      )}

      {view === "backtest" && <BacktestLab bots={catalog.bots} />}

      {view === "bots" && (
        <section className="section">
          <h2>All bots</h2>
          <p className="sub">Each bot includes dashboard, docs, and license key on purchase.</p>
          <div className="grid grid-3">
            {catalog.bots.map((b) => (
              <BotCard key={b.id} bot={b} onSelect={openBot} />
            ))}
          </div>
        </section>
      )}

      {view === "bot" && botDetail && (
        <section className="section">
          <button className="btn btn-ghost" onClick={() => go("bots")} style={{ marginBottom: "1rem" }}>
            ← All bots
          </button>
          <h2>{botDetail.bot.name}</h2>
          <p className="sub">{botDetail.bot.tagline}</p>
          <EquityChart data={botDetail.equity_curve} />
          <p style={{ fontSize: "0.8rem", color: "var(--muted)" }}>
            Teal = bot equity curve · Gray = BTC buy & hold (backtest period)
          </p>
          <div className="metrics" style={{ marginTop: "1.5rem" }}>
            {Object.entries(botDetail.bot.backtest)
              .filter(([k]) => !["period", "benchmark_btc_buy_hold_pct", "benchmark_eth_buy_hold_pct"].includes(k))
              .slice(0, 8)
              .map(([k, v]) => (
                <div className="metric" key={k}>
                  <span>{k.replace(/_/g, " ")}</span>
                  <strong>{String(v)}</strong>
                </div>
              ))}
          </div>
          <ul style={{ margin: "1.5rem 0", paddingLeft: "1.25rem", color: "var(--muted)" }}>
            {botDetail.bot.highlights.map((h) => (
              <li key={h}>{h}</li>
            ))}
          </ul>
          <button
            className="btn btn-primary"
            onClick={() => startCheckout([botDetail.bot.slug])}
          >
            Buy for ${botDetail.bot.price_usd}
          </button>
          <button
            className="btn btn-ghost"
            style={{ marginLeft: "0.5rem" }}
            onClick={() => go("backtest")}
          >
            Backtest on all assets
          </button>
        </section>
      )}

      {view === "packages" && (
        <section className="section">
          <h2>Stacks & packages</h2>
          <p className="sub">Bundle bots and save. Empire bundle ranks #1 on combined backtest metrics.</p>
          <div className="grid grid-3">
            {catalog.packages.map((pkg: Package) => (
              <article className="card" key={pkg.id}>
                {pkg.badge && <span className="badge">{pkg.badge}</span>}
                <h3>{pkg.name}</h3>
                <p className="price">
                  ${pkg.price_usd}{" "}
                  <span style={{ fontSize: "0.85rem", color: "var(--muted)", textDecoration: "line-through" }}>
                    ${pkg.original_usd}
                  </span>
                </p>
                <ul style={{ margin: "1rem 0", paddingLeft: "1.1rem", fontSize: "0.88rem", color: "var(--muted)" }}>
                  {pkg.includes.map((i) => (
                    <li key={i}>{i}</li>
                  ))}
                </ul>
                <button className="btn btn-primary" style={{ width: "100%" }} onClick={() => startCheckout([], pkg.id)}>
                  Get package
                </button>
              </article>
            ))}
          </div>
        </section>
      )}

      {view === "checkout" && (
        <section className="section">
          <h2>Checkout</h2>
          <div className="checkout-grid">
            <div className="card">
              <label>Email (for license & fulfillment)</label>
              <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@email.com" />
              <label>Promo code</label>
              <input value={promo} onChange={(e) => setPromo(e.target.value)} placeholder="LAUNCH30" />
              <div className="pay-tabs">
                <button className={payMethod === "card" ? "active" : ""} onClick={() => setPayMethod("card")}>
                  💳 Card
                </button>
                <button className={payMethod === "crypto" ? "active" : ""} onClick={() => setPayMethod("crypto")}>
                  ₿ Crypto
                </button>
              </div>
              {payMethod === "crypto" && (
                <>
                  <label>Asset</label>
                  <select value={cryptoAsset} onChange={(e) => setCryptoAsset(e.target.value)}>
                    <option value="USDT">USDT (TRC20)</option>
                    <option value="BTC">BTC</option>
                    <option value="ETH">ETH</option>
                  </select>
                </>
              )}
              {quoteData && (
                <p style={{ marginTop: "1rem" }}>
                  Total: <span className="price">${quoteData.total}</span>
                  {quoteData.discount > 0 && (
                    <span style={{ color: "var(--ok)", marginLeft: "0.5rem" }}>(-${quoteData.discount} promo)</span>
                  )}
                </p>
              )}
              <button className="btn btn-primary" style={{ marginTop: "1rem", width: "100%" }} onClick={() => doCheckout(false)}>
                Pay now
              </button>
              <button className="btn btn-ghost" style={{ marginTop: "0.5rem", width: "100%" }} onClick={() => doCheckout(true)}>
                Demo checkout (dev)
              </button>
            </div>
            <div className="card">
              <h3>Active promos</h3>
              <ul style={{ paddingLeft: "1.1rem", color: "var(--muted)", fontSize: "0.9rem" }}>
                {catalog.deals.map((d) => (
                  <li key={d.code}>
                    <strong>{d.code}</strong> — {d.label} ({d.pct_off}% off)
                  </li>
                ))}
              </ul>
              <p style={{ marginTop: "1rem", fontSize: "0.85rem", color: "var(--muted)" }}>
                Payments secured via Stripe (cards) or on-chain crypto. By purchasing you agree you are not receiving financial advice
                from {legal.operator}. Bob&apos;s Concierge can match payments and issue manual refunds.
              </p>
            </div>
          </div>
        </section>
      )}

      {view === "order" && orderResult && (
        <section className="section">
          <h2>Order confirmed</h2>
          <div className="order-box">
            <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(orderResult, null, 2)}</pre>
          </div>
          <p style={{ marginTop: "1rem", color: "var(--muted)" }}>
            Check your email for install steps. Paste your license key in <code>.env</code> as BOBS_BOTS_LICENSE.
          </p>
        </section>
      )}

      {view === "legal" && <LegalPage legal={legal} />}

      {view === "support" && (
        <section className="section">
          <h2>Refunds, disputes & support</h2>
          <p className="sub">
            Bob&apos;s Bots Concierge can approve refunds, partial credits, tier swaps, and custom bundles. Human escalation within 24h.
          </p>
          <div className="checkout-grid">
            <div className="card">
              <h3>Request a refund</h3>
              <label>Order ID (BB-…)</label>
              <input value={refundOrderId} onChange={(e) => setRefundOrderId(e.target.value)} />
              <label>Reason</label>
              <textarea rows={4} value={refundReason} onChange={(e) => setRefundReason(e.target.value)} />
              <button className="btn btn-primary" style={{ marginTop: "1rem" }} onClick={submitRefund}>
                Submit
              </button>
              {refundMsg && <p style={{ marginTop: "0.75rem", color: "var(--ok)" }}>{refundMsg}</p>}
            </div>
            <div className="card">
              <h3>Creative resolutions</h3>
              <ul style={{ paddingLeft: "1.1rem", color: "var(--muted)", fontSize: "0.9rem" }}>
                <li>7-day no-questions refund if bot won&apos;t install on your machine</li>
                <li>50% refund + keep access if backtest expectations misaligned</li>
                <li>Store credit toward any package — stacks with LAUNCH30</li>
                <li>Free tier swap (e.g. Conservative → 3R Fast Lane)</li>
                <li>90-day concierge extension instead of cash refund</li>
              </ul>
              <button className="btn btn-ghost" style={{ marginTop: "1rem" }} onClick={() => setChatOpen(true)}>
                Open concierge chat
              </button>
            </div>
          </div>
        </section>
      )}

      {view !== "legal" && <p className="disclaimer">{catalog.disclaimer}</p>}
      <DisclaimerFooter legal={legal} onLegal={() => go("legal")} />

      <button className="concierge-fab" onClick={() => setChatOpen(!chatOpen)} title="Bob's Bots Concierge">
        💬
      </button>
      {chatOpen && (
        <div className="concierge-panel">
          <div className="concierge-head">
            Bob&apos;s Bots Concierge
            <small>Refunds · deals · install help · dispute resolution</small>
          </div>
          <div className="concierge-msgs">
            {chatMsgs.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                {m.text}
              </div>
            ))}
          </div>
          <div className="concierge-input">
            <input
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && sendChat()}
              placeholder="Ask about bots, refunds, deals…"
            />
            <button className="btn btn-primary" onClick={sendChat}>
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
