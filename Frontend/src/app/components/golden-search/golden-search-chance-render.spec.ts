import { describe, expect, it } from 'vitest';

import { init } from './charts/golden-search-echarts-setup';
import type { ChartOption } from './charts/golden-search-echarts';
import { chanceComparisonSpec } from './golden-search-chance-charts';
import { carryOverSpec, finalIntervalsSpec } from './golden-search-chance-final-charts';
import { rehearsalSeries, rehearsalSpec } from './golden-search-rehearsal-charts';
import { FAKE_CHART_THEME } from './testing/fake-charts';
import { chanceCarryOver, chanceComparison, chanceRehearsal } from './testing/fixtures';
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

function newChart(option: ChartOption) {
  const chart = init(null as unknown as HTMLElement, null, { renderer: 'svg', ssr: true, width: WIDTH, height: HEIGHT });
  chart.setOption(option as Parameters<typeof chart.setOption>[0], { notMerge: true });
  return chart;
}

function renderSvg(option: ChartOption): string {
  const chart = newChart(option);
  try {
    return (chart as unknown as { renderToSVGString(): string }).renderToSVGString();
  } finally {
    chart.dispose();
  }
}

function parse(svg: string): Document {
  return new DOMParser().parseFromString(svg, 'image/svg+xml');
}

interface PaintedPoint {
  x: number;
  y: number;
}

/**
 * Every point a path in the drawing visits — the geometry actually painted. zrender writes SVG paths with
 * absolute and relative commands and no spaces, so the commands are walked once, tracking absolute x and y.
 */
function paintedPoints(doc: Document): PaintedPoint[] {
  const points: PaintedPoint[] = [];
  for (const path of Array.from(doc.querySelectorAll('path'))) {
    const commands = (path.getAttribute('d') ?? '').match(/[MmLlHhVvZz][^MmLlHhVvZz]*/g) ?? [];
    let x = 0;
    let y = 0;
    for (const command of commands) {
      const name = command[0];
      const numbers = (command.slice(1).match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number.parseFloat);
      if (name === 'M') {
        [x, y] = numbers;
        points.push({ x, y });
      } else if (name === 'm') {
        x += numbers[0];
        y += numbers[1] ?? 0;
        points.push({ x, y });
      } else if (name === 'L') {
        [x, y] = numbers;
        points.push({ x, y });
      } else if (name === 'l') {
        x += numbers[0];
        y += numbers[1] ?? 0;
        points.push({ x, y });
      } else if (name === 'H') {
        x = numbers[0];
        points.push({ x, y });
      } else if (name === 'h') {
        x += numbers[0];
        points.push({ x, y });
      } else if (name === 'V') {
        y = numbers[0];
        points.push({ x, y });
      } else if (name === 'v') {
        y += numbers[0];
        points.push({ x, y });
      }
    }
  }
  return points;
}

function paintedXs(doc: Document): number[] {
  return paintedPoints(doc).map((point) => point.x);
}

/** Wide edges within 20px of each other belong to one band rectangle; each cluster is one row's center. */
function clusterRows(edges: number[]): number[] {
  const centers: number[] = [];
  let sum = 0;
  let count = 0;
  for (const edge of edges) {
    if (count > 0 && edge - (sum / count) > 20) {
      centers.push(sum / count);
      sum = 0;
      count = 0;
    }
    sum += edge;
    count += 1;
  }
  if (count > 0) centers.push(sum / count);
  return centers;
}

/**
 * The y positions of every painted wide horizontal run — the interval bands, far wider than the 3px median
 * ticks and, unlike axis and mark lines, horizontal. zrender writes a rect as ``M x y l w 0 l 0 h l -w 0 Z``.
 */
function bandRowCenters(doc: Document): number[] {
  const centers: number[] = [];
  for (const path of Array.from(doc.querySelectorAll('path'))) {
    const commands = (path.getAttribute('d') ?? '').match(/[MmLlHhVvZz][^MmLlHhVvZz]*/g) ?? [];
    let x = 0;
    let y = 0;
    for (const command of commands) {
      const name = command[0];
      const numbers = (command.slice(1).match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number.parseFloat);
      if (name === 'M') {
        x = numbers[0];
        y = numbers[1];
      } else if (name === 'L') {
        x = numbers[0];
        y = numbers[1] ?? y;
      } else if (name === 'l' || name === 'm') {
        const dx = numbers[0];
        const dy = numbers[1] ?? 0;
        if (Math.abs(dy) < 1 && Math.abs(dx) > 50) centers.push(y);
        x += dx;
        y += dy;
      } else if (name === 'H') {
        if (Math.abs(numbers[0] - x) > 50) centers.push(y);
        x = numbers[0];
      } else if (name === 'h') {
        if (Math.abs(numbers[0]) > 50) centers.push(y);
        x += numbers[0];
      } else if (name === 'V') {
        y = numbers[0];
      } else if (name === 'v') {
        y += numbers[0];
      }
    }
  }
  return [...new Set(centers.map((center) => Math.round(center)))].sort((a, b) => a - b);
}

