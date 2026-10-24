import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { EXAMPLE_NO_REENTRY, EXAMPLE_ONE_REENTRY } from '../../../components/options-backtest/testing/fixtures';
import { checkButton, dateFields, pickDates } from '../../../components/options-backtest/testing/page-actions';
import { renderExplore, setupUrl } from '../../testing/render-workspace';

describe('the Duration card', () => {
  it('takes its dates from the shared picker with no plain date input anywhere, and offers quick ranges that reach no further back than the options plan', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);

    const card = screen.getByRole('region', { name: 'Duration' });
    const ranges = within(screen.getByRole('group', { name: 'Quick ranges' }));

    expect.soft(within(card).getByRole('group', { name: 'Backtest dates' }), 'takes its dates from the shared picker').toBeTruthy();
    expect.soft(within(card).getByLabelText('Start date'), 'has a Start date field').toBeTruthy();
    expect.soft(within(card).getByLabelText('End date'), 'has an End date field').toBeTruthy();
    expect.soft([...document.querySelectorAll('input')].filter((field) => field.type === 'date'), 'has no plain date input anywhere').toEqual([]);
    expect.soft(ranges.getByRole('button', { name: /^1M/ }), 'offers the 1M quick range').toBeTruthy();
    expect.soft(ranges.queryByRole('button', { name: /^5Y/ }), 'offers no 5Y quick range, further back than the options plan').toBeNull();
    expect.soft(ranges.queryByRole('button', { name: /^Max/ }), 'offers no Max quick range, further back than the options plan').toBeNull();
  });
});

describe('the dates are not part of the setup', () => {
  it('reach no request that reads the setup back or checks its data, and a backtest sends them beside the setup, never in it', async () => {
    const service = fakeService(vi);
    await renderExplore(service, EXAMPLE_ONE_REENTRY);

    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByRole('region', { name: 'Check data' });

    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
    const [setup] = service.checkData.mock.calls[0] ?? [];
    const asked = [...service.describe.mock.calls.map((call) => JSON.stringify(call)), JSON.stringify(setup)].join('\n');
    for (const text of [asked]) {
      expect(text).not.toContain('2026-09');
      expect(text).not.toContain('1789358400000');
      expect(text).not.toContain('start_ms');
      expect(text).not.toContain('end_ms');
    }
  });

  it('are not touched by going to another setup', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);
    pickDates('2026-09-14', '2026-09-18');

    await TestBed.inject(Router).navigateByUrl(setupUrl(EXAMPLE_NO_REENTRY, 'explore'));
    await screen.findByRole('heading', { name: 'Duration' });

    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
  });
});

describe('the line beside the dates, and the lower bound', () => {
  it('says that Check data tells from when option prices are available, until it has run', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);

    expect(screen.getByText('From when option prices are available is shown once Check data has run.')).toBeTruthy();
  });

  it('then says the day, in New York dates, as the server gave it', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));

    fireEvent.click(checkButton());

    const line = await screen.findByText(/Option prices are available from/);
    expect(line.textContent?.replace(/\s+/g, ' ').trim()).toBe('Option prices are available from 2025-10-15');
    expect(line.querySelector('app-timestamp-display')?.getAttribute('data-timestamp-mode')).toBe('date-et');
  });

  it('refuses a start date before that day, with the reason in words, and the run waits for a date', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByText(/Option prices are available from/);

    pickDates('2025-10-14', '2026-09-18');

    expect(await screen.findByText('Option prices are available from 2025-10-15, so no earlier day can be picked.')).toBeTruthy();
    await waitFor(() => expect(checkButton().disabled).toBe(true));
    expect(screen.getByText('Check data and Start are available once both dates are picked.')).toBeTruthy();
  });

  it('takes the day itself', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByText(/Option prices are available from/);

    pickDates('2025-10-15', '2026-09-18');

    await waitFor(() => expect(dateFields()).toEqual({ from: '2025-10-15', to: '2026-09-18' }));
    expect(checkButton().disabled).toBe(false);
  });
});

describe('the upper bound', () => {
  // The standard date world's clock is Monday 2026-09-21 at noon: that day's session is still open, so the last completed
  // session is Friday 2026-09-18. A run is over completed sessions, and today's session is never testable.
  it('refuses an end date after the last completed session, with the reason in words, before Check data has run', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);

    pickDates('2026-09-14', '2026-09-21');

    expect(await screen.findByText('The last completed session is 2026-09-18.')).toBeTruthy();
    await waitFor(() => expect(checkButton().disabled).toBe(true));
    expect(screen.getByText('Check data and Start are available once both dates are picked.')).toBeTruthy();
  });

  it('still refuses it once Check data has answered and set the lower bound', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByText(/Option prices are available from/);

    pickDates('2026-09-14', '2026-09-23');

    expect(await screen.findByText('The last completed session is 2026-09-18.')).toBeTruthy();
    await waitFor(() => expect(checkButton().disabled).toBe(true));
  });
});
