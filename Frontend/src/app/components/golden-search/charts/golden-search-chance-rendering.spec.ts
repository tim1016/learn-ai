import { use } from 'echarts/core';
import { SVGRenderer } from 'echarts/renderers';
import { describe, expect, it } from 'vitest';
import { chanceComparisonSpec } from '../golden-search-chance-charts';
import { carryOverSpec, finalIntervalsSpec } from '../golden-search-chance-final-charts';
import { rehearsalSeries, rehearsalSpec } from '../golden-search-rehearsal-charts';
import { FAKE_CHART_THEME } from '../testing/fake-charts';
import { chanceCarryOver, chanceComparison, chanceRehearsal } from '../testing/fixtures';
import type { ChartSpec } from './golden-search-chart-spec';
import { init } from './golden-search-echarts-setup';

// Use the production registrations; SVG makes actual geometry observable
// without a browser or canvas. These catch #3027 and #3028's blank charts.
use([SVGRenderer]);
const FIT = { key: 'all_period' as const, label: 'All-period fit' };

function chartFor(spec: ChartSpec) {
  const chart = init(null, null, { renderer: 'svg', ssr: true, width: 800, height: 260 });
  chart.setOption({ ...spec.option(FAKE_CHART_THEME), animation: false });
  return chart;
}

describe('chance readings rendered by the production chart library', () => {
  it.each([[-0.3, 0.05, 0.4], [0.05, 0.2, 0.7], [-0.9, -0.5, -0.1], [1500, 2000, 3200]])('keeps the full difference interval visible: %j', (p5, p50, p95) => {
    const reading = chanceComparison({ difference_range: { p5, p50, p95, finite: 10_000 } });
    const chart = chartFor(chanceComparisonSpec(FIT, reading, null));
    try {
      for (const value of [p5, p50, p95, 0]) {
        const pixel = chart.convertToPixel({ gridIndex: 0 }, [value, 0]);
        expect(Array.isArray(pixel) && pixel.every(Number.isFinite)).toBe(true);
        expect(chart.containPixel({ gridIndex: 0 }, pixel)).toBe(true);
      }
    } finally { chart.dispose(); }
  });

  it('draws two visible bands on distinct final-result rows', () => {
    const chart = chartFor(finalIntervalsSpec(FIT, chanceComparison(), null));
    try {
      const bands = chart.getZr().storage.getDisplayList().filter(element => element.type === 'rect' && element.style['opacity'] === 0.28).map(element => element.getBoundingRect());
      expect(bands).toHaveLength(2);
      expect(new Set(bands.map(rect => rect.y)).size).toBe(2);
      const difference = chart.convertToPixel({ gridIndex: 0 }, [0, 0]);
      const ownResult = chart.convertToPixel({ gridIndex: 0 }, [0, 1]);
      expect(difference[1]).toBeLessThan(ownResult[1]);
      for (const rect of bands) {
        expect([rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)).toBe(true);
        expect(rect.x).toBeGreaterThanOrEqual(0);
        expect(rect.y).toBeGreaterThanOrEqual(0);
        expect(rect.x + rect.width).toBeLessThanOrEqual(chart.getWidth());
        expect(rect.y + rect.height).toBeLessThanOrEqual(chart.getHeight());
      }
    } finally { chart.dispose(); }
  });

  it.each([-8, 8])('keeps an extreme recorded final objective on screen: %s', recorded => {
    const chart = chartFor(carryOverSpec(FIT, chanceCarryOver({ recorded_objective: recorded }), null));
    try {
      const pixel = chart.convertToPixel({ gridIndex: 0 }, [50, recorded]);
      expect(Array.isArray(pixel) && pixel.every(Number.isFinite)).toBe(true);
      expect(chart.containPixel({ gridIndex: 0 }, pixel)).toBe(true);
    } finally { chart.dispose(); }
  });

  it('shows every rehearsal series in the legend', () => {
    const reading = chanceRehearsal();
    const chart = chartFor(rehearsalSpec(FIT, reading, null));
    try {
      const svg = chart.renderToSVGString();
      for (const series of rehearsalSeries(reading)) expect(svg).toContain(series.label);
    } finally { chart.dispose(); }
  });
});
