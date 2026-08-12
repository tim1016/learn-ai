import { describe, expect, it, vi } from 'vitest';
import type { IChartApi } from 'lightweight-charts';

import { createAppChart } from './chart-utils';

describe('createAppChart', () => {
  it('preserves caller layout options while forcing the attribution logo off', () => {
    const container = document.createElement('div');
    const chartFactory = vi.fn(() => ({}) as IChartApi);

    createAppChart(container, {
      width: 640,
      layout: {
        textColor: '#f0f3fa',
        attributionLogo: true,
      },
    }, chartFactory);

    expect(chartFactory).toHaveBeenCalledWith(container, {
      width: 640,
      layout: {
        textColor: '#f0f3fa',
        attributionLogo: false,
      },
    });
  });
});
