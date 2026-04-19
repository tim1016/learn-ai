const { useState } = React;

const chain = [
  { callBid: 22.40, callAsk: 22.80, strike: 420, putBid: 0.15, putAsk: 0.20, itmCall: true },
  { callBid: 17.60, callAsk: 18.00, strike: 425, putBid: 0.35, putAsk: 0.45, itmCall: true },
  { callBid: 12.40, callAsk: 12.80, strike: 430, putBid: 0.80, putAsk: 0.90, itmCall: true },
  { callBid: 8.10, callAsk: 8.40, strike: 435, putBid: 1.70, putAsk: 1.85, itmCall: true },
  { callBid: 5.20, callAsk: 5.40, strike: 442.50, putBid: 5.30, putAsk: 5.50, atm: true },
  { callBid: 3.10, callAsk: 3.30, strike: 447.50, putBid: 8.20, putAsk: 8.50, itmPut: true },
  { callBid: 1.10, callAsk: 1.30, strike: 455, putBid: 13.60, putAsk: 14.00, itmPut: true },
  { callBid: 0.45, callAsk: 0.55, strike: 460, putBid: 18.20, putAsk: 18.60, itmPut: true },
];

const OptionsChain = () => {
  const [exp, setExp] = useState('2026-05-16');
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div>
        <Eyebrow>Options / Options Chain</Eyebrow>
        <h1 style={{ margin: '4px 0 0', fontSize: 28, fontWeight: 600, letterSpacing: '-0.02em' }}>
          SPY · <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent)' }}>$442.50</span>
        </h1>
        <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
          Spot moved +0.84% today. Chain shown at last mid. ATM row highlighted; ITM cells hatched.
        </p>
      </div>

      <Card>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, alignItems: 'end' }}>
          <Input label="Ticker" value="SPY" onChange={() => {}} mono />
          <Select label="Expiration" value={exp} onChange={e => setExp(e.target.value)} options={['2026-05-16','2026-06-20','2026-09-19','2026-12-19']} />
          <Select label="Type" value="Both" onChange={() => {}} options={['Both','Calls','Puts']} />
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', paddingBottom: 2 }}>
            <Badge tone="accent" icon="pi-bookmark-fill">Live · delayed 15m</Badge>
          </div>
        </div>
      </Card>

      <Card padding="0" style={{ overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}>
          <thead>
            <tr>
              <th colSpan="2" style={{ padding: '8px 14px', fontSize: 10, fontWeight: 700, color: 'var(--bull)', background: 'var(--bg-canvas)', textAlign: 'center', letterSpacing: '0.08em', borderBottom: '2px solid var(--border-light)' }}>CALL</th>
              <th style={{ padding: '8px 14px', fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', background: 'var(--bg-canvas)', textAlign: 'center', letterSpacing: '0.08em', borderBottom: '2px solid var(--border-light)' }}>STRIKE</th>
              <th colSpan="2" style={{ padding: '8px 14px', fontSize: 10, fontWeight: 700, color: 'var(--bear)', background: 'var(--bg-canvas)', textAlign: 'center', letterSpacing: '0.08em', borderBottom: '2px solid var(--border-light)' }}>PUT</th>
            </tr>
            <tr>
              {['Bid','Ask','','Bid','Ask'].map((h, i) => (
                <th key={i} style={{ padding: '6px 14px', fontSize: 9, fontWeight: 600, color: 'var(--text-muted)', background: 'var(--bg-canvas)', textAlign: 'right', borderBottom: '1px solid var(--border)', textTransform: 'uppercase' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {chain.map((r, i) => {
              const itmCallStyle = r.itmCall ? {
                color: '#d1fae5',
                backgroundImage: 'repeating-linear-gradient(-45deg, transparent, transparent 4px, rgba(16,185,129,0.12) 4px, rgba(16,185,129,0.12) 5px)',
                background: 'rgba(6,78,59,0.25)',
              } : { color: 'var(--text-muted)' };
              const itmPutStyle = r.itmPut ? {
                color: '#fecaca',
                backgroundImage: 'repeating-linear-gradient(-45deg, transparent, transparent 4px, rgba(239,68,68,0.12) 4px, rgba(239,68,68,0.12) 5px)',
                background: 'rgba(127,29,29,0.25)',
              } : { color: 'var(--text-muted)' };
              const atmRow = r.atm ? {
                borderTop: '2px solid rgba(245,158,11,0.8)',
                borderBottom: '2px solid rgba(245,158,11,0.8)',
                background: 'rgba(245,158,11,0.12)',
                color: '#fef3c7',
                fontWeight: 700,
              } : {};
              const td = { padding: '8px 14px', textAlign: 'right', borderBottom: '1px solid var(--border)' };
              return (
                <tr key={i} style={atmRow}>
                  <td style={{ ...td, ...itmCallStyle }}>{r.callBid.toFixed(2)}</td>
                  <td style={{ ...td, ...itmCallStyle }}>{r.callAsk.toFixed(2)}</td>
                  <td style={{ ...td, textAlign: 'center', color: r.atm ? '#fef3c7' : 'var(--text-primary)', fontWeight: 700 }}>{r.strike.toFixed(2).replace(/\.00$/, '')}</td>
                  <td style={{ ...td, ...itmPutStyle }}>{r.putBid.toFixed(2)}</td>
                  <td style={{ ...td, ...itmPutStyle }}>{r.putAsk.toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      <Callout tone="warn">
        Minute-granularity options data is not available on the <code>Polygon Starter</code> plan. Use day aggregates for ranges &gt; 5 days.
      </Callout>
    </div>
  );
};

Object.assign(window, { OptionsChain });
