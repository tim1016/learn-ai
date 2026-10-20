import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { etIsoDate, etMidnightMs, isoDateAfter } from '../../shared/date/et-midnight';
import { checkButton, openExample, pickDates } from './testing/page-actions';
import { renderPage, type FakeService } from './testing/render-page';
import { scriptedRun } from './testing/run-script';

const startButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
const isNewYorkMidnight = (ms: number) => etMidnightMs(etIsoDate(ms)) === ms;

async function readyWith(service: FakeService, dates: [string, string]): Promise<void> {
  await renderPage(service);
  await openExample();
  pickDates(...dates);
  await waitFor(() => expect(startButton().disabled).toBe(false));
}

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

