const { useState } = React;

const NAV = [
  { label: 'Stocks', icon: 'pi-chart-line', items: ['Market Data', 'Ticker Explorer', 'Technical Analysis', 'Strategy Lab', 'Strategy Validation', 'Indicator Validation', 'Data Lab'] },
  { label: 'Data Quality', icon: 'pi-shield', items: ['Pipeline Analysis', 'Docs'] },
  { label: 'Options', icon: 'pi-objects-column', items: ['Options Chain', 'Strategy Builder', 'Pricing Lab', 'Options History', 'Snapshots'] },
  { label: 'Engine', icon: 'pi-cog', items: ['Lean Engine'] },
  { label: 'Portfolio', icon: 'pi-wallet', items: ['Dashboard', 'Positions', 'Risk Panel', 'Scenario Explorer', 'Attribution', 'Reconciliation'] },
  { label: 'Research Lab', icon: 'pi-search', items: ['Feature Runner', 'Signal Runner', 'Batch Runner', 'Reports'] },
];

const TopNav = ({ current, onNavigate }) => {
  const [open, setOpen] = useState(null);
  return (
    <div style={{
      background: 'var(--bg-surface)', borderBottom: '1px solid var(--border)',
      display: 'flex', alignItems: 'center', padding: '0 24px', height: 52, gap: 2,
      position: 'sticky', top: 0, zIndex: 50,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginRight: 24 }}>
        <svg width="18" height="22" viewBox="0 0 22 26"><rect x="9" y="0" width="4" height="26" fill="#5a6178"/><rect x="4" y="5" width="14" height="14" fill="#00c896" rx="1"/></svg>
        <div style={{ fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 14, letterSpacing: '-0.01em' }}>
          quant<span style={{ color: 'var(--text-muted)', fontWeight: 500 }}>/</span>lab
        </div>
      </div>

      {NAV.map(g => (
        <div key={g.label} style={{ position: 'relative' }} onMouseLeave={() => setOpen(null)}>
          <button
            onMouseEnter={() => setOpen(g.label)}
            onClick={() => setOpen(open === g.label ? null : g.label)}
            style={{
              background: open === g.label ? 'var(--bg-hover)' : 'transparent',
              color: open === g.label ? 'var(--text-primary)' : 'var(--text-secondary)',
              border: 'none', padding: '8px 14px', borderRadius: 6, cursor: 'pointer',
              display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 13, fontWeight: 500,
              fontFamily: 'inherit',
            }}
          >
            <i className={`pi ${g.icon}`} style={{ fontSize: 12 }} />
            {g.label}
            <i className="pi pi-angle-down" style={{ fontSize: 10, opacity: 0.6 }} />
          </button>
          {open === g.label && (
            <div style={{
              position: 'absolute', top: 38, left: 0, minWidth: 200,
              background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              borderRadius: 8, boxShadow: '0 4px 12px rgba(0,0,0,0.4)', padding: 4, zIndex: 100,
            }}>
              {g.items.map(item => (
                <button key={item} onClick={() => { onNavigate(item); setOpen(null); }}
                  style={{
                    display: 'block', width: '100%', textAlign: 'left',
                    padding: '8px 12px', background: current === item ? 'var(--accent-soft)' : 'transparent',
                    color: current === item ? 'var(--accent)' : 'var(--text-primary)',
                    border: 'none', borderRadius: 5, cursor: 'pointer',
                    fontSize: 13, fontFamily: 'inherit',
                  }}
                  onMouseEnter={e => { if (current !== item) e.currentTarget.style.background = 'var(--bg-hover)'; }}
                  onMouseLeave={e => { if (current !== item) e.currentTarget.style.background = 'transparent'; }}
                >{item}</button>
              ))}
            </div>
          )}
        </div>
      ))}

      <div style={{ flex: 1 }} />
      <button style={{
        background: 'transparent', border: '1px solid var(--border)',
        color: 'var(--text-secondary)', padding: '5px 12px', borderRadius: 6,
        fontSize: 12, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6,
        fontFamily: 'var(--font-mono)',
      }}>
        <span style={{ width: 6, height: 6, borderRadius: 9999, background: 'var(--bull)' }} />
        polygon · live
      </button>
    </div>
  );
};

Object.assign(window, { TopNav });
