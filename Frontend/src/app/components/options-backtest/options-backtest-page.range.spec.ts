import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { etIsoDate, etMidnightMs, isoDateAfter } from '../../shared/date/et-midnight';
import { RunRefusedError } from './options-backtest.service';
import { checkButton, dateFields, openExample, pickDates } from './testing/page-actions';
import { fakeService, renderPage, type FakeService } from './testing/render-page';
import { scriptedRun } from './testing/run-script';

const startButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
const isNewYorkMidnight = (ms: number) => etMidnightMs(etIsoDate(ms)) === ms;

async function readyWith(service: FakeService, dates: [string, string]): Promise<void> {
  await renderPage(service);
  await openExample();
  pickDates(...dates);
  await waitFor(() => expect(startButton().disabled).toBe(false));
}

/** The server refuses a range whose two instants are not New York midnights; the page always sends midnights, so it should never meet this. */
const NOT_ON_MIDNIGHT = { code: 'OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT', message: 'A run starts and ends at New York midnight; 1789358400001 is not one.' };

describe('the range the page sends', () => {
  // The picker takes trading days only, so a last date is never a Sunday (that range is the pure spec of `rangeInstants`, in
  // `duration.spec.ts`); the days beside a clock change are the Friday before it and the Monday after it.
  it.each([
    ['the Friday before the clocks go forward', ['2026-03-02', '2026-03-06'] as [string, string]],
    ['the Monday after the clocks go forward', ['2026-03-09', '2026-03-13'] as [string, string]],
    ['the Friday before the clocks go back', ['2025-10-27', '2025-10-31'] as [string, string]],
    ['the Monday after the clocks go back', ['2025-11-03', '2025-11-07'] as [string, string]],
  ])('is two New York midnights, for Check data and for Start, for a range with %s in it', async (_name, dates) => {
    const setup = scriptedRun(vi);
    await readyWith(setup.service, dates);

    fireEvent.click(checkButton());
    await waitFor(() => expect(setup.service.checkData).toHaveBeenCalledTimes(1));
    fireEvent.click(startButton());
    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));

    const checked = setup.service.checkData.mock.calls[0];
    const started = setup.service.startRun.mock.calls[0]?.[0];
    for (const [startMs, endMs] of [[checked?.[1], checked?.[2]], [started?.start_ms, started?.end_ms]]) {
      expect(isNewYorkMidnight(startMs ?? Number.NaN)).toBe(true);
      expect(isNewYorkMidnight(endMs ?? Number.NaN)).toBe(true);
      expect(etIsoDate(startMs ?? Number.NaN)).toBe(dates[0]);
      expect(etIsoDate(endMs ?? Number.NaN)).toBe(isoDateAfter(dates[1]));
    }
  });

  it('waits for a trading day when the last date is a Sunday, so no range is ever sent that ends on one', async () => {
    const setup = scriptedRun(vi);
    await renderPage(setup.service);
    await openExample();

    pickDates('2026-03-06', '2026-03-08');

    await waitFor(() => expect(screen.getByText('Check data and Start are available once both dates are picked.')).toBeTruthy());
    expect(startButton().disabled).toBe(true);
    expect(setup.service.startRun).not.toHaveBeenCalled();
  });
});

describe('a refusal of the range with a code the page has not met', () => {
  it('is shown at the dates in the server’s words for Check data, opens no panel, and is gone when the dates change', async () => {
    const service = fakeService(vi, {
      checkData: vi.fn(async () => {
        throw new RunRefusedError(NOT_ON_MIDNIGHT.code, NOT_ON_MIDNIGHT.message, 400);
      }),
    });
    await renderPage(service);
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));

    fireEvent.click(checkButton());

    const reason = await within(screen.getByRole('region', { name: 'Duration' })).findByText(NOT_ON_MIDNIGHT.message);
    expect(reason.getAttribute('role')).toBe('alert');
    expect(screen.queryByRole('region', { name: 'Check data' })).toBeNull();
    pickDates('2026-09-15', '2026-09-18');
    await waitFor(() => expect(screen.queryByText(NOT_ON_MIDNIGHT.message)).toBeNull());
    expect(dateFields()).toEqual({ from: '2026-09-15', to: '2026-09-18' });
  });

  it('is shown at the dates in the server’s words for Start, starts no run, and leaves Start free', async () => {
    const setup = scriptedRun(vi, {
      startRun: vi.fn(async () => {
        throw new RunRefusedError(NOT_ON_MIDNIGHT.code, NOT_ON_MIDNIGHT.message, 400);
      }),
    });
    await readyWith(setup.service, ['2026-09-14', '2026-09-18']);

    fireEvent.click(startButton());

    const reason = await within(screen.getByRole('region', { name: 'Duration' })).findByText(NOT_ON_MIDNIGHT.message);
    expect(reason.getAttribute('role')).toBe('alert');
    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
    expect(startButton().disabled).toBe(false);
  });
});
