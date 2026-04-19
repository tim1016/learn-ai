const { useState } = React;

const PortfolioDashboard = () => {
  const [positions, setPositions] = useState([
    { ticker: 'SPY', qty: 100, entry: 438.20, mark: 442.50, pl: 430 },
    { ticker: 'QQQ', qty: 50, entry: 378.80, mark: 384.20, pl: 270 },
    { ticker: 'GLD', qty: 75, entry: 188.10, mark: 185.40, pl: -202.50 },
    { ticker: 'TLT', qty: 200, entry: 92.40, mark: 90.80, pl: -320 },
  ]);
  const [form, setForm] = useState({ ticker: '', qty: '', price: '' });
  const [toast, setToast] = useState(null);

  const record = () => {
    if (!form.ticker || !form.qty || !form.price) return;
    const qty = parseFloat(form.qty); const price = parseFloat(form.price);
    setPositions([...positions, { ticker: form.ticker.toUpperCase(), qty, entry: price, mark: price, pl: 0 }]);
    setForm({ ticker: '', qty: '', price: '' });
    setToast('Trade recorded. Portfolio revalued.');
    setTimeout(() => setToast(null), 2400);
  };

  const totalPL = positions.reduce((s, p) => s + p.pl, 0);
  const totalMV = positions.reduce((s, p) => s + p.qty * p.mark, 0);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div>
        <Eyebrow>Portfolio / Dashboard</Eyebrow>
        <h1 style={{ margin: '4px 0 0', fontSize: 28, fontWeight: 600, letterSpacing: '-0.02em' }}>
          Core research book
        </h1>
        <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
          Marks from last Polygon snapshot at <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>2026-04-19 16:00 ET</span>.
        </p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
        <StatCard tone={totalPL >= 0 ? 'bull' : 'bear'} label="Unrealized P&L" value={`${totalPL >= 0 ? '+' : '−'}$${Math.abs(totalPL).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`} sub={`${positions.length} positions`} />
        <StatCard label="Market Value" value={`$${totalMV.toLocaleString(undefined, {maximumFractionDigits: 0})}`} sub="long only" />
        <StatCard label="Exposure" value="42.3%" sub="of $1.2M equity" />
        <StatCard label="Beta (SPY)" value="0.812" sub="60d trailing" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1.8fr 1fr', gap: 16 }}>
        <Card padding="0" style={{ overflow: 'hidden' }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center' }}>
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Positions</h3>
            <div style={{ flex: 1 }} />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{positions.length} open</span>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, fontVariantNumeric: 'tabular-nums' }}>
            <thead>
              <tr>
                {['Ticker','Qty','Entry','Mark','P&L'].map((h, i) => (
                  <th key={h} style={{
                    fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase',
                    background: 'var(--bg-canvas)', borderBottom: '2px solid var(--border-light)',
                    padding: '8px 14px', textAlign: i === 0 ? 'left' : 'right', letterSpacing: '0.04em',
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((p, i) => (
                <tr key={i}>
                  <td style={{ padding: '9px 14px', fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', fontWeight: 600, borderBottom: '1px solid var(--border)' }}>{p.ticker}</td>
                  <td style={{ padding: '9px 14px', textAlign: 'right', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)', borderBottom: '1px solid var(--border)' }}>{p.qty}</td>
                  <td style={{ padding: '9px 14px', textAlign: 'right', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)', borderBottom: '1px solid var(--border)' }}>${p.entry.toFixed(2)}</td>
                  <td style={{ padding: '9px 14px', textAlign: 'right', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)', borderBottom: '1px solid var(--border)' }}>${p.mark.toFixed(2)}</td>
                  <td style={{ padding: '9px 14px', textAlign: 'right', color: p.pl >= 0 ? 'var(--bull)' : 'var(--bear)', fontWeight: 600, fontFamily: 'var(--font-mono)', borderBottom: '1px solid var(--border)' }}>
                    {p.pl >= 0 ? '+' : '−'}${Math.abs(p.pl).toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card>
          <Eyebrow style={{ marginBottom: 12 }}>Record trade</Eyebrow>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <Input label="Ticker" value={form.ticker} onChange={e => setForm({ ...form, ticker: e.target.value })} placeholder="AAPL" mono />
            <Input label="Quantity" value={form.qty} onChange={e => setForm({ ...form, qty: e.target.value })} placeholder="100" mono />
            <Input label="Fill Price" value={form.price} onChange={e => setForm({ ...form, price: e.target.value })} placeholder="188.25" mono />
            <Button icon="pi-plus" onClick={record} style={{ marginTop: 6 }}>Record Trade</Button>
          </div>
          {toast && <div style={{ marginTop: 12 }}><Callout tone="info" title="Saved">{toast}</Callout></div>}
        </Card>
      </div>
    </div>
  );
};

Object.assign(window, { PortfolioDashboard });
