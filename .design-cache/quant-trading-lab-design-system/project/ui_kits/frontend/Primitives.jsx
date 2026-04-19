const { useState } = React;

// Small shared primitives used across the kit.

const Eyebrow = ({ children, style }) => (
  <div style={{
    fontSize: 11, fontWeight: 700, textTransform: 'uppercase',
    letterSpacing: '0.05em', color: 'var(--text-muted)', ...style,
  }}>{children}</div>
);

const Button = ({ variant = 'primary', icon, children, onClick, style, disabled }) => {
  const base = {
    fontFamily: 'inherit', fontSize: 13, fontWeight: 600,
    padding: '8px 18px', borderRadius: 6, cursor: disabled ? 'not-allowed' : 'pointer',
    display: 'inline-flex', alignItems: 'center', gap: 6, border: 'none',
    transition: 'all 0.15s', opacity: disabled ? 0.55 : 1,
  };
  const variants = {
    primary: { background: 'var(--accent)', color: '#fff' },
    secondary: { background: 'var(--bg-elevated)', color: 'var(--text-secondary)', border: '1px solid var(--border)' },
    ghost: { background: 'transparent', color: 'var(--text-secondary)' },
    danger: { background: 'var(--bear)', color: '#fff' },
  };
  const [hover, setHover] = useState(false);
  const hoverStyles = {
    primary: { background: '#2563eb' },
    secondary: { background: 'var(--bg-hover)', color: 'var(--text-primary)' },
    ghost: { background: 'var(--bg-hover)', color: 'var(--text-primary)' },
    danger: { background: '#c92742' },
  };
  return (
    <button
      onClick={onClick} disabled={disabled}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{ ...base, ...variants[variant], ...(hover && !disabled ? hoverStyles[variant] : {}), ...style }}
    >
      {icon && <i className={`pi ${icon}`} style={{ fontSize: 11 }} />}
      {children}
    </button>
  );
};

const PresetPill = ({ active, children, onClick }) => (
  <button onClick={onClick} style={{
    padding: '3px 11px', fontSize: 11, fontWeight: 500, borderRadius: 12,
    border: '1px solid ' + (active ? 'var(--accent)' : 'var(--border)'),
    background: active ? 'var(--accent)' : 'transparent',
    color: active ? '#fff' : 'var(--text-muted)',
    cursor: 'pointer', fontFamily: 'inherit', transition: 'all 0.15s',
  }}>{children}</button>
);

const Input = ({ label, value, onChange, mono, ...rest }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
    {label && <label style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)' }}>{label}</label>}
    <input value={value} onChange={onChange} style={{
      background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      color: 'var(--text-primary)', padding: '7px 10px', borderRadius: 6,
      fontSize: 13, fontFamily: mono ? 'var(--font-mono)' : 'inherit', outline: 'none',
    }} {...rest} />
  </div>
);

const Select = ({ label, value, onChange, options }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
    {label && <label style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)' }}>{label}</label>}
    <select value={value} onChange={onChange} style={{
      background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      color: 'var(--text-primary)', padding: '7px 10px', borderRadius: 6,
      fontSize: 13, fontFamily: 'inherit', outline: 'none',
    }}>{options.map(o => <option key={o} value={o}>{o}</option>)}</select>
  </div>
);

const Card = ({ children, style, padding = '1.25rem' }) => (
  <div style={{
    background: 'var(--bg-surface)', border: '1px solid var(--border)',
    borderRadius: 10, padding, boxShadow: '0 1px 3px rgba(0,0,0,0.2)', ...style,
  }}>{children}</div>
);

const StatCard = ({ label, value, sub, tone }) => {
  const tones = {
    bull: { bg: 'rgba(0,200,150,0.08)', border: 'rgba(0,200,150,0.25)', fg: 'var(--bull)' },
    bear: { bg: 'rgba(229,51,78,0.08)', border: 'rgba(229,51,78,0.25)', fg: 'var(--bear)' },
    neutral: { bg: 'var(--bg-surface)', border: 'var(--border)', fg: 'var(--text-primary)' },
  };
  const t = tones[tone || 'neutral'];
  return (
    <div style={{
      background: t.bg, border: '1px solid ' + t.border, borderRadius: 10,
      padding: '16px 14px', display: 'flex', flexDirection: 'column', alignItems: 'center', textAlign: 'center',
    }}>
      <Eyebrow style={{ marginBottom: 6, fontSize: 10 }}>{label}</Eyebrow>
      <div style={{
        fontSize: 24, fontWeight: 700, color: t.fg,
        fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.01em', lineHeight: 1.15,
      }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>{sub}</div>}
    </div>
  );
};

const Badge = ({ tone = 'accent', icon, children }) => {
  const tones = {
    accent: { bg: 'var(--accent-soft)', fg: 'var(--accent)' },
    bull: { bg: 'var(--bull-soft)', fg: 'var(--bull)' },
    bear: { bg: 'var(--bear-soft)', fg: 'var(--bear)' },
    warn: { bg: 'rgba(245,158,11,0.12)', fg: 'var(--warn)' },
  }[tone];
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: '2px 10px', borderRadius: 9999, fontSize: 11, fontWeight: 600,
      background: tones.bg, color: tones.fg,
    }}>
      {icon && <i className={`pi ${icon}`} style={{ fontSize: 9 }} />}
      {children}
    </span>
  );
};

const Tag = ({ tone = 'win', children }) => {
  const t = {
    win: { bg: 'rgba(0,200,150,0.15)', fg: 'var(--bull)' },
    loss: { bg: 'rgba(229,51,78,0.15)', fg: 'var(--bear)' },
    depr: { bg: 'rgba(255,152,0,0.15)', fg: '#ff9800' },
  }[tone];
  return <span style={{
    display: 'inline-block', padding: '2px 7px', borderRadius: 4,
    fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em',
    background: t.bg, color: t.fg,
  }}>{children}</span>;
};

const Callout = ({ tone = 'info', title, children }) => {
  const tones = {
    info: { bg: '#eff6ff', border: '#2563eb', fg: '#334155', icon: 'pi-info-circle', iconColor: '#2563eb', titleColor: '#1e293b' },
    warn: { bg: '#fffbeb', border: '#d97706', fg: '#3a2608', icon: 'pi-exclamation-triangle', iconColor: '#d97706', titleColor: '#3a2608' },
    err: { bg: 'rgba(229,51,78,0.12)', border: 'var(--bear)', fg: '#fca5a5', icon: 'pi-times-circle', iconColor: 'var(--bear)', titleColor: '#fca5a5' },
  }[tone];
  return (
    <div style={{
      display: 'flex', gap: 10, padding: '10px 14px', borderRadius: 6, fontSize: 12, lineHeight: 1.5,
      background: tones.bg, borderLeft: '3px solid ' + tones.border, color: tones.fg,
    }}>
      <i className={`pi ${tones.icon}`} style={{ color: tones.iconColor, marginTop: 1 }} />
      <div>{title && <strong style={{ color: tones.titleColor, display: 'block', marginBottom: 2, fontSize: 11 }}>{title}</strong>}{children}</div>
    </div>
  );
};

Object.assign(window, { Eyebrow, Button, PresetPill, Input, Select, Card, StatCard, Badge, Tag, Callout });
