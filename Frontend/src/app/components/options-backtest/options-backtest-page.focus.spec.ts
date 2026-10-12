import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { RunRefusedError } from './options-backtest.service';
import { checkButton, openExample, pickDates, press } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

/** Where the keyboard is, once the control it was on has done its work and gone, or is far from the answer. */
async function readyToCheck(service = fakeService(vi)) {
  await renderPage(service);
  await openExample();
  pickDates('2026-09-14', '2026-09-18');
  await waitFor(() => expect(checkButton().disabled).toBe(false));
  return service;
}

describe('Check data and the keyboard', () => {
  it('goes to the heading of the answer when it comes in, because the bar that asked is far from it', async () => {
    await readyToCheck();
    checkButton().focus();

    fireEvent.click(checkButton());

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Check data' })));
  });

  it('goes to the heading of the answer when the check could not be made, which is also shown there', async () => {
    const service = fakeService(vi, {
      checkData: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE', 'The data catalog cannot be asked right now.', 503);
      }),
    });
    await readyToCheck(service);

    fireEvent.click(checkButton());

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Check data' })));
  });

  it('does not go anywhere when the range is refused and no panel opens', async () => {
    const service = fakeService(vi, {
      checkData: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RANGE_TOO_LONG', 'A run takes at most 1300 sessions.', 400);
      }),
    });
    await readyToCheck(service);
    checkButton().focus();

    fireEvent.click(checkButton());
    await screen.findByText('A run takes at most 1300 sessions.');

    expect(screen.queryByRole('heading', { name: 'Check data' })).toBeNull();
    expect(document.activeElement).not.toBe(screen.queryByRole('heading', { name: 'Duration' }));
  });

  it('goes to the heading of the Duration card, the dates it was about, when the answer is dismissed with the keyboard on Dismiss', async () => {
    await readyToCheck();
    fireEvent.click(checkButton());
    const dismiss = await screen.findByRole('button', { name: 'Dismiss' });
    dismiss.focus();

    press('Dismiss');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Duration' })));
    expect(screen.queryByRole('region', { name: 'Check data' })).toBeNull();
  });
});
