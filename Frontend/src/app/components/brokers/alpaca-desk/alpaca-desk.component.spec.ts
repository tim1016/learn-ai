import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskComponent } from './alpaca-desk.component';

describe('AlpacaDeskComponent', () => {
  it('renders the Alpaca desk heading and subtitle', async () => {
    await render(AlpacaDeskComponent, {
      providers: [
        {
          provide: BrokersService,
          useValue: {
            getAccount: vi.fn().mockResolvedValue(undefined),
            listPositions: vi.fn().mockResolvedValue([]),
            listOrders: vi.fn().mockResolvedValue([]),
            getClerkStatus: vi.fn().mockResolvedValue({
              broker: 'alpaca',
              account_id: 'PA1',
              hold: { active: false, reason_code: null, reason: null, since_ms: null },
              latest_reconciliation: null,
              outstanding_intents: 0,
              observed_at_ms: 1,
            }),
          },
        },
      ],
    });

    expect(screen.getByRole('heading', { name: /Alpaca/i })).toBeTruthy();
    expect(screen.getByText(/Broker desk · Broker System v2/i)).toBeTruthy();
  });
});
