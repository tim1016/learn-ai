import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { SetupInvalidError } from './options-backtest.service';
import type { SetupRequest, SetupSummary } from './options-backtest.types';
import { formatClockText } from './session-time';
import { SESSION_CLOCK } from './testing/fixtures';
import { addLeg, inLeg, typeInto } from './testing/page-actions';
import { fakeService, renderPage, sentencesFor } from './testing/render-page';

const { open_minute_et: OPEN, close_minute_et: CLOSE } = SESSION_CLOCK;

describe('the plain-words summary', () => {
  it('shows the sentences the server wrote, as they came, notices first', async () => {
    const service = fakeService(vi, {
      describe: vi.fn(async (body: unknown) => ({
        ...sentencesFor(body as SetupRequest),
        window: 'THE WINDOW SENTENCE',
        costs: 'THE COSTS SENTENCE',
        notices: ['THE NOTICE'],
      })),
    });
    await renderPage(service);

    addLeg();

    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(within(summary).getByText('THE WINDOW SENTENCE')).toBeTruthy());
    const text = summary.textContent ?? '';
    expect(text.indexOf('THE NOTICE')).toBeGreaterThanOrEqual(0);
    expect(text.indexOf('THE NOTICE')).toBeLessThan(text.indexOf('THE WINDOW SENTENCE'));
    expect(text.indexOf('THE WINDOW SENTENCE')).toBeLessThan(text.indexOf('Sentence for leg call'));
    expect(text.indexOf('Sentence for leg call')).toBeLessThan(text.indexOf('THE COSTS SENTENCE'));
  });

  it('says the setup is not complete while it has no legs, and the legs card says to add one without an alarm', async () => {
    await renderPage(fakeService(vi));

    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(summary.textContent).toMatch(/not complete/i));
    const legs = screen.getByRole('region', { name: 'Legs' });
    expect(within(legs).getByRole('status')).toBeTruthy();
    expect(within(legs).queryByRole('alert')).toBeNull();
  });

  it('refreshes when the setup changes', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    await screen.findByText('Sentence for leg call.');

    typeInto('Slippage (percent)', '0.5');

    await waitFor(() => expect(service.describe.mock.lastCall?.[0]).toMatchObject({ slippage_percent: '0.5' }));
  });

  it('never lets a slow answer to an earlier setup overwrite the sentences of a newer one', async () => {
    const answers: { body: SetupRequest; resolve: (summary: SetupSummary) => void }[] = [];
    const service = fakeService(vi, {
      describe: vi.fn(
        (body: unknown) =>
          new Promise<SetupSummary>((resolve) => {
            answers.push({ body: body as SetupRequest, resolve });
          }),
      ),
    });
    await renderPage(service);
    await waitFor(() => expect(answers).toHaveLength(1));

    typeInto('Slippage (percent)', '0.4');
    await waitFor(() => expect(answers).toHaveLength(2));
    typeInto('Slippage (percent)', '0.5');
    await waitFor(() => expect(answers).toHaveLength(3));
    answers[2]?.resolve({ ...sentencesFor(answers[2].body), window: 'NEWEST' });
    answers[1]?.resolve({ ...sentencesFor(answers[1].body), window: 'MIDDLE' });
    answers[0]?.resolve({ ...sentencesFor(answers[0].body), window: 'OLDEST' });

    await screen.findByText('NEWEST');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('MIDDLE')).toBeNull();
    expect(screen.queryByText('OLDEST')).toBeNull();
  });

  it('offers to try again when the setup could not be read back', async () => {
    let fail = true;
    const service = fakeService(vi, {
      describe: vi.fn(async (body: unknown) => {
        if (fail) throw new Error('offline');
        return sentencesFor(body as SetupRequest);
      }),
    });
    await renderPage(service);
    addLeg();
    const again = await screen.findByRole('button', { name: 'Try again' });

    fail = false;
    fireEvent.click(again);

    await screen.findByText('Sentence for leg call.');
  });
});

