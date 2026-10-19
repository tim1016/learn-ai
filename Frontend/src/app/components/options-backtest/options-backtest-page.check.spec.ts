import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { etDayEndMs, etMidnightMs } from '../../shared/date/et-midnight';
import { sessionOpenMs } from '../../shared/date-picker/testing/fake-trading-calendar';
import { RunRefusedError } from './options-backtest.service';
import type { DataCheck, DataProblem, SessionCheck } from './options-backtest.types';
import { dataCheck, sessionCheck } from './testing/fixtures';
import { checkButton, dateFields, expectTradingDatesInNewYork, lettingGo, openExample, paragraph, pickDates, press, reads, typeInto } from './testing/page-actions';
import { fakeService, renderPage, type FakeService } from './testing/render-page';
import { scriptedRun } from './testing/run-script';

const panel = () => screen.getByRole('region', { name: 'Check data' });

async function readyToCheck(service: FakeService = fakeService(vi), dates: [string, string] = ['2026-09-14', '2026-09-18']): Promise<FakeService> {
  await renderPage(service);
  await openExample();
  pickDates(...dates);
  await waitFor(() => expect(checkButton().disabled).toBe(false));
  return service;
}

async function runCheck(): Promise<HTMLElement> {
  fireEvent.click(checkButton());
  return screen.findByRole('region', { name: 'Check data' });
}

const cannot = (day: string, ...problems: DataProblem[]): SessionCheck => sessionCheck(day, { verdict: 'cannot_be_tested', problems });
const problem = (code: DataProblem['code'], message: string, lake_reason: string | null = null): DataProblem => ({ code, message, lake_reason });

describe('when Check data can be asked', () => {
  it('needs the setup complete and read back, and both dates, and says which is missing', async () => {
    await renderPage(fakeService(vi));

    expect(checkButton().disabled).toBe(true);
    expect(screen.getByText('Check data and Start are available once the setup is complete.')).toBeTruthy();
    await openExample();
    await waitFor(() => expect(screen.getByText('Check data and Start are available once both dates are picked.')).toBeTruthy());
    expect(checkButton().disabled).toBe(true);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    expect(screen.queryByText(/Check data and Start are available once/)).toBeNull();
  });
});

describe('what is posted', () => {
  it('is the setup as the server read it back, with the range as the two instants', async () => {
    const service = await readyToCheck();

    await runCheck();

    await waitFor(() => expect(service.checkData).toHaveBeenCalledTimes(1));
    const [setup, startMs, endMs] = service.checkData.mock.calls[0] ?? [];
    const answer = await service.describe.mock.results.at(-1)?.value;
    expect(setup).toEqual(answer?.setup);
    expect(startMs).toBe(etMidnightMs('2026-09-14'));
    expect(endMs).toBe(etDayEndMs('2026-09-18'));
    // 2026-09-14 00:00 EDT, and the midnight after Friday the 18th, both written out from the clock.
    expect([startMs, endMs]).toEqual([1_789_358_400_000, 1_789_790_400_000 + 0]);
  });

  it('is the range across the clock change in March, each end on its own side of it', async () => {
    const service = await readyToCheck(fakeService(vi), ['2026-03-06', '2026-03-09']);

    await runCheck();

    await waitFor(() => expect(service.checkData).toHaveBeenCalledTimes(1));
    expect(service.checkData.mock.calls[0]?.slice(1)).toEqual([1_772_773_200_000, 1_773_115_200_000]);
  });
});

