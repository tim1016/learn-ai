import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import axe from 'axe-core';
import { BrokerHealthService } from '../services/broker-health.service';
import { BrokerBannerComponent } from './broker-banner.component';

it('renders the compact IB Gateway control with an accessible connection action', async () => {
  const user = userEvent.setup();
  const disconnect = vi.fn().mockResolvedValue(undefined);
  const health = signal({
    connected: true,
    is_paper: true,
    account_id: 'DU123',
    connection_state: 'connected',
    condition: null,
  });
  await render(BrokerBannerComponent, {
    providers: [
      {
        provide: BrokerHealthService,
        useValue: {
          health,
          bannerState: signal('paper'),
          lifecycleAction: signal(null),
          connect: vi.fn(),
          disconnect,
        },
      },
    ],
  });

  const control = screen.getByRole('button', { name: 'Disconnect IBKR market data' });
  expect(control.textContent).toContain('IB');
  expect(screen.queryByText('Market data')).toBeNull();

  await user.click(control);
  expect(disconnect).toHaveBeenCalledOnce();

  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  expect(results.violations).toEqual([]);
});
