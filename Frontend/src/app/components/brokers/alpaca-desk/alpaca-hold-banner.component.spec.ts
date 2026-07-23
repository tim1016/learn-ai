import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ClerkStatus } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaHoldBannerComponent } from './alpaca-hold-banner.component';

function heldStatus(overrides: Partial<ClerkStatus> = {}): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: 'PA9',
    hold: {
      active: true,
      reason_code: 'UNEXPLAINED_ORDER_HOLD',
      reason: 'An order this account did not submit was observed at Alpaca.',
      since_ms: 1_700_000_000_000,
    },
    latest_reconciliation: { verdict: 'unexplained_order', recorded_at_ms: 1_700_000_000_000 },
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function clearStatus(): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: 'PA9',
    hold: { active: false, reason_code: null, reason: null, since_ms: null },
    latest_reconciliation: { verdict: 'clean', recorded_at_ms: 1_700_000_000_000 },
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
  };
}

async function renderBanner(service: Partial<BrokersService>) {
  return render(AlpacaHoldBannerComponent, {
    providers: [{ provide: BrokersService, useValue: service }],
  });
}

describe('AlpacaHoldBannerComponent', () => {
  it('renders the hold reason_code through receiptLabel and the backend prose when held', async () => {
    await renderBanner({ getClerkStatus: () => Promise.resolve(heldStatus()) });

    // reason_code rendered code-like via receiptLabel (UNEXPLAINED_ORDER_HOLD →
    // "Unexplained Order Hold").
    expect(await screen.findByText(/Unexplained Order Hold/)).toBeTruthy();
    // Backend-authored reason prose is rendered unpiped, verbatim.
    expect(
      screen.getByText(/An order this account did not submit was observed at Alpaca\./),
    ).toBeTruthy();
  });

  it('renders no banner when there is no active hold', async () => {
    await renderBanner({ getClerkStatus: () => Promise.resolve(clearStatus()) });

    // Give the resource a tick to resolve, then confirm nothing hold-related shows.
    await waitFor(() => {
      expect(screen.queryByText(/Submission paused/)).toBeNull();
    });
    expect(screen.queryByRole('button', { name: /Clear hold/ })).toBeNull();
  });

  it('refreshes a desk already open when the sweep raises a hold', async () => {
    // Fake only the polling interval. Testing Library's async queries use
    // timeouts internally and must keep their real clock.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      const getClerkStatus = vi
        .fn()
        .mockResolvedValueOnce(clearStatus())
        .mockResolvedValueOnce(heldStatus());
      await renderBanner({ getClerkStatus });

      await waitFor(() => expect(getClerkStatus).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(15_000);

      expect(await screen.findByText(/Unexplained Order Hold/)).toBeTruthy();
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });

  it('invokes BrokersService.clearHold when the clear-hold button is clicked', async () => {
    const clearHold = vi.fn().mockResolvedValue(clearStatus());
    await renderBanner({
      getClerkStatus: () => Promise.resolve(heldStatus()),
      clearHold,
    });

    const button = await screen.findByRole('button', { name: /Clear hold/ });
    fireEvent.click(button);

    await waitFor(() => {
      expect(clearHold).toHaveBeenCalledWith(
        'alpaca',
        expect.objectContaining({ reason: expect.any(String) }),
      );
    });
  });
});
