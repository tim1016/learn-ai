import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createChart } from 'lightweight-charts';

vi.mock('lightweight-charts', () => ({
  createChart: vi.fn(() => ({})),
}));

import { createAppChart } from './chart-utils';

describe('createAppChart', () => {
  beforeEach(() => {
    vi.mocked(createChart).mockClear();
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

    expect(createChart).toHaveBeenCalledWith(container, {
      width: 640,
      layout: {
        textColor: '#f0f3fa',
        attributionLogo: false,
      },
    });
  });
});
