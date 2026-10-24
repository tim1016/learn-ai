import { describe, expect, it } from 'vitest';

import { resolveLegacyDataLabUrl } from './data-lab-ingress';

const UUID = '6f9619ff-8b86-d011-b42d-00c04fc964ff';

describe('resolveLegacyDataLabUrl — PRD §14 table', () => {
  it('/data-lab → /data-lab/explore', () => {
    expect(resolveLegacyDataLabUrl('/data-lab')).toEqual({
      route: '/data-lab/explore',
      params: {},
      warnings: [],
    });
  });

  it.each([
    ['?mode=explore → /data-lab/explore', '/data-lab?mode=explore', '/data-lab/explore'],
    ['?mode=build → /data-lab/export', '/data-lab?mode=build', '/data-lab/export'],
    ['?mode=export → /data-lab/export', '/data-lab?mode=export', '/data-lab/export'],
    ['?mode=validate → Operations → Data checks → TradingView', '/data-lab?mode=validate', '/operations/data-checks/tradingview'],
    [
      'an explicit mode still overrides an already-canonical child path (/data-lab/returns?mode=build → /data-lab/export)',
      '/data-lab/returns?mode=build',
      '/data-lab/export',
    ],
  ])('%s', (_name, url, route) => {
    expect(resolveLegacyDataLabUrl(url).route).toBe(route);
  });

  it('legacy ticker/range/trading-session query survives verbatim', () => {
    const res = resolveLegacyDataLabUrl(
      `/data-lab?ticker=AAPL&from=2026-01-02&to=2026-06-30&trading-session=regular`,
    );
    expect(res.route).toBe('/data-lab/explore');
    expect(res.params).toEqual({
      ticker: 'AAPL',
      from: '2026-01-02',
      to: '2026-06-30',
      'trading-session': 'regular',
    });
    expect(res.warnings).toEqual([]);
  });

  it('sessionId=<uuid> keeps Explore unless mode explicitly selects Export/Validate', () => {
    const explore = resolveLegacyDataLabUrl(`/data-lab?sessionId=${UUID}`);
    expect(explore.route).toBe('/data-lab/explore');
    expect(explore.params).toEqual({ sessionId: UUID });

    const exportSel = resolveLegacyDataLabUrl(`/data-lab?sessionId=${UUID}&mode=build`);
    expect(exportSel.route).toBe('/data-lab/export');
    expect(exportSel.params).toEqual({ sessionId: UUID });

    const validateSel = resolveLegacyDataLabUrl(`/data-lab?sessionId=${UUID}&mode=validate`);
    expect(validateSel.route).toBe('/operations/data-checks/tradingview');
  });

  it('drops unknown keys with a warning (indicator recipe treated opaquely in Phase 1)', () => {
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

  it('drops an invalid mode and an invalid sessionId with warnings', () => {
    const res = resolveLegacyDataLabUrl('/data-lab?mode=hack&sessionId=not-a-uuid');
    expect(res.route).toBe('/data-lab/explore');
    expect(res.params).toEqual({});
    expect(res.warnings.some(w => w.includes('invalid mode'))).toBe(true);
    expect(res.warnings.some(w => w.includes('not a UUID'))).toBe(true);
  });

  it('keeps a redirected child and its scope params', () => {
    // The /data-lab redirect preserves the query string, so the shell first
    // sees the REDIRECTED URL. The resolver must keep that child and the
    // scope params — keying only on the original legacy path would drop them.
    const res = resolveLegacyDataLabUrl(
      '/data-lab/export?ticker=AAPL&from=2026-01-02&to=2026-06-30',
    );
    expect(res.route).toBe('/data-lab/export');
    expect(res.params).toEqual({
      ticker: 'AAPL',
      from: '2026-01-02',
      to: '2026-06-30',
    });
    expect(res.warnings).toEqual([]);
  });

  it('drops known keys with empty values', () => {
    const res = resolveLegacyDataLabUrl('/data-lab?ticker=');
    expect(res.params).toEqual({});
    expect(res.warnings.some(w => w.includes('"ticker"'))).toBe(true);
  });

  it('is pure — repeated calls with the same input agree', () => {
    const input = `/data-lab?mode=build&ticker=MSFT&sessionId=${UUID}`;
    expect(resolveLegacyDataLabUrl(input)).toEqual(resolveLegacyDataLabUrl(input));
  });
});
