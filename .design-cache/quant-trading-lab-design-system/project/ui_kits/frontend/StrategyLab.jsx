const { useState } = React;

const StrategyLab = () => {
  const [preset, setPreset] = useState('1M');
  const [ticker, setTicker] = useState('SPY');
  const [running, setRunning] = useState(false);
  const [ran, setRan] = useState(false);

  const run = () => {
    setRunning(true);
    setTimeout(() => { setRunning(false); setRan(true); }, 900);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div>
        <Eyebrow>Stocks / Strategy Lab</Eyebrow>
        <h1 style={{ margin: '4px 0 0', fontSize: 28, fontWeight: 600, letterSpacing: '-0.02em' }}>
          EMA crossover backtest
        </h1>
        <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
          Validate the <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>ema_12</code> / <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>ema_26</code> crossover on daily bars. Results are cached.
        </p>
      </div>

      <Card>
        <Eyebrow style={{ marginBottom: 12 }}>Configuration</Eyebrow>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 14, alignItems: 'end' }}>
          <Input label="Ticker" value={ticker} onChange={e => setTicker(e.target.value.toUpperCase())} mono />
          <Input label="From" value="2024-01-01" onChange={() => {}} mono />
          <Input label="To" value="2024-12-31" onChange={() => {}} mono />
          <Select label="Timespan" value="Daily" onChange={() => {}} options={['Daily', 'Hour', '15min', '5min', '1min']} />
          <Input label="EMA Short / Long" value="12 / 26" onChange={() => {}} mono />
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 14, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', marginRight: 4 }}>Range</span>
          {['1D','1W','1M','3M','YTD','1Y'].map(p => (
            <PresetPill key={p} active={preset === p} onClick={() => setPreset(p)}>{p}</PresetPill>
          ))}
          <div style={{ flex: 1 }} />
          <Button variant="secondary" onClick={() => setRan(false)}>Reset</Button>
          <Button icon={running ? 'pi-spin pi-spinner' : 'pi-play'} onClick={run} disabled={running}>
            {running ? 'Running…' : 'Run Backtest'}
          </Button>
        </div>
      </Card>

      {ran && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12 }}>
            <StatCard tone="bull" label="Total Return" value="+24.17%" sub="vs benchmark +12.04%" />
            <StatCard label="Sharpe" value="1.873" sub="rf=0, daily" />
            <StatCard tone="bear" label="Max Drawdown" value="−8.42%" sub="2023-03-14 → 03-22" />
            <StatCard label="Win Rate" value="62.3%" sub="121 trades" />
            <StatCard label="Profit Factor" value="2.14" sub="gross win / gross loss" />
          </div>

          <Card padding="0" style={{ overflow: 'hidden' }}>
            <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10 }}>
              <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Trade log</h3>
              <Badge tone="bull" icon="pi-check">validated</Badge>
              <div style={{ flex: 1 }} />
              <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>121 trades · 75W / 46L</span>
            </div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, fontVariantNumeric: 'tabular-nums' }}>
              <thead>
                <tr>
                  {['#','Trade','Entry','Exit','Bars','P&L','Result'].map((h, i) => (
                    <th key={h} style={{
                      fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase',
                      background: 'var(--bg-canvas)', borderBottom: '2px solid var(--border-light)',
                      padding: '8px 14px', textAlign: i === 0 ? 'center' : i === 1 ? 'left' : 'right',
                      letterSpacing: '0.04em',
                    }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[
                  { n:1, t:'SPY 442C', e:'441.20', x:'448.90', b:'4', pl:'+$770', win:true },
                  { n:2, t:'SPY 445P', e:'443.80', x:'446.10', b:'2', pl:'−$230', win:false },
                  { n:3, t:'QQQ 380C', e:'378.50', x:'384.20', b:'6', pl:'+$570', win:true },
                  { n:4, t:'SPY 438P', e:'439.10', x:'435.60', b:'3', pl:'+$350', win:true },
                  { n:5, t:'QQQ 385C', e:'384.90', x:'383.40', b:'1', pl:'−$150', win:false },
                ].map(r => (
                  <tr key={r.n} style={{ background: r.win ? 'rgba(0,200,150,0.04)' : 'rgba(229,51,78,0.04)' }}>
                    <td style={{ padding: '8px 14px', textAlign: 'center', color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>{r.n}</td>
                    <td style={{ padding: '8px 14px', textAlign: 'left', fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', borderBottom: '1px solid var(--border)' }}>{r.t}</td>
                    <td style={{ padding: '8px 14px', textAlign: 'right', color: 'var(--text-secondary)', borderBottom: '1px solid var(--border)' }}>{r.e}</td>
                    <td style={{ padding: '8px 14px', textAlign: 'right', color: 'var(--text-secondary)', borderBottom: '1px solid var(--border)' }}>{r.x}</td>
                    <td style={{ padding: '8px 14px', textAlign: 'right', color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>{r.b}</td>
                    <td style={{ padding: '8px 14px', textAlign: 'right', color: r.win ? 'var(--bull)' : 'var(--bear)', fontWeight: 600, borderBottom: '1px solid var(--border)' }}>{r.pl}</td>
                    <td style={{ padding: '8px 14px', textAlign: 'right', borderBottom: '1px solid var(--border)' }}><Tag tone={r.win ? 'win' : 'loss'}>{r.win ? 'Win' : 'Loss'}</Tag></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <Callout tone="info" title="Data cached">
            Next fetch for this range will be instant. Results expire after 24 hours.
          </Callout>
        </>
      )}

      {!ran && !running && (
        <Card style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-muted)' }}>
          <i className="pi pi-chart-line" style={{ fontSize: 28, marginBottom: 12, color: 'var(--text-muted)' }} />
          <div style={{ fontSize: 14 }}>Configure a backtest and click <strong style={{ color: 'var(--text-primary)' }}>Run Backtest</strong> to see results.</div>
        </Card>
      )}
    </div>
  );
};

Object.assign(window, { StrategyLab });