/** A text node's painted position, from its transform or its coordinates. */
function textPosition(node: Element): { x: number; y: number } | null {
  const transform = node.getAttribute('transform');
  if (transform !== null) {
    const match = transform.match(/translate\((-?\d+(?:\.\d+)?)[, ]\s*(-?\d+(?:\.\d+)?)\)/);
    if (match !== null) return { x: Number.parseFloat(match[1]), y: Number.parseFloat(match[2]) };
  }
  const x = node.getAttribute('x');
  const y = node.getAttribute('y');
  return x !== null && y !== null ? { x: Number.parseFloat(x), y: Number.parseFloat(y) } : null;
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

describe('the final uncertainty charts, actually rendered by the installed ECharts (#3028)', () => {
  const GRID = { left: 168, right: 76, top: 42, bottom: 34 };

  function finalPlot(): { left: number; right: number; top: number; bottom: number } {
    return { left: GRID.left, right: WIDTH - GRID.right, top: 42, bottom: HEIGHT - GRID.bottom };
  }

  it('draws two distinct finite interval rows inside the plot, not one overlaid NaN band', () => {
    const comparison = chanceComparison({ difference_range: { p5: -0.3, p50: -0.1, p95: 0.1, finite: 10_000 }, candidate_range: { p5: -0.2, p50: 0.05, p95: 0.3, finite: 10_000 } });
    const svg = renderSvg(finalIntervalsSpec(ALL_PERIOD, comparison, 'all_period').option(FAKE_CHART_THEME));
    expect(svg).not.toContain('NaN');
    const doc = parse(svg);
    const points = paintedPoints(doc);
    expect(points.length).toBeGreaterThan(0);
    const plot = finalPlot();
    for (const point of points) {
      expect(point.x).toBeGreaterThanOrEqual(plot.left - 1);
      expect(point.x).toBeLessThanOrEqual(plot.right + 1);
      expect(point.y).toBeGreaterThanOrEqual(0);
      expect(point.y).toBeLessThanOrEqual(HEIGHT);
    }
    // The two rows are two painted band rectangles at separated heights — not axis furniture, not one
    // overlaid row, and not the nothing a NaN coordinate leaves behind. Each band paints a top and a bottom
    // wide edge 14px apart, so the edges are clustered into their rows.
    const rows = clusterRows(bandRowCenters(doc));
    expect(rows.length).toBe(2);
    expect(Math.abs(rows[1] - rows[0])).toBeGreaterThan(15);
  });

  it('keeps a recorded final result far above the curve visible inside the plot', () => {
    const carry = chanceCarryOver({ recorded_objective: 8, rank: 1.0, below: 10_000, label: 'above_most' });
    const svg = renderSvg(carryOverSpec(ALL_PERIOD, carry, 'all_period').option(FAKE_CHART_THEME));
    expect(svg).not.toContain('NaN');
    const doc = parse(svg);
    const marker = Array.from(doc.querySelectorAll('text')).find((node) => (node.textContent ?? '').includes('recorded final result'));
    expect(marker).toBeDefined();
    const position = marker !== undefined ? textPosition(marker) : null;
    const carryPlot = { top: 26, bottom: HEIGHT - 46 };  // the carry-over chart's own grid
    expect(position).not.toBeNull();
    if (position !== null) {
      expect(position.y).toBeGreaterThan(carryPlot.top);
      expect(position.y).toBeLessThan(carryPlot.bottom);
    }
    // The curve itself still paints, far below the marker.
    const points = paintedPoints(doc);
    expect(points.length).toBeGreaterThan(0);
  });

  it('keeps a recorded final result far below the curve visible inside the plot', () => {
    const carry = chanceCarryOver({ recorded_objective: -5, rank: 0.0, below: 0, label: 'below_most' });
    const svg = renderSvg(carryOverSpec(ALL_PERIOD, carry, 'all_period').option(FAKE_CHART_THEME));
    expect(svg).not.toContain('NaN');
    const doc = parse(svg);
    const marker = Array.from(doc.querySelectorAll('text')).find((node) => (node.textContent ?? '').includes('recorded final result'));
    expect(marker).toBeDefined();
    const position = marker !== undefined ? textPosition(marker) : null;
    const carryPlot = { top: 26, bottom: HEIGHT - 46 };  // the carry-over chart's own grid
    expect(position).not.toBeNull();
    if (position !== null) {
      expect(position.y).toBeGreaterThan(carryPlot.top);
      expect(position.y).toBeLessThan(carryPlot.bottom);
    }
  });

  it('draws an in-range marker between the curve and a flat curve without NaN', () => {
    const inRange = chanceCarryOver({ recorded_objective: 0.03 });
    const inRangeSvg = renderSvg(carryOverSpec(ALL_PERIOD, inRange, 'all_period').option(FAKE_CHART_THEME));
    expect(inRangeSvg).not.toContain('NaN');
    const flat = chanceCarryOver({ curve: { p: Array.from({ length: 99 }, () => 0.05), finite: 10_000 }, recorded_objective: 0.05 });
    const flatSvg = renderSvg(carryOverSpec(ALL_PERIOD, flat, 'all_period').option(FAKE_CHART_THEME));
    expect(flatSvg).not.toContain('NaN');
    expect(paintedPoints(parse(flatSvg)).length).toBeGreaterThan(0);
  });

  it('draws no band but no NaN when a stored range holds no finite path', () => {
    const comparison = chanceComparison({ difference_range: { p5: null, p50: null, p95: null, finite: 0 }, candidate_range: { p5: null, p50: null, p95: null, finite: 0 } });
    const svg = renderSvg(finalIntervalsSpec(ALL_PERIOD, comparison, 'all_period').option(FAKE_CHART_THEME));
    expect(svg).not.toContain('NaN');
    // The unavailable-range note reads in the tooltip copy, and the chart still paints its axes and zero line.
    expect(paintedPoints(parse(svg)).length).toBeGreaterThan(0);
  });
});

describe('the chance charts, checked through the rendered instance (#3031 approach, consolidated)', () => {
  it.each([
    [-0.3, 0.05, 0.4],
    [0.05, 0.2, 0.7],
    [-0.9, -0.5, -0.1],
    [1500, 2000, 3200],
  ])('keeps every percentile and zero inside the grid: %j', (p5, p50, p95) => {
    const reading = chanceComparison({ difference_range: { p5, p50, p95, finite: 10_000 } });
    const chart = newChart(chanceComparisonSpec(ALL_PERIOD, reading, null).option(FAKE_CHART_THEME) as ChartOption);
    try {
      for (const value of [p5, p50, p95, 0]) {
        const pixel = chart.convertToPixel({ gridIndex: 0 }, [value, 0]);
        expect(Array.isArray(pixel) && pixel.every(Number.isFinite)).toBe(true);
        expect(chart.containPixel({ gridIndex: 0 }, pixel)).toBe(true);
      }
    } finally {
      chart.dispose();
    }
  });

  it('draws two painted band rectangles on ordered rows, first row on top', () => {
    const chart = newChart(finalIntervalsSpec(ALL_PERIOD, chanceComparison(), null).option(FAKE_CHART_THEME) as ChartOption);
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- zrender's storage display list is untyped through the public surface.
      const bands = (chart.getZr().storage.getDisplayList() as any[]).filter((element) => element.type === 'rect' && element.style?.['opacity'] === 0.28).map((element) => element.getBoundingRect());
      expect(bands).toHaveLength(2);
      expect(new Set(bands.map((rect: { y: number }) => rect.y)).size).toBe(2);
      const difference = chart.convertToPixel({ gridIndex: 0 }, [0, 0]);
      const ownResult = chart.convertToPixel({ gridIndex: 0 }, [0, 1]);
      expect(difference[1]).toBeLessThan(ownResult[1]);  // the difference row reads first, top to bottom
      for (const rect of bands) {
        expect([rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)).toBe(true);
        expect(rect.x).toBeGreaterThanOrEqual(0);
        expect(rect.y).toBeGreaterThanOrEqual(0);
        expect(rect.x + rect.width).toBeLessThanOrEqual(chart.getWidth());
        expect(rect.y + rect.height).toBeLessThanOrEqual(chart.getHeight());
      }
    } finally {
      chart.dispose();
    }
  });

  it.each([-8, 8])('keeps an extreme recorded final objective inside the grid: %s', (recorded) => {
    const chart = newChart(carryOverSpec(ALL_PERIOD, chanceCarryOver({ recorded_objective: recorded }), null).option(FAKE_CHART_THEME) as ChartOption);
    try {
      const pixel = chart.convertToPixel({ gridIndex: 0 }, [50, recorded]);
      expect(Array.isArray(pixel) && pixel.every(Number.isFinite)).toBe(true);
      expect(chart.containPixel({ gridIndex: 0 }, pixel)).toBe(true);
    } finally {
      chart.dispose();
    }
  });
});
