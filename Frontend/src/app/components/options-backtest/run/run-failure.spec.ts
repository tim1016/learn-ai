import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { RunFailure } from '../options-backtest.types';
import { reads } from '../testing/page-actions';
import { runItem } from '../testing/run-fixtures';
import { sessionOpenMs } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { RunFailureComponent } from './run-failure.component';

async function renderFailure(failure: RunFailure) {
  return render(RunFailureComponent, { inputs: { failure } });
}

const item = (code: string, message: string, session: number | null = runItem().first_session_open_ms) => ({ session_open_ms: session, code, message });
const groups = () => within(screen.getByRole('list', { name: 'What failed' }));

describe('why a run failed, whatever code the server gives', () => {
  it('shows a run code the page has never seen through the label helper, with the server’s reason', async () => {
    await renderFailure({ code: 'A_FAILURE_ADDED_NEXT_YEAR', reason: 'Something the page has no word for happened.', items: [] });

    expect(reads(screen.getByRole('alert'))).toBe('Something the page has no word for happened.');
    expect(screen.getByText('A Failure Added Next Year')).toBeTruthy();
  });

  it('shows a save that timed out as the server words it, with no words of the page’s own about it', async () => {
    await renderFailure({ code: 'SAVE_TIMED_OUT', reason: 'The result may or may not have been saved. Look in History.', items: [] });

    expect(reads(screen.getByRole('alert'))).toBe('The result may or may not have been saved. Look in History.');
    expect(screen.getByText('Save Timed Out')).toBeTruthy();
    expect(screen.queryByRole('list', { name: 'What failed' })).toBeNull();
  });

  it('shows an item whose code the page has never seen under that code through the label helper, with its session and the server’s message', async () => {
    await renderFailure({
      code: 'DOWNLOAD_FAILED',
      reason: 'The market data for some sessions could not be fetched.',
      items: [item('a_cause_found_next_year', 'The vendor did something new.')],
    });

    expect(reads(groups().getAllByRole('listitem')[0] ?? null)).toBe('A Cause Found Next Year · Items: 1 2026-09-14 – The vendor did something new.');
  });
});

describe('what failed, in the list under the reason', () => {
  it('announces the reason alone, never the items', async () => {
    await renderFailure({ code: 'DOWNLOAD_FAILED', reason: 'Some data could not be fetched.', items: [item('listing', 'No listing.'), item('listing', 'No listing either.')] });

    const alert = screen.getByRole('alert');

    expect(reads(alert)).toBe('Some data could not be fetched.');
    expect(alert.contains(screen.getByRole('list', { name: 'What failed' }))).toBe(false);
    expect(document.querySelectorAll('[role="alert"], [aria-live]')).toHaveLength(1);
  });

  it('groups the items by their code, with a count, in the order each code is first met, and names a session by its date and the run as a whole by words', async () => {
    await renderFailure({
      code: 'DOWNLOAD_FAILED',
      reason: 'Some data could not be fetched.',
      items: [
        item('listing_not_captured', 'No listing for the 14th.', sessionOpenMs('2026-09-14')),
        item('took_too_long', 'It took longer than the run waits.', sessionOpenMs('2026-09-15')),
        item('listing_not_captured', 'No listing for the 16th.', sessionOpenMs('2026-09-16')),
        item('internal_error', 'The run as a whole could not go on.', null),
      ],
    });

    const [listing, tooLong, internal] = groups().getAllByRole('button');
    expect([listing, tooLong, internal].map((group) => reads(group ?? null))).toEqual(['Listing Not Captured · Items: 2', 'Took Too Long · Items: 1', 'Internal Error · Items: 1']);
    expect(reads(groups().getAllByRole('list')[0] ?? null)).toBe('2026-09-14 – No listing for the 14th. 2026-09-16 – No listing for the 16th.');
    expect(reads(groups().getAllByRole('list')[2] ?? null)).toBe('The run as a whole – The run as a whole could not go on.');
  });

  it('keeps a long run of items shut until the owner opens it: 1,300 sessions are 1,300 items of one group, and none is drawn', async () => {
    const items = Array.from({ length: 1_300 }, (_, day) => item('session_cannot_be_tested', 'No data for this session.', sessionOpenMs('2026-09-14') + day * 86_400_000));
    await renderFailure({ code: 'SESSIONS_CANNOT_BE_TESTED', reason: 'Some sessions cannot be tested.', items });

    const group = screen.getByRole('button', { name: /Session Cannot Be Tested/ });

    expect(reads(group)).toBe('Session Cannot Be Tested · Items: 1300');
    expect(group.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryAllByRole('listitem')).toHaveLength(1);
    expect(reads(screen.getByRole('alert'))).toBe('Some sessions cannot be tested.');
  });

  it('opens a group on request, and shuts it again, and the button says which it is', async () => {
    const items = Array.from({ length: 8 }, (_, day) => item('took_too_long', `Took too long on day ${day}.`, sessionOpenMs('2026-09-14') + day * 86_400_000));
    await renderFailure({ code: 'DOWNLOAD_FAILED', reason: 'Some data could not be fetched.', items });
    const group = screen.getByRole<HTMLButtonElement>('button', { name: /Took Too Long/ });
    expect(group.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(group);

    expect(group.getAttribute('aria-expanded')).toBe('true');
    const opened = document.getElementById(group.getAttribute('aria-controls') ?? '');
    expect(opened?.querySelectorAll('li')).toHaveLength(8);
    fireEvent.click(group);
    expect(group.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Took too long on day 0.')).toBeNull();
  });

  it('shows a small group open already, and lets it be shut', async () => {
    await renderFailure({ code: 'DOWNLOAD_FAILED', reason: 'Some data could not be fetched.', items: [item('took_too_long', 'Took too long.')] });
    const group = screen.getByRole<HTMLButtonElement>('button', { name: /Took Too Long/ });

    expect(group.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(/Took too long\./)).toBeTruthy();
    fireEvent.click(group);

    expect(screen.queryByText(/Took too long\./)).toBeNull();
  });
});