describe('the panel', () => {
  it('counts the sessions that are ready, need a download and cannot be tested', async () => {
    const answer = dataCheck([
      sessionCheck('2026-09-14'),
      sessionCheck('2026-09-15'),
      sessionCheck('2026-09-16', { verdict: 'needs_download', missing: ['listing'] }),
      cannot('2026-09-17', problem('older_than_the_data_plan', 'Older.')),
    ]);
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => answer) }));

    const check = await runCheck();

    const counts = within(check);
    expect(counts.getByText('Ready').nextElementSibling?.textContent).toBe('2');
    expect(counts.getByText('Needs Download', { selector: 'dt' }).nextElementSibling?.textContent).toBe('1');
    expect(counts.getByText('Cannot Be Tested', { selector: 'dt' }).nextElementSibling?.textContent).toBe('1');
  });

  it('says what range it checked, in New York dates, and that nothing was downloaded', async () => {
    await readyToCheck();

    const check = await runCheck();

    await within(check).findByText('Ready');
    expect(paragraph(/^Checked 2026-09-14 – 2026-09-18$/, check)).toBeTruthy();
    expect(within(check).getByText('The answer is read from what is already stored. Nothing was downloaded.')).toBeTruthy();
  });

  it('groups the reasons by code with how many sessions each, the server’s message, and the sessions by date', async () => {
    const old = problem('older_than_the_data_plan', 'This session is older than the data plan reaches.');
    const fees = problem('no_published_fee_rates', 'No fee rates are published for this date.');
    const answer = dataCheck([
      cannot('2026-09-14', old),
      cannot('2026-09-15', old),
      cannot('2026-09-16', fees),
      sessionCheck('2026-09-17'),
    ]);
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => answer) }));

    const check = await runCheck();

    const section = within(check).getByRole('region', { name: 'Cannot Be Tested' });
    const olderHead = within(section).getByText('Older Than The Data Plan');
    const olderGroup = olderHead.closest('.reason') as HTMLElement;
    expect(olderGroup.querySelector('.reason__head')?.textContent?.replace(/\s+/g, ' ')).toBe('Older Than The Data Plan · Sessions: 2');
    expect(within(olderGroup).getByText('This session is older than the data plan reaches.')).toBeTruthy();
    expect([...olderGroup.querySelectorAll('.days li')].map((day) => day.textContent?.trim())).toEqual(['2026-09-14', '2026-09-15']);
    const feesGroup = within(section).getByText('No Published Fee Rates').closest('.reason') as HTMLElement;
    expect(feesGroup.querySelector('.reason__head')?.textContent?.replace(/\s+/g, ' ')).toBe('No Published Fee Rates · Sessions: 1');
    expect(within(feesGroup).getByText('No fee rates are published for this date.')).toBeTruthy();
    expect([...feesGroup.querySelectorAll('.days li')].map((day) => day.textContent?.trim())).toEqual(['2026-09-16']);
    expectTradingDatesInNewYork(section);
  });

  it('shows the lake’s own reason when it recorded a failure, and a session’s notes', async () => {
    const answer = dataCheck([
      sessionCheck('2026-09-14', {
        verdict: 'cannot_be_tested',
        problems: [problem('lake_holds_a_failure', 'The lake gave up on this session.', 'rate_limited')],
        notes: ['The last try failed.'],
      }),
    ]);
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => answer) }));

    const check = await runCheck();

    expect(within(check).getByText('Lake Holds A Failure')).toBeTruthy();
    expect(within(check).getByText('The lake gave up on this session.')).toBeTruthy();
    expect(within(check).getByText('The lake recorded: Rate Limited')).toBeTruthy();
    expect(within(check).getByText(/The last try failed\./)).toBeTruthy();
  });

  it('lists what each session that needs a download is missing, in the server’s codes through the label helper', async () => {
    const answer = dataCheck([
      sessionCheck('2026-09-14', { verdict: 'needs_download', missing: ['underlying_bars', 'listing', 'contracts'], contracts: { state: 'not_yet_known', needed: null, missing: null } }),
      sessionCheck('2026-09-15', { verdict: 'needs_download', missing: ['contracts'], contracts: { state: 'known', needed: 9, missing: 4 } }),
    ]);
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => answer) }));

    const check = await runCheck();

    const rows = [...within(check).getByRole('region', { name: 'Needs Download' }).querySelectorAll('li')].map((row) => row.textContent?.replace(/\s+/g, ' ').trim());
    expect(rows).toEqual(['2026-09-14 – Underlying Bars, Listing, Contracts (not yet known)', '2026-09-15 – Contracts (4 of 9 missing)']);
    expectTradingDatesInNewYork(check);
  });

  it('lists the sessions that are ready but will be skipped, by date, with the reason', async () => {
    const answer = dataCheck([
      sessionCheck('2026-09-14'),
      sessionCheck('2026-09-15', { skip: 'window_does_not_fit' }),
      sessionCheck('2026-09-16', { skip: 'underlying_did_not_trade' }),
    ]);
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => answer) }));

    const check = await runCheck();

    const rows = [...within(check).getByRole('region', { name: 'Ready, but skipped' }).querySelectorAll('li')].map((row) => row.textContent?.replace(/\s+/g, ' ').trim());
    expect(rows).toEqual(['2026-09-15 – Window Does Not Fit', '2026-09-16 – Underlying Did Not Trade']);
    expectTradingDatesInNewYork(check);
  });

  it('has no section for what does not occur', async () => {
    await readyToCheck();

    const check = await runCheck();

    await within(check).findByText('Ready');
    expect(within(check).queryByRole('region', { name: 'Cannot Be Tested' })).toBeNull();
    expect(within(check).queryByRole('region', { name: 'Needs Download' })).toBeNull();
    expect(within(check).queryByRole('region', { name: 'Ready, but skipped' })).toBeNull();
    expect(within(check).queryByRole('button', { name: 'Run available dates' })).toBeNull();
  });
});