describe('a problem lands at its field', () => {
  const rejecting = (...details: { loc: (string | number)[]; msg: string }[]) =>
    fakeService(vi, {
      describe: vi.fn(async () => {
        throw new SetupInvalidError(details);
      }),
    });

  it('on the leg the 422 counts, by position', async () => {
    await renderPage(rejecting({ loc: ['body', 'legs', 1, 'contracts'], msg: 'Input should be less than or equal to 1000' }));
    addLeg('Call');
    addLeg('Put');

    const second = await screen.findByRole('article', { name: 'Leg 2' });
    await waitFor(() => expect(within(second).getByRole('alert').textContent).toContain('less than or equal to 1000'));
    expect(within(screen.getByRole('article', { name: 'Leg 1' })).queryByRole('alert')).toBeNull();
  });

  it('on the legs as a whole, once there are legs it can be about', async () => {
    await renderPage(rejecting({ loc: ['body', 'legs'], msg: 'List should have at most 8 items' }));
    addLeg();

    const legs = screen.getByRole('region', { name: 'Legs' });
    await waitFor(() => expect(within(legs).getByRole('alert').textContent).toContain('at most 8 items'));
  });

  it('on the stop loss value of a leg', async () => {
    await renderPage(rejecting({ loc: ['body', 'legs', 0, 'stop_loss', 'percent', 'value'], msg: 'a stop loss in percent must be under 100' }));
    addLeg();

    await waitFor(() => expect(inLeg(1).getByRole('alert').textContent).toContain('under 100'));
  });

  it('on the costs, the entry time, the instrument and the square-off alike', async () => {
    await renderPage(
      rejecting(
        { loc: ['body', 'slippage_percent'], msg: 'SLIPPAGE PROBLEM' },
        { loc: ['body', 'entry', 'offset_ms'], msg: 'ENTRY PROBLEM' },
        { loc: ['body', 'underlying'], msg: 'UNDERLYING PROBLEM' },
        { loc: ['body', 'starting_cash_usd'], msg: 'CASH PROBLEM' },
      ),
    );
    addLeg();

    const costs = await screen.findByRole('region', { name: 'Costs' });
    await waitFor(() => expect(within(costs).getByText('SLIPPAGE PROBLEM')).toBeTruthy());
    expect(within(costs).getByText('CASH PROBLEM')).toBeTruthy();
    expect(within(screen.getByRole('region', { name: 'Entry settings' })).getByText('ENTRY PROBLEM')).toBeTruthy();
    expect(within(screen.getByRole('region', { name: 'Instrument' })).getByText('UNDERLYING PROBLEM')).toBeTruthy();
    expect(screen.getByLabelText('Slippage (percent)').getAttribute('aria-invalid')).toBe('true');
  });

  it('and the summary says the setup is not complete, keeping a reason that has no field', async () => {
    await renderPage(rejecting({ loc: ['body'], msg: 'Value error, every leg needs its own id; repeated: call' }));
    addLeg();

    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(summary.textContent).toMatch(/not complete/i));
    expect(within(summary).getByText('every leg needs its own id; repeated: call')).toBeTruthy();
  });
});

describe('typed times', () => {
  it('say what is stored beside each time', async () => {
    await renderPage(fakeService(vi));

    typeInto('Entry time', formatClockText(OPEN + 5));
    typeInto('Exit time', formatClockText(CLOSE - 46));

    expect(screen.getByText('5 minutes after the open')).toBeTruthy();
    expect(screen.getByText('46 minutes before the close')).toBeTruthy();
  });

  it('outside the full session are an error at the field, and nothing is sent for them', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    await waitFor(() => expect(service.describe).toHaveBeenCalled());
    const calls = service.describe.mock.calls.length;

    typeInto('Exit time', formatClockText(CLOSE + 1));
    typeInto('Entry time', formatClockText(OPEN - 1));

    const entry = screen.getByLabelText('Entry time');
    const exit = screen.getByLabelText('Exit time');
    expect(entry.getAttribute('aria-invalid')).toBe('true');
    expect(exit.getAttribute('aria-invalid')).toBe('true');
    expect(within(screen.getByRole('region', { name: 'Entry settings' })).getAllByRole('alert')).toHaveLength(2);
    expect(service.describe.mock.calls.length).toBe(calls);
    expect(screen.getByRole('region', { name: 'In plain words' }).textContent).toMatch(/not complete/i);

    typeInto('Exit time', formatClockText(CLOSE));
    typeInto('Entry time', formatClockText(OPEN));

    expect(entry.getAttribute('aria-invalid')).toBe('false');
    await waitFor(() => expect(service.describe.mock.lastCall?.[0]).toMatchObject({ entry: { anchor: 'open', offset_ms: 0 }, exit: { anchor: 'close', offset_ms: 0 } }));
  });

  it('follow the noon rule for "No re-entry after": the open up to noon, the close after it', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    fireEvent.click(screen.getByRole('switch', { name: 'No re-entry after' }));

    typeInto('No re-entry after time', '12:00');
    await waitFor(() => expect(service.describe.mock.lastCall?.[0]).toMatchObject({ no_reentry_after: { anchor: 'open', offset_ms: (12 * 60 - OPEN) * 60_000 } }));
    typeInto('No re-entry after time', '12:01');

    await waitFor(() => expect(service.describe.mock.lastCall?.[0]).toMatchObject({ no_reentry_after: { anchor: 'close', offset_ms: (CLOSE - 12 * 60 - 1) * 60_000 } }));
  });

  it('are read from the clock the server serves, whatever that clock is', async () => {
    const service = fakeService(vi, { sessionClock: vi.fn(async () => ({ open_minute_et: 600, close_minute_et: 900 })) });
    await renderPage(service);
    addLeg();

    expect((screen.getByLabelText('Entry time') as HTMLInputElement).value).toBe('10:05');
    expect((screen.getByLabelText('Exit time') as HTMLInputElement).value).toBe('14:45');
    typeInto('Entry time', '10:00');
    typeInto('Exit time', '15:01');

    expect(screen.getByLabelText('Exit time').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByLabelText('Entry time').getAttribute('aria-invalid')).toBe('false');
  });
});
