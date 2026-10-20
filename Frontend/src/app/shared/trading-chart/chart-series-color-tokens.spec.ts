import { describe, expect, it } from 'vitest';

// The TS-hex ↔ _tokens.scss lockstep is enforced by the Node guard
// `scripts/verify-chart-series-color-tokens-guard.cjs` (run via
// `npm run test:guards`); this spec covers registry behavior and contrast.
import {
  CHART_SERIES_COLOR_TOKENS,
  CHART_SERIES_ELIGIBLE_TOKENS,
  CHART_SERIES_SURFACE_HEX,
  ChartSeriesColorTokenDef,
  isChartSeriesColorToken,
} from './chart-series-color-tokens';

// ── WCAG relative luminance / contrast ────────────────────────
function channelToLinear(c: number): number {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(hex: string): number {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return 0.2126 * channelToLinear(r) + 0.7152 * channelToLinear(g) + 0.0722 * channelToLinear(b);
}

function contrastRatio(fgHex: string, bgHex: string): number {
  const l1 = relativeLuminance(fgHex);
  const l2 = relativeLuminance(bgHex);
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

describe('CHART_SERIES_COLOR_TOKENS', () => {
  it('every eligible token has >= 3:1 WCAG contrast on the chart surface (TV-dark)', () => {
    const surface = CHART_SERIES_SURFACE_HEX;
    for (const id of CHART_SERIES_ELIGIBLE_TOKENS) {
      const def = CHART_SERIES_COLOR_TOKENS.get(id) as ChartSeriesColorTokenDef;
      const ratio = contrastRatio(def.hex, surface);
      expect(
        ratio,
        `${id} (${def.hex}) contrast ${ratio.toFixed(2)}:1 on ${surface}`,
      ).toBeGreaterThanOrEqual(3);
    }
  });

  it('registers ten eligible tokens, accepts only registered IDs, and keeps the registry and eligible list frozen', () => {
    expect.soft(CHART_SERIES_ELIGIBLE_TOKENS, 'registers ten eligible tokens').toHaveLength(10);
    for (const id of CHART_SERIES_ELIGIBLE_TOKENS) {
      expect.soft(CHART_SERIES_COLOR_TOKENS.get(id), `registers a definition for eligible token ${id}`).toBeDefined();
    }

    expect.soft(isChartSeriesColorToken('series-blue'), 'isChartSeriesColorToken accepts a registered ID').toBe(true);
    expect.soft(isChartSeriesColorToken('series-blue '), 'isChartSeriesColorToken rejects a registered ID with trailing whitespace').toBe(false);
    expect.soft(isChartSeriesColorToken('#4d8dff'), 'isChartSeriesColorToken rejects a hex value').toBe(false);
    expect.soft(isChartSeriesColorToken('var(--chart-series-blue)'), 'isChartSeriesColorToken rejects a CSS variable reference').toBe(false);
    expect.soft(isChartSeriesColorToken(null), 'isChartSeriesColorToken rejects null').toBe(false);
    expect.soft(isChartSeriesColorToken(42), 'isChartSeriesColorToken rejects a number').toBe(false);

    expect.soft(Object.isFrozen(CHART_SERIES_ELIGIBLE_TOKENS), 'the eligible list is frozen').toBe(true);
    expect.soft(() => {
      (CHART_SERIES_COLOR_TOKENS as Map<string, unknown>).set('series-hack', {} as never);
    }, 'the registry rejects a new entry').toThrow();
  });
});
