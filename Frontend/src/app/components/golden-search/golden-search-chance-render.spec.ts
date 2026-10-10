import { describe, expect, it } from 'vitest';

import { init } from './charts/golden-search-echarts-setup';
import type { ChartOption } from './charts/golden-search-echarts';
import { chanceComparisonSpec } from './golden-search-chance-charts';
import { rehearsalSeries, rehearsalSpec } from './golden-search-rehearsal-charts';
import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceComparison, chanceRehearsal } from './testing/fixtures';
import type { CandidateRef } from './golden-search.types';
import { install as SvgRenderer } from 'echarts/lib/renderer/installSVGRenderer';
import { use } from 'echarts/core';

// The rendering tests draw with the installed ECharts through the production modular setup, in SSR SVG mode
// so no canvas is needed (#3027, #3028): the geometry in the SVG string is the geometry the canvas would
// draw, so a band outside the plot or a legend that never registered is a failure here, not a silent option.
use([SvgRenderer]);

const ALL_PERIOD: CandidateRef = { key: 'all_period', label: 'All-period fit' };
const WIDTH = 800;
const HEIGHT = 300;

function renderSvg(option: ChartOption): string {
  const chart = init(null as unknown as HTMLElement, null, { renderer: 'svg', ssr: true, width: WIDTH, height: HEIGHT });
  try {
    chart.setOption(option as Parameters<typeof chart.setOption>[0], { notMerge: true });
    return (chart as unknown as { renderToSVGString(): string }).renderToSVGString();
  } finally {
    chart.dispose();
  }
}

function parse(svg: string): Document {
  return new DOMParser().parseFromString(svg, 'image/svg+xml');
}

/**
 * Every x coordinate a path in the drawing visits — the geometry actually painted. zrender writes SVG paths
 * with absolute and relative commands and no spaces, so the commands are walked once, tracking absolute x.
 */
function paintedXs(doc: Document): number[] {
  const xs: number[] = [];
  for (const path of Array.from(doc.querySelectorAll('path'))) {
    const commands = (path.getAttribute('d') ?? '').match(/[MmLlHhVvZz][^MmLlHhVvZz]*/g) ?? [];
    let x = 0;
    for (const command of commands) {
      const name = command[0];
      const numbers = (command.slice(1).match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number.parseFloat);
      if (name === 'M') {
        x = numbers[0];
        xs.push(x);
      } else if (name === 'm') {
        x += numbers[0];
        xs.push(x);
      } else if (name === 'L' || name === 'H') {
        x = numbers[0];
        xs.push(x);
      } else if (name === 'l' || name === 'h') {
        x += numbers[0];
        xs.push(x);
      }
      // V, v and Z move vertically or close; they add no new x.
    }
  }
  return xs;
}

function paintedTexts(doc: Document): string[] {
  return Array.from(doc.querySelectorAll('text')).map((node) => node.textContent ?? '');
}

/** The plot's horizontal extent for a grid, in pixels. */
function plotBounds(grid: { left: number; right: number }): { left: number; right: number } {
  return { left: grid.left, right: WIDTH - grid.right };
}

describe('the Golden Search chance charts, actually rendered by the installed ECharts (#3027)', () => {
  it('keeps an entirely negative difference interval inside the plot, endpoints visible', () => {
    const comparison = chanceComparison({ difference_range: { p5: -0.3, p50: -0.1, p95: -0.02, finite: 10_000 } });
    const option = chanceComparisonSpec(ALL_PERIOD, comparison, 'all_period').option(FAKE_CHART_THEME) as ChartOption & { grid: { left: number; right: number } };
    const bounds = plotBounds(option.grid);
    const xs = paintedXs(parse(renderSvg(option)));
    expect(xs.length).toBeGreaterThan(0);
    // Every painted x sits inside the plot; the interval spans most of it rather than a sliver at the edge.
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(bounds.left - 1);
    expect(Math.max(...xs)).toBeLessThanOrEqual(bounds.right + 1);
    expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan((bounds.right - bounds.left) * 0.5);
  });

  it('keeps a cross-zero interval inside the plot with zero visibly between the endpoints', () => {
    const comparison = chanceComparison({ difference_range: { p5: -0.3, p50: -0.1, p95: 0.1, finite: 10_000 } });
    const option = chanceComparisonSpec(ALL_PERIOD, comparison, 'all_period').option(FAKE_CHART_THEME) as ChartOption & { grid: { left: number; right: number } };
    const bounds = plotBounds(option.grid);
    const xs = paintedXs(parse(renderSvg(option)));
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(bounds.left - 1);
    expect(Math.max(...xs)).toBeLessThanOrEqual(bounds.right + 1);
    // The zero mark sits strictly between the interval's painted ends, so crossing is visible.
    const zero = xs.find((x) => Math.abs(x - (bounds.left + bounds.right) / 2) < (bounds.right - bounds.left) / 2);
    expect(zero).toBeDefined();
    expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan((bounds.right - bounds.left) * 0.6);
  });

  it('keeps an all-tie degenerate interval visible rather than collapsing the axis', () => {
    const comparison = chanceComparison({ difference_range: { p5: 0, p50: 0, p95: 0, finite: 10_000 } });
    const option = chanceComparisonSpec(ALL_PERIOD, comparison, 'all_period').option(FAKE_CHART_THEME) as ChartOption & { grid: { left: number; right: number } };
    const bounds = plotBounds(option.grid);
    const xs = paintedXs(parse(renderSvg(option)));
    expect(xs.length).toBeGreaterThan(0);
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(bounds.left - 1);
    expect(Math.max(...xs)).toBeLessThanOrEqual(bounds.right + 1);
  });

  it('renders the rehearsal legend labels through the registered component, so rules are readable without hover', () => {
    const rehearsal = { ...chanceRehearsal(), active_rules: ['drawdown', 'exits', 'objective'] as string[] };
    const option = rehearsalSpec(ALL_PERIOD, rehearsal, 'all_period').option(FAKE_CHART_THEME);
    const texts = paintedTexts(parse(renderSvg(option))).join('\n');
    for (const series of rehearsalSeries(rehearsal)) {
      expect(texts).toContain(series.label);
    }
    // A rule the plan does not model — the positive-net rule, off in this fixture — gets no legend entry.
    expect(rehearsal.active_rules).not.toContain('positive_net');
    expect(texts).not.toContain('Positive net');
  });
});
