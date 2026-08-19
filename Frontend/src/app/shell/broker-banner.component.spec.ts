import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { vi } from 'vitest';
import { BrokerHealthService } from '../services/broker-health.service';
import { BrokerBannerComponent } from './broker-banner.component';

it('renders the preserved read-only IBKR market-data status', async () => {
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
          disconnect: vi.fn(),
        },
      },
    ],
  });

  expect(screen.getByText('Market data')).toBeTruthy();
  expect(screen.getByText('Paper connected')).toBeTruthy();
  expect(screen.queryByText(/host runner/i)).toBeNull();
});
