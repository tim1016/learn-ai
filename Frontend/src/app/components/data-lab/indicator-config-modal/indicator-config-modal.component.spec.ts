import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { IndicatorConfigModalComponent } from './indicator-config-modal.component';

const EMA = { name: 'ema', params: { length: 10 } };
const EMA_PARAMS = [{ name: 'length', type: 'int' as const, default: 10, min: 1, max: 500, description: 'Window length' }];

describe('IndicatorConfigModalComponent related indicators', () => {
  it('offers the related indicators to a host that can add them, and tells it which one was picked', async () => {
    const addRelated = vi.fn();
    await render(IndicatorConfigModalComponent, {
      inputs: { visible: true, mode: 'configure', entry: EMA, paramConfigs: EMA_PARAMS },
      on: { addRelated },
    });

    await userEvent.setup().click(await screen.findByRole('button', { name: /SMA/ }));

    expect(addRelated).toHaveBeenCalledWith('sma');
  });

  it('leaves them out for a host that holds one indicator, so no chip claims an add it cannot make', async () => {
    await render(IndicatorConfigModalComponent, {
      inputs: { visible: true, mode: 'configure', entry: EMA, paramConfigs: EMA_PARAMS, showRelated: false },
    });

    expect(await screen.findByRole('heading', { name: /Exponential Moving Average/ })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Related indicators' })).toBeNull();
  });
});