describe('Run available dates', () => {
  const stretch = {
    start_ms: etMidnightMs('2026-09-16'),
    end_ms: etMidnightMs('2026-09-19'),
    first_session_open_ms: sessionOpenMs('2026-09-16'),
    last_session_open_ms: sessionOpenMs('2026-09-18'),
    sessions: 3,
  };
  const partlyTestable = (): DataCheck =>
    dataCheck(
      [
        cannot('2026-09-14', problem('older_than_the_data_plan', 'Older.')),
        cannot('2026-09-15', problem('older_than_the_data_plan', 'Older.')),
        sessionCheck('2026-09-16'),
        sessionCheck('2026-09-17'),
        sessionCheck('2026-09-18'),
      ],
      { longest_testable_stretch: stretch },
    );

  it('is offered when not every session can be tested, and changes nothing until it is pressed', async () => {
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => partlyTestable()) }));

    const check = await runCheck();

    const button = await within(check).findByRole('button', { name: 'Run available dates' });
    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
    expect(screen.queryByText(/Range changed to/)).toBeNull();
    expect(within(check).getByText(/Longest stretch that can be: .* Sessions in it: 3$/)).toBeTruthy();
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it('sets the range to the longest testable stretch when pressed, and says so', async () => {
    const service = await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => partlyTestable()) }));
    const check = await runCheck();

    fireEvent.click(await within(check).findByRole('button', { name: 'Run available dates' }));

    await waitFor(() => expect(dateFields()).toEqual({ from: '2026-09-16', to: '2026-09-18' }));
    const note = screen.getByText(/Range changed to/);
    expect(reads(note)).toBe('Range changed to 2026-09-16 – 2026-09-18');
    expect(note.getAttribute('role')).toBe('status');
    expect(service.checkData).toHaveBeenCalledTimes(1);
  });

  it('stops saying the range was changed once the owner edits the dates', async () => {
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => partlyTestable()) }));
    const check = await runCheck();
    fireEvent.click(await within(check).findByRole('button', { name: 'Run available dates' }));
    await screen.findByText(/Range changed to/);

    pickDates('2026-09-15', '2026-09-18');

    await waitFor(() => expect(screen.queryByText(/Range changed to/)).toBeNull());
  });

  it('is not offered when every session can be tested', async () => {
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => dataCheck([sessionCheck('2026-09-14'), sessionCheck('2026-09-15', { verdict: 'needs_download' })], { longest_testable_stretch: stretch })) }));

    const check = await runCheck();

    await within(check).findByText('Needs Download', { selector: 'dt' });
    expect(within(check).queryByRole('button', { name: 'Run available dates' })).toBeNull();
  });

  it('says so, and offers no range, when no session can be tested', async () => {
    const none = dataCheck([cannot('2026-09-14', problem('older_than_the_data_plan', 'Older.'))]);
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => none) }));

    const check = await runCheck();

    expect(await within(check).findByText('No session in this range can be tested.')).toBeTruthy();
    expect(within(check).queryByRole('button', { name: 'Run available dates' })).toBeNull();
  });
});

