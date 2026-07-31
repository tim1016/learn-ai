import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../services/brokers.service';
import { BrokerDeployPageComponent } from './broker-deploy-page.component';

describe('BrokerDeployPageComponent', () => {
  it('renders a bounded account-resolution error instead of leaving Deploy blank', async () => {
    const getAccount = vi.fn().mockRejectedValue(new Error('Alpaca unavailable'));

    await render(BrokerDeployPageComponent, {
      providers: [{ provide: BrokersService, useValue: { getAccount } }],
      componentInputs: { broker: 'alpaca' },
    });

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Alpaca account context is unavailable.');
    expect(alert.textContent).toContain('Restore the paper account connection');
    expect(getAccount).toHaveBeenCalledWith('alpaca');
  });
});
