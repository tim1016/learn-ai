import { describe, expect, it } from 'vitest';

import { resolveLegacyDataLabUrl } from './data-lab-ingress';

const UUID = '6f9619ff-8b86-d011-b42d-00c04fc964ff';

// Which page an address opens is the router's (see the route inventory spec);
// this resolver only reports the workspace state a bookmark carried and what
// it ignored.
describe('resolveLegacyDataLabUrl', () => {
  it('a bare address carries nothing', () => {
    expect(resolveLegacyDataLabUrl('/data-lab')).toEqual({ params: {}, warnings: [] });
  });

  it('legacy ticker/range/trading-session query survives verbatim', () => {
    const res = resolveLegacyDataLabUrl(
      `/data-lab?ticker=AAPL&from=2026-01-02&to=2026-06-30&trading-session=regular`,
    );
    expect(res.params).toEqual({
      ticker: 'AAPL',
      from: '2026-01-02',
      to: '2026-06-30',
      'trading-session': 'regular',
    });
    expect(res.warnings).toEqual([]);
  });

  it('sessionId=<uuid> survives', () => {
    expect(resolveLegacyDataLabUrl(`/data-lab?sessionId=${UUID}`).params).toEqual({ sessionId: UUID });
  });

  it('ignores unknown keys with a note (indicator recipe treated opaquely in Phase 1)', () => {
    const res = resolveLegacyDataLabUrl(
      '/data-lab?ticker=AAPL&indicators=ema&ema.length=10&theme=dark&debug=1',
    );
    expect(res.params).toEqual({ ticker: 'AAPL' });
    expect(res.warnings.some(w => w.includes('"indicators"'))).toBe(true);
    expect(res.warnings.some(w => w.includes('"ema.length"'))).toBe(true);
    expect(res.warnings.some(w => w.includes('"theme"'))).toBe(true);
    expect(res.warnings.some(w => w.includes('"debug"'))).toBe(true);
    expect(res.warnings.some(w => w.includes('"ticker"'))).toBe(false);
  });

  it('ignores an invalid mode and an invalid sessionId with notes', () => {
    const res = resolveLegacyDataLabUrl('/data-lab?mode=hack&sessionId=not-a-uuid');
    expect(res.params).toEqual({});
    expect(res.warnings.some(w => w.includes('invalid mode'))).toBe(true);
    expect(res.warnings.some(w => w.includes('not a UUID'))).toBe(true);
  });

  it('says a mode on a canonical child is ignored because the address already names the page', () => {
    const res = resolveLegacyDataLabUrl('/data-lab/returns?mode=build&ticker=AAPL');
    expect(res.params).toEqual({ ticker: 'AAPL' });
    expect(res.warnings).toEqual(['Ignored mode "build": the address already names the page']);
  });

  it('a redirected bookmark keeps its ticker and range params', () => {
    // The /data-lab redirect keeps the query, so the shell first sees the
    // REDIRECTED URL on its child.
    const res = resolveLegacyDataLabUrl(
      '/data-lab/export?ticker=AAPL&from=2026-01-02&to=2026-06-30',
    );
    expect(res.params).toEqual({
      ticker: 'AAPL',
      from: '2026-01-02',
      to: '2026-06-30',
    });
    expect(res.warnings).toEqual([]);
  });

  it('ignores known keys with empty values', () => {
    const res = resolveLegacyDataLabUrl('/data-lab?ticker=');
    expect(res.params).toEqual({});
    expect(res.warnings.some(w => w.includes('"ticker"'))).toBe(true);
  });

  it('is pure — repeated calls with the same input agree', () => {
    const input = `/data-lab?mode=build&ticker=MSFT&sessionId=${UUID}`;
    expect(resolveLegacyDataLabUrl(input)).toEqual(resolveLegacyDataLabUrl(input));
  });
});