describe('an answer that no longer describes the setup or the dates', () => {
  it('stays open, says the setup or dates changed, and cannot be used to set the range', async () => {
    const answer = dataCheck([cannot('2026-09-14', problem('older_than_the_data_plan', 'Older.')), sessionCheck('2026-09-15')], {
      longest_testable_stretch: { start_ms: etMidnightMs('2026-09-15'), end_ms: etMidnightMs('2026-09-19'), first_session_open_ms: sessionOpenMs('2026-09-15'), last_session_open_ms: sessionOpenMs('2026-09-18'), sessions: 4 },
    });
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => answer) }));
    const check = await runCheck();
    const run = await within(check).findByRole('button', { name: 'Run available dates' });
    expect(screen.queryByText(/have changed since this check/)).toBeNull();

    pickDates('2026-09-14', '2026-09-17');

    expect(await screen.findByText(/have changed since this check/)).toBeTruthy();
    expect((run as HTMLButtonElement).disabled).toBe(true);
    expect(panel()).toBeTruthy();
  });

  it('is current again when the setup is put back as it was', async () => {
    await readyToCheck();
    await runCheck();
    typeInto('Slippage (percent)', '0.4');
    await screen.findByText(/have changed since this check/);

    typeInto('Slippage (percent)', '0.3');

    await waitFor(() => expect(screen.queryByText(/have changed since this check/)).toBeNull());
  });
});

describe('an answer, once the setup the server read back is a different one', () => {
  it('still says so after the server has read the changed setup back, not only while it is reading it', async () => {
    await readyToCheck();
    await runCheck();

    typeInto('Slippage (percent)', '0.4');
    await screen.findByText(/have changed since this check/);
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    for (let settled = 0; settled < 5; settled += 1) await lettingGo();

    expect(screen.queryByText(/have changed since this check/)).not.toBeNull();
  });

  it('says so once a backtest has started, because its download changes what is stored', async () => {
    const setup = scriptedRun(vi);
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    await runCheck();
    expect(screen.queryByText(/have changed since this check/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Start backtest' }));

    expect(await screen.findByText(/a backtest has started/)).toBeTruthy();
    expect(screen.getByText(/have changed since this check/)).toBeTruthy();
    expect(panel()).toBeTruthy();
  });

  it('is current again when it is asked afresh after the backtest has started', async () => {
    const setup = scriptedRun(vi);
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    await runCheck();
    fireEvent.click(screen.getByRole('button', { name: 'Start backtest' }));
    await screen.findByText(/a backtest has started/);

    fireEvent.click(checkButton());

    await waitFor(() => expect(screen.queryByText(/have changed since this check/)).toBeNull());
    expect(setup.service.checkData).toHaveBeenCalledTimes(2);
  });

  it('is not made stale by a backtest the server refused to start', async () => {
    const setup = scriptedRun(vi, {
      startRun: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RANGE_HAS_NO_SESSION', 'No trading session falls in that range.', 400);
      }),
    });
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    await runCheck();

    fireEvent.click(screen.getByRole('button', { name: 'Start backtest' }));
    await screen.findByText('No trading session falls in that range.');
    await lettingGo();

    expect(screen.queryByText(/have changed since this check/)).toBeNull();
  });
});

