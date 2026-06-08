export type LegalContent = {
  operator: string;
  brand: string;
  short_disclaimer: string;
  footer_line: string;
  sections: { title: string; body: string }[];
};

export const DEFAULT_LEGAL: LegalContent = {
  operator: "Matthew Anthony Knight",
  brand: "Bob's Bots",
  short_disclaimer:
    "Operated by Matthew Anthony Knight. Not financial advice. Trading involves substantial risk of loss.",
  footer_line: "© 2026 Bob's Bots — Matthew Anthony Knight. All rights reserved.",
  sections: [],
};

export function DisclaimerBanner({ legal }: { legal: LegalContent }) {
  return (
    <aside className="disclaimer-banner" role="note">
      <strong>Important:</strong> {legal.short_disclaimer}
    </aside>
  );
}

export function DisclaimerFooter({ legal, onLegal }: { legal: LegalContent; onLegal?: () => void }) {
  return (
    <footer className="footer">
      <p>{legal.footer_line}</p>
      <p className="footer-legal">{legal.short_disclaimer}</p>
      {onLegal && (
        <button type="button" className="link-btn" onClick={onLegal}>
          Full legal disclaimers
        </button>
      )}
    </footer>
  );
}

export function LegalPage({ legal }: { legal: LegalContent }) {
  return (
    <section className="section legal-page">
      <h2>Legal disclaimers</h2>
      <p className="sub">
        Bob&apos;s Bots is operated by <strong>{legal.operator}</strong>. Please read before purchasing or trading.
      </p>
      <div className="grid grid-3" style={{ gridTemplateColumns: "1fr" }}>
        {legal.sections.map((s) => (
          <article className="card" key={s.title}>
            <h3>{s.title}</h3>
            <p className="tagline" style={{ marginTop: "0.5rem" }}>
              {s.body}
            </p>
          </article>
        ))}
      </div>
      <p className="disclaimer" style={{ marginTop: "2rem" }}>
        By using this site, purchasing software, or running bots you acknowledge these terms and accept full responsibility
        for your trading activity.
      </p>
    </section>
  );
}
