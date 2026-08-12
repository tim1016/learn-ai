import { beforeEach, describe, expect, it, vi } from 'vitest';

const chartMocks = vi.hoisted(() => ({
  createChart: vi.fn(() => ({})),
}));

vi.mock('lightweight-charts', () => ({
  createChart: chartMocks.createChart,
}));

import { createAppChart } from './chart-utils';

describe('createAppChart', () => {
  beforeEach(() => {
    chartMocks.createChart.mockClear();
  });

  it('preserves caller layout options while forcing the attribution logo off', () => {
    const container = document.createElement('div');

    createAppChart(container, {
      width: 640,
      layout: {
        textColor: '#f0f3fa',
        attributionLogo: true,
      },
    });

    expect(chartMocks.createChart).toHaveBeenCalledWith(container, {
      width: 640,
      layout: {
        textColor: '#f0f3fa',
        attributionLogo: false,
      },
    });
  });
});
