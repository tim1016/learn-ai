/* Page-level redesign mock: sticky ToC, search, filter chips.
   Replaces the current "one giant vertical scroll of 30+ accordions." */

const PageRedesign = () => {
  const [q, setQ] = React.useState("");
  const [panel, setPanel] = React.useState("all");

  const indicators = [
    { id: "ema",  name: "EMA",       full: "Exponential Moving Average", panel: "overlay", cols: 1, tags: ["trend", "ma"], caveats: 1 },
    { id: "sma",  name: "SMA",       full: "Simple Moving Average",      panel: "overlay", cols: 1, tags: ["trend", "ma"], caveats: 1 },
    { id: "bbands", name: "BBands",  full: "Bollinger Bands",            panel: "overlay", cols: 5, tags: ["volatility"], caveats: 1, delay: true },
    { id: "supertrend", name: "Supertrend", full: "Supertrend",          panel: "overlay", cols: 4, tags: ["trend"], caveats: 2, delay: true },
    { id: "vwap", name: "VWAP",      full: "Volume Weighted Average Price", panel: "overlay", cols: 1, tags: ["volume"], caveats: 4, delay: true },
    { id: "rsi",  name: "RSI",       full: "Relative Strength Index",    panel: "sub",     cols: 1, tags: ["momentum"], caveats: 1, delay: true, active: true },
    { id: "macd", name: "MACD",      full: "Moving Avg Conv / Div",      panel: "sub",     cols: 3, tags: ["momentum"], caveats: 1, delay: true },
    { id: "adx",  name: "ADX",       full: "Average Directional Index",  panel: "sub",     cols: 3, tags: ["trend"], caveats: 1 },
    { id: "atr",  name: "ATR",       full: "Average True Range",         panel: "sub",     cols: 1, tags: ["volatility"], caveats: 2 },
    { id: "stoch", name: "Stoch",    full: "Stochastic Oscillator",      panel: "sub",     cols: 2, tags: ["momentum"], caveats: 1, delay: true },
    { id: "obv",  name: "OBV",       full: "On Balance Volume",          panel: "sub",     cols: 1, tags: ["volume"], caveats: 3 },
    { id: "mfi",  name: "MFI",       full: "Money Flow Index",           panel: "sub",     cols: 1, tags: ["volume", "momentum"], caveats: 3, delay: true },
  ];

  const filtered = indicators.filter(i =>
    (panel === "all" || i.panel === panel) &&
    (q === "" ||
      i.name.toLowerCase().includes(q.toLowerCase()) ||
      i.full.toLowerCase().includes(q.toLowerCase()) ||
      i.tags.some(t => t.includes(q.toLowerCase())))
  );

  return (
    <div style={{
      background: "var(--bg-canvas)",
      color: "var(--text-primary)",
      fontFamily: "var(--font-sans)",
      borderRadius: "var(--radius-md)",
      overflow: "hidden",
      border: "1px solid var(--border)",
    }}>
      <div style={{
        display: "grid",
        gridTemplateColumns: "280px 1fr",
        minHeight: 620,
      }}>

        {/* ── Sticky sidebar with search + filters + list ── */}
        <aside style={{
          background: "var(--bg-surface)",
          borderRight: "1px solid var(--border)",
          padding: "16px 0",
          display: "flex",
          flexDirection: "column",
        }}>
          {/* Page title */}
          <div style={{ padding: "0 16px 12px", borderBottom: "1px solid var(--border)" }}>
            <div style={{
              fontSize: "var(--fs-xxs)",
              color: "var(--text-muted)",
              textTransform: "uppercase",
              letterSpacing: "var(--ls-caps)",
              fontWeight: 600,
              marginBottom: 4,
            }}>Data Lab Docs</div>
            <div style={{ fontSize: "var(--fs-md)", fontWeight: 600 }}>Indicator Reference</div>
            <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)", marginTop: 4 }}>
              {indicators.length} indicators · pandas-ta
            </div>
          </div>

          {/* Search */}
          <div style={{ padding: "12px 16px 10px" }}>
            <div style={{ position: "relative" }}>
              <span style={{
                position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
                color: "var(--text-muted)", fontSize: 12,
              }}>⌕</span>
              <input
                type="text"
                placeholder="Search name or tag…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                style={{
                  width: "100%",
                  padding: "7px 10px 7px 28px",
                  background: "var(--bg-elevated)",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius-sm)",
                  color: "var(--text-primary)",
                  fontSize: "var(--fs-sm)",
                  fontFamily: "inherit",
                  outline: "none",
                  boxSizing: "border-box",
                }}
              />
            </div>
          </div>

          {/* Panel filter */}
          <div style={{ padding: "0 16px 10px", display: "flex", gap: 6 }}>
            {[
              ["all", "All"],
              ["overlay", "Overlay"],
              ["sub", "Sub-panel"],
            ].map(([v, label]) => (
              <button
                key={v}
                onClick={() => setPanel(v)}
                style={{
                  flex: 1,
                  padding: "5px 8px",
                  fontSize: "var(--fs-xs)",
                  fontFamily: "inherit",
                  background: panel === v ? "var(--accent-soft)" : "var(--bg-elevated)",
                  color: panel === v ? "var(--accent)" : "var(--text-secondary)",
                  border: "1px solid " + (panel === v ? "rgba(59,130,246,0.3)" : "var(--border)"),
                  borderRadius: "var(--radius-sm)",
                  cursor: "pointer",
                }}
              >{label}</button>
            ))}
          </div>

          {/* Indicator list */}
          <div style={{ flex: 1, overflowY: "auto", padding: "4px 8px 16px" }}>
            {filtered.map(ind => (
              <button
                key={ind.id}
                style={{
                  width: "100%",
                  display: "grid",
                  gridTemplateColumns: "auto 1fr auto",
                  gap: 8,
                  alignItems: "center",
                  padding: "7px 10px",
                  marginBottom: 1,
                  background: ind.active ? "var(--bg-hover)" : "transparent",
                  borderLeft: "2px solid " + (ind.active ? "var(--accent)" : "transparent"),
                  border: "none",
                  borderRadius: 0,
                  color: "inherit",
                  fontFamily: "inherit",
                  cursor: "pointer",
                  textAlign: "left",
                }}
              >
                <code style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                  color: ind.active ? "var(--accent)" : "var(--text-muted)",
                  background: "transparent",
                  minWidth: 40,
                  padding: 0,
                  border: 0,
                }}>{ind.name}</code>
                <span style={{
                  fontSize: "var(--fs-xs)",
                  color: ind.active ? "var(--text-primary)" : "var(--text-secondary)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}>{ind.full}</span>
                <span style={{ display: "flex", gap: 3 }}>
                  {ind.delay && <span title="Affected by feed delay" style={{
                    width: 6, height: 6, borderRadius: "50%", background: "var(--bear)"
                  }} />}
                  {ind.caveats > 1 && <span title="Multiple data caveats" style={{
                    width: 6, height: 6, borderRadius: "50%", background: "var(--warn)"
                  }} />}
                </span>
              </button>
            ))}
            {filtered.length === 0 && (
              <div style={{ padding: 16, color: "var(--text-muted)", fontSize: "var(--fs-xs)", textAlign: "center" }}>
                No indicators match "{q}"
              </div>
            )}
          </div>

          {/* Legend */}
          <div style={{
            padding: "10px 16px",
            borderTop: "1px solid var(--border)",
            display: "flex",
            gap: 12,
            fontSize: "var(--fs-xxs)",
            color: "var(--text-muted)",
          }}>
            <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--bear)" }} /> delay
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--warn)" }} /> caveat
            </span>
          </div>
        </aside>

        {/* ── Right pane: contextual view ─── */}
        <main style={{
          padding: 24,
          overflowY: "auto",
          background: "var(--bg-canvas)",
        }}>
          <div style={{
            fontSize: "var(--fs-xxs)",
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "var(--ls-caps)",
            fontWeight: 600,
            marginBottom: 6,
          }}>Docs › Sub-panel › Momentum</div>

          <h1 style={{
            fontSize: "1.6rem",
            fontWeight: 700,
            margin: "0 0 6px",
            letterSpacing: "var(--ls-tight)",
          }}>
            Relative Strength Index <span style={{
              fontFamily: "var(--font-mono)",
              fontSize: "0.8rem",
              color: "var(--text-muted)",
              marginLeft: 8,
              padding: "3px 8px",
              background: "var(--bg-elevated)",
              borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border)",
              verticalAlign: "middle",
              fontWeight: 500,
            }}>RSI</span>
          </h1>
          <p style={{
            color: "var(--text-secondary)",
            fontSize: "var(--fs-sm)",
            margin: "0 0 20px",
            maxWidth: "56ch",
          }}>
            Jump between indicators without losing your place. The URL hash stays in sync so{" "}
            <code style={{ fontFamily: "var(--font-mono)", fontSize: "0.78rem", background: "var(--bg-elevated)", padding: "1px 5px", borderRadius: 3 }}>
              #ind-rsi
            </code>{" "}
            still works, and deep links to anchors like <code style={{ fontFamily: "var(--font-mono)", fontSize: "0.78rem", background: "var(--bg-elevated)", padding: "1px 5px", borderRadius: 3 }}>
              #ind-rsi/math
            </code> open the right tab.
          </p>

          <div style={{
            padding: 16,
            background: "var(--bg-surface)",
            border: "1px dashed var(--border-light)",
            borderRadius: "var(--radius-md)",
            textAlign: "center",
            color: "var(--text-muted)",
            fontSize: "var(--fs-sm)",
            lineHeight: 1.6,
          }}>
            → The redesigned indicator card from §3 loads here.<br/>
            <span style={{ color: "var(--text-secondary)" }}>
              Right rail stays fixed while the left list scrolls through the full catalog.
            </span>
          </div>

          <div style={{ marginTop: 16, display: "flex", gap: 8, justifyContent: "space-between" }}>
            <button style={{
              padding: "8px 14px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-secondary)",
              fontFamily: "inherit",
              fontSize: "var(--fs-sm)",
              cursor: "pointer",
            }}>← Previous: MACD</button>
            <button style={{
              padding: "8px 14px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-secondary)",
              fontFamily: "inherit",
              fontSize: "var(--fs-sm)",
              cursor: "pointer",
            }}>Next: ADX →</button>
          </div>
        </main>
      </div>
    </div>
  );
};

window.PageRedesign = PageRedesign;
