import { screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import { WINDOW_QUERY, openPlace } from '../testing/instrument-place';
import { STUDY } from '../testing/returns-study';
import { ReturnsDistributionService } from './returns-distribution.service';

vi.mock('chart.js', () => ({
  Chart: class {
    static register(..._args: unknown[]): void {}
    destroy(): void {}
  },
  registerables: [],
}));

function returnsService() {
  return { distribution: vi.fn(() => of(STUDY)), minuteCandles: vi.fn(() => of([])) };
}

/** The person's Run: the study fetches missing sessions into the lake, so opening the tab runs nothing. */
async function run(): Promise<void> {
  await userEvent.click(await screen.findByRole('button', { name: 'Run the study' }));
}

describe('an instrument\'s Returns tab', () => {
  it('opens without running the study, which fetches missing sessions into the lake', async () => {
    const service = returnsService();
    await openPlace(`/markets/SPY/returns?${WINDOW_QUERY}`, [{ provide: ReturnsDistributionService, useValue: service }]);

    expect(await screen.findByRole('button', { name: 'Run the study' })).toBeTruthy();
    expect(screen.getByText(/Opening this tab fetches nothing/)).toBeTruthy();
    expect(screen.queryByText('Days studied')).toBeNull();
    expect(service.distribution).not.toHaveBeenCalled();
  });

  it('runs the study once over the committed window as Python takes it: int64 ms UTC, with no client-side date math', async () => {
    const service = returnsService();
    await openPlace(`/markets/SPY/returns?${WINDOW_QUERY}`, [{ provide: ReturnsDistributionService, useValue: service }]);
    await run();

    expect(await screen.findByText('Days studied')).toBeTruthy();
    expect(service.distribution).toHaveBeenCalledTimes(1);
    expect(service.distribution).toHaveBeenCalledWith({
      symbol: 'SPY',
      fromMsUtc: Date.UTC(2026, 3, 15),
      toMsUtc: Date.UTC(2026, 4, 15) + 86_400_000 - 1,
      binWidthPct: 0.5,
      spanPct: 5,
    });
  });

  it('says it cannot run on an intraday timeframe before running anything, and runs once the person moves to the daily one', async () => {
    const service = returnsService();
    const opened = await openPlace(`/markets/SPY/returns?${WINDOW_QUERY}&timeframe=5m`, [
      { provide: ReturnsDistributionService, useValue: service },
    ]);

    expect(await screen.findByText('Returns does not run on 5m bars, so nothing was run.')).toBeTruthy();
    expect(screen.queryByText('Days studied')).toBeNull();
    expect(service.distribution).not.toHaveBeenCalled();
    expect(opened.router.url, 'the choice stays as it was until it is changed').toContain('timeframe=5m');

    await userEvent.click(screen.getByRole('button', { name: 'Use the 1 day timeframe' }));
    // Moving to the daily timeframe makes the study possible; it is still the person's Run that starts it.
    expect(await screen.findByRole('button', { name: 'Run the study' })).toBeTruthy();
    expect(service.distribution).not.toHaveBeenCalled();
    await run();

    expect(await screen.findByText('Days studied')).toBeTruthy();
    expect(opened.router.url).toContain('timeframe=1d');
    expect(service.distribution).toHaveBeenCalledTimes(1);
  });

  it('runs on the Extended hours session and says the setting changes nothing here', async () => {
    const service = returnsService();
    await openPlace(`/markets/SPY/returns?${WINDOW_QUERY}&session=extended`, [
      { provide: ReturnsDistributionService, useValue: service },
    ]);

    expect(await screen.findByText(/the Extended hours setting changes nothing here/)).toBeTruthy();
    await run();
    await waitFor(() => expect(service.distribution).toHaveBeenCalledTimes(1));
  });

  it('shows no note about the session when it is the regular one', async () => {
    await openPlace(`/markets/SPY/returns?${WINDOW_QUERY}`, [{ provide: ReturnsDistributionService, useValue: returnsService() }]);
    await run();

    await screen.findByText('Days studied');
    expect(screen.queryByText(/changes nothing here/)).toBeNull();
  });
});