describe('closing the panel', () => {
  it('stays open until it is dismissed, through edits and a second look at the page', async () => {
    await readyToCheck();
    await runCheck();
    typeInto('Slippage (percent)', '0.4');
    await screen.findByText(/have changed since this check/);

    expect(panel()).toBeTruthy();
    press('Dismiss');

    expect(screen.queryByRole('region', { name: 'Check data' })).toBeNull();
  });

  it('leaves the dates as they are when it is dismissed, however much of the range it said could not be tested', async () => {
    const stretch = {
      start_ms: etMidnightMs('2026-09-16'),
      end_ms: etMidnightMs('2026-09-19'),
      first_session_open_ms: sessionOpenMs('2026-09-16'),
      last_session_open_ms: sessionOpenMs('2026-09-18'),
      sessions: 3,
    };
    const answer = dataCheck([cannot('2026-09-14', problem('older_than_the_data_plan', 'Older.')), sessionCheck('2026-09-16')], { longest_testable_stretch: stretch });
    await readyToCheck(fakeService(vi, { checkData: vi.fn(async () => answer) }));
    await runCheck();
    await screen.findByRole('button', { name: 'Run available dates' });

    press('Dismiss');

    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
    expect(screen.queryByText(/Range changed to/)).toBeNull();
  });

  it('keeps from when option prices are available after it is dismissed', async () => {
    await readyToCheck();
    await runCheck();
    await screen.findByText(/Option prices are available from/);

    press('Dismiss');

    expect(reads(screen.getByText(/Option prices are available from/))).toBe('Option prices are available from 2025-10-15');
  });
});

describe('when Check data does not give an answer', () => {
  it('shows a refused range at the dates with the server’s message, and opens no panel', async () => {
    const service = fakeService(vi, {
      checkData: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RANGE_TOO_LONG', 'A run takes at most 1300 sessions; that range holds more.', 400);
      }),
    });
    await readyToCheck(service);

    fireEvent.click(checkButton());

    const duration = screen.getByRole('region', { name: 'Duration' });
    const reason = await within(duration).findByText('A run takes at most 1300 sessions; that range holds more.');
    expect(reason.getAttribute('role')).toBe('alert');
    expect(screen.queryByRole('region', { name: 'Check data' })).toBeNull();
    pickDates('2026-09-15', '2026-09-18');
    await waitFor(() => expect(screen.queryByText(/A run takes at most 1300 sessions/)).toBeNull());
  });

  it('says the server’s reason in the panel when the catalog cannot be asked, and asks again on request', async () => {
    let down = true;
    const service = fakeService(vi, {
      checkData: vi.fn(async () => {
        if (down) throw new RunRefusedError('OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE', 'The data catalog cannot be asked right now.', 503);
        return dataCheck([sessionCheck(`2026-09-14`)]);
      }),
    });
    await readyToCheck(service);

    const check = await runCheck();

    expect((await within(check).findByText('The data catalog cannot be asked right now.')).getAttribute('role')).toBe('alert');
    down = false;
    press('Try again');
    await within(panel()).findByText('Ready');
  });

  it('cannot be asked a second time while the first is on its way, and shows nothing if it is dismissed before it answers', async () => {
    const waiting: { resolve: (answer: DataCheck) => void }[] = [];
    const service = fakeService(vi, {
      checkData: vi.fn(() => new Promise<DataCheck>((resolve) => waiting.push({ resolve }))),
    });
    await readyToCheck(service);

    fireEvent.click(checkButton());
    await waitFor(() => expect(waiting).toHaveLength(1));
    expect(checkButton().disabled).toBe(true);
    fireEvent.click(checkButton());
    press('Dismiss');
    waiting[0]?.resolve(dataCheck([sessionCheck('2026-09-14')]));
    await lettingGo();

    expect(service.checkData).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('region', { name: 'Check data' })).toBeNull();
  });
});
