import { render } from '@testing-library/angular';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { KindDistribution } from '../returns-distribution.service';
import { ReturnsHistogramChartComponent } from './returns-histogram-chart.component';

/** chart.js under jsdom renders through the test-setup canvas shim, but its
 * click wiring is what this spec must prove — so the module is replaced with
 * a recording fake and the page invokes the same options.onClick the library
 * would. */
const harness = vi.hoisted(() => {
  return {
    instances: [] as {
      config: {
        data: { labels: string[]; datasets: { data: unknown[]; type?: string }[] };
        options: { onClick: ((event: unknown, elements: { index: number }[]) => void) | undefined };
      };
      destroy: ReturnType<typeof vi.fn>;
    }[],
  };
});

vi.mock('chart.js', () => {
  return {
    Chart: class {
      static register(..._args: unknown[]): void {}
      destroy = vi.fn();
      constructor(
        _canvas: unknown,
        config: {
          data: { labels: string[]; datasets: { data: unknown[]; type?: string }[] };
          options: { onClick?: (event: unknown, elements: { index: number }[]) => void };
        },
      ) {
        harness.instances.push({ config: config as never, destroy: this.destroy });
      }
    },
    registerables: [],
  };
});

const DISTRIBUTION: KindDistribution = {
  kind: 'close_to_close',
  bins: [
    { lowerPct: null, upperPct: -5, count: 2, isEdge: true },
    { lowerPct: -0.5, upperPct: 0, count: 120, isEdge: false },
    { lowerPct: 0, upperPct: 0.5, count: 130, isEdge: false },
    { lowerPct: 5, upperPct: null, count: 1, isEdge: true },
  ],
  normalExpectedCounts: [0.4, 118.2, 126.1, 0.2],
  stats: {
    nDays: 253,
    meanPct: 0.04,
    stdPct: 0.98,
    annualizedVolPct: 15.6,
    skewness: -0.2,
    excessKurtosis: 1.7,
    var95Pct: -1.5,
    cvar95Pct: -2.1,
    bestDay: { sessionOpenMsUtc: 0, valuePct: 2.9 },
    worstDay: { sessionOpenMsUtc: 0, valuePct: -5.4 },
  },
};

beforeEach(() => {
  harness.instances.length = 0;
});

describe('ReturnsHistogramChartComponent', () => {
  it('draws one bar per bin with the normal overlay line and closed label set', async () => {
    await render(ReturnsHistogramChartComponent, {
      componentInputs: { distribution: DISTRIBUTION, selectedBinIndex: null },
    });

    expect(harness.instances).toHaveLength(1);
    const chart = harness.instances[0]!;
    expect(chart.config.data.labels).toEqual(['< -5.0%', '-0.5…0.0%', '0.0…0.5%', '≥ 5.0%']);
    expect(chart.config.data.datasets[0]!.data).toEqual([2, 120, 130, 1]);
    expect(chart.config.data.datasets[1]!.type).toBe('line');
    expect(chart.config.data.datasets[1]!.data).toEqual(DISTRIBUTION.normalExpectedCounts);
  });

  it('emits the clicked bin index through the chart click handler', async () => {
    const emitted: number[] = [];
    const { fixture } = await render(ReturnsHistogramChartComponent, {
      componentInputs: { distribution: DISTRIBUTION, selectedBinIndex: null },
    });
    fixture.componentInstance.binSelected.subscribe((index: number) => emitted.push(index));

    harness.instances[0]!.config.options.onClick!(null, [{ index: 2 }]);

    expect(emitted).toEqual([2]);
  });

  it('destroys the previous chart before re-rendering on input change', async () => {
    const { rerender } = await render(ReturnsHistogramChartComponent, {
      componentInputs: { distribution: DISTRIBUTION, selectedBinIndex: null },
    });
    await rerender({
      componentInputs: {
        distribution: { ...DISTRIBUTION, bins: DISTRIBUTION.bins.slice(0, 2), normalExpectedCounts: [0.4, 118.2] },
        selectedBinIndex: null,
      },
    });

    expect(harness.instances).toHaveLength(2);
    expect(harness.instances[0]!.destroy).toHaveBeenCalled();
  });
});
