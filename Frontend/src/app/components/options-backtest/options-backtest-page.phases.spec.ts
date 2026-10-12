import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { openExample, pickDates } from './testing/page-actions';
import { renderPage } from './testing/render-page';
import { scriptedRun } from './testing/run-script';

/**
 * The events a run's job sends for its phases, as the server sends them: the phase's name and, beside it, the plain label
 * the server gives that phase (`friendly`). The page names the phase in the server's words; the words it would make from
 * the phase's name ("Checking Data", "Waiting For Engine") are only for a job that sent none.
 */
const PHASES = [
  { phase: 'checking_data', friendly: 'Checking which sessions can be tested', made: 'Checking Data' },
  { phase: 'downloading_data', friendly: 'Fetching the market data the run is missing', made: 'Downloading Data' },
  { phase: 'waiting_for_engine', friendly: 'Waiting for the backtest already running', made: 'Waiting For Engine' },
  { phase: 'replaying', friendly: 'Replaying each session', made: 'Replaying' },
  { phase: 'saving', friendly: 'Saving the run', made: 'Saving' },
] as const;

describe('the phases of a run on the page', () => {
  it('names each phase in the words the server sends with it, one after the other, and never in words made from its name', async () => {
    const setup = scriptedRun(vi);
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    const start = screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
    await waitFor(() => expect(start.disabled).toBe(false));
    fireEvent.click(start);
    const panel = await screen.findByRole('region', { name: 'Backtest run' });

    for (const { phase, friendly, made } of PHASES) {
      setup.jobs.emit('job-1', { type: 'job.phase', phase, friendly });

      expect(await within(panel).findByText(friendly)).toBeTruthy();
      expect(within(panel).queryByText(made)).toBeNull();
    }
  });
});
