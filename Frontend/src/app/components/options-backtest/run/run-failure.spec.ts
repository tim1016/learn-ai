import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { RunFailure } from '../options-backtest.types';
import { reads } from '../testing/page-actions';
import { runItem } from '../testing/run-fixtures';
import { RunFailureComponent } from './run-failure.component';

async function renderFailure(failure: RunFailure) {
  return render(RunFailureComponent, { inputs: { failure } });
}

describe('why a run failed, whatever code the server gives', () => {
  it('shows a run code the page has never seen through the label helper, with the server’s reason', async () => {
    await renderFailure({ code: 'A_FAILURE_ADDED_NEXT_YEAR', reason: 'Something the page has no word for happened.', items: [] });

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('Something the page has no word for happened.')).toBeTruthy();
    expect(within(alert).getByText('A Failure Added Next Year')).toBeTruthy();
  });

  it('shows a save that timed out as the server words it, with no words of the page’s own about it', async () => {
    await renderFailure({ code: 'SAVE_TIMED_OUT', reason: 'The result may or may not have been saved. Look in History.', items: [] });

    const alert = screen.getByRole('alert');
    expect(reads(alert)).toBe('The result may or may not have been saved. Look in History. Save Timed Out');
  });

  it('shows an item whose code the page has never seen by its session and the server’s message', async () => {
    await renderFailure({
      code: 'DOWNLOAD_FAILED',
      reason: 'The market data for some sessions could not be fetched.',
      items: [{ session_open_ms: runItem().first_session_open_ms, code: 'a_cause_found_next_year', message: 'The vendor did something new.' }],
    });

    expect(reads(screen.getByRole('listitem'))).toBe('2026-09-14 – The vendor did something new.');
  });
});
