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
          },
        },
      ],
    });

    expect(screen.getByRole('heading', { name: /Alpaca/i })).toBeTruthy();
    expect(screen.getByText(/Broker desk · Broker System v2/i)).toBeTruthy();
  });
});
