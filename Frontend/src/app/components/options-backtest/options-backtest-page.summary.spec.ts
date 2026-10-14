import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { SetupInvalidError } from './options-backtest.service';
import type { SetupRequest, SetupSummary } from './options-backtest.types';
import { formatClockText } from './session-time';
import { exampleBody, minutes, servedSaved, SESSION_CLOCK } from './testing/fixtures';
import { addLeg, inLeg, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage, sentencesFor } from './testing/render-page';

const { open_minute_et: OPEN, close_minute_et: CLOSE } = SESSION_CLOCK;

describe('the plain-words summary', () => {
  it('shows the sentences the server wrote, as they came, notices first', async () => {
    const service = fakeService(vi, {
      describe: vi.fn(async (body: unknown) => ({
        ...sentencesFor(body as SetupRequest),
        window: ['THE WINDOW SENTENCE'],
        costs: ['THE COSTS SENTENCE'],
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

  it('puts every sentence on a line of its own, as served and in order, and each leg’s sentences in a box of its own', async () => {
    const window = ['Trades SPY.', 'Each fill is moved 0.3% against you, then rounded.', 'A stop later than 15:00 earns no re-entry.'];
    const fills = ['An order fills at the next minute.', 'An order to open is dropped.'];
    const costs = ['Cash is 10000.', 'Fees are not charged.'];
    const service = fakeService(vi, {
      describe: vi.fn(async (body: unknown) => {
        const read = sentencesFor(body as SetupRequest);
        return {
          ...read,
          window,
          fills,
          costs,
          legs: read.legs.map((leg) => ({ leg_id: leg.leg_id, sentences: [`First of ${leg.leg_id}.`, `Second of ${leg.leg_id}.`] })),
        };
      }),
    });
    await renderPage(service);

    addLeg('Call');
    addLeg('Put');

    const summary = await screen.findByRole('region', { name: 'In plain words' });
    for (const sentence of [...window, ...fills, ...costs]) expect((await within(summary).findByText(sentence)).tagName).toBe('P');
    const lines = [...summary.querySelectorAll('p.sentence')].map((line) => line.textContent);
    expect(lines).toEqual([...window, ...fills, 'First of call.', 'Second of call.', 'First of put.', 'Second of put.', 'Square off is partial.', ...costs]);
    const legs = within(summary).getAllByRole('listitem');
    expect(legs.map((leg) => [...leg.querySelectorAll('p')].map((line) => line.textContent))).toEqual([
      ['First of call.', 'Second of call.'],
      ['First of put.', 'Second of put.'],
    ]);
  });

  it('folds how orders fill under the window, closed, says how many rules it holds, and stays as it was left across a re-reading', async () => {
    await renderPage(fakeService(vi));
    addLeg();
    const summary = await screen.findByRole('region', { name: 'In plain words' });
    const fold = await waitFor(() => {
      const found = summary.querySelector('details');
      if (found === null) throw new Error('no fold yet');
      return found;
    });

    expect(fold.open).toBe(false);
    expect(within(fold).getByText('How orders fill').closest('summary')?.textContent).toMatch(/2 rules/);
    expect(summary.querySelector('.block')?.textContent).not.toMatch(/Orders fill a minute on/);

    fold.open = true;
    // The element says it has toggled in a task of its own, as a browser does.
    await new Promise((resolve) => setTimeout(resolve, 0));
    typeInto('Slippage (percent)', '0.5');
    await waitFor(() => expect(summary.querySelector('details')?.open).toBe(true));
    expect(within(summary).getByText('Orders fill a minute on.')).toBeTruthy();
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
    answers[2]?.resolve({ ...sentencesFor(answers[2].body), window: ['NEWEST'] });
    answers[1]?.resolve({ ...sentencesFor(answers[1].body), window: ['MIDDLE'] });
    answers[0]?.resolve({ ...sentencesFor(answers[0].body), window: ['OLDEST'] });

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

describe('what a screen reader is told about the summary', () => {
  const summaryRegion = () => screen.findByRole('region', { name: 'In plain words' });

  it('is its status, and never the summary text', async () => {
    await renderPage(fakeService(vi));
    addLeg();

    const summary = await summaryRegion();
    const sentence = await within(summary).findByText('Sentence for leg call.');
    const status = within(summary).getByRole('status');

    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.textContent).toMatch(/read this setup back/i);
    expect(status.textContent).not.toContain('Sentence for leg call');
    expect(sentence.closest('[aria-live], [role="status"], [role="alert"]')).toBeNull();
    expect(summary.querySelectorAll('[aria-live]')).toHaveLength(1);
  });

  it('says while the setup is being read back', async () => {
    await renderPage(fakeService(vi, { describe: vi.fn(() => new Promise<SetupSummary>(() => undefined)) }));
    addLeg();

    const status = within(await summaryRegion()).getByRole('status');

    await waitFor(() => expect(status.textContent).toMatch(/reading the setup back/i));
  });

  it('says when the setup is not complete', async () => {
    await renderPage(fakeService(vi));

    const status = within(await summaryRegion()).getByRole('status');

    await waitFor(() => expect(status.textContent).toMatch(/not complete/i));
  });

  it('says when the read failed, and keeps the way to try again outside it', async () => {
    await renderPage(fakeService(vi, { describe: vi.fn(async () => Promise.reject(new Error('offline'))) }));
    addLeg();

    const summary = await summaryRegion();
    const retry = await within(summary).findByRole('button', { name: 'Try again' });
    const status = within(summary).getByRole('status');

    expect(status.textContent?.trim()).not.toBe('');
    expect(status.contains(retry)).toBe(false);
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

  it('on the instrument, for an underlying of six letters, which the server now refuses', async () => {
    const service = fakeService(vi, {
      list: vi.fn(async () => [servedSaved('Six letters', { ...exampleBody(), underlying: 'ABCDEF' })]),
      describe: vi.fn(async (body: unknown) => {
        const setup = body as SetupRequest;
        if (setup.underlying.length > 5) throw new SetupInvalidError([{ loc: ['body', 'underlying'], msg: "String should match pattern '^[A-Z]{1,5}$'" }]);
        return sentencesFor(setup);
      }),
    });
    await renderPage(service);

    fireEvent.click(await within(screen.getByRole('complementary', { name: 'Saved setups' })).findByRole('button', { name: /^Six letters/ }));

    const instrument = screen.getByRole('region', { name: 'Instrument' });
    const reason = await within(instrument).findByText("String should match pattern '^[A-Z]{1,5}$'");
    expect(reason.getAttribute('role')).toBe('alert');
    expect(service.describe.mock.calls.map(([body]) => (body as SetupRequest).underlying)).toContain('ABCDEF');
  });

  it('on the square-off setting, in the Legs panel, tied to the choice', async () => {
    await renderPage(rejecting({ loc: ['body', 'square_off'], msg: "Input should be 'partial' or 'complete'" }));
    addLeg();

    const card = within(await screen.findByRole('region', { name: 'Legs' }));
    const reason = await card.findByText("Input should be 'partial' or 'complete'");
    expect(reason.getAttribute('role')).toBe('alert');
    expect(card.getByRole('group', { name: 'Square off' }).getAttribute('aria-describedby')).toBe(reason.id);
  });

  it('and the summary says the setup is not complete, and says plainly that a reason with no field is about the whole setup', async () => {
    await renderPage(rejecting({ loc: ['body'], msg: 'Value error, every leg needs its own id; repeated: call' }));
    addLeg();

    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(summary.textContent).toMatch(/not complete/i));
    const reasons = within(summary).getByRole('list', { name: /about the setup as a whole/i });
    expect(within(reasons).getByText('every leg needs its own id; repeated: call')).toBeTruthy();
    expect(summary.textContent).not.toMatch(/fields marked above/i);
  });

  it('keeps pointing at the fields when the server also marked some', async () => {
    await renderPage(rejecting({ loc: ['body'], msg: 'Value error, a rule about the whole' }, { loc: ['body', 'slippage_percent'], msg: 'SLIPPAGE PROBLEM' }));
    addLeg();

    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(summary.textContent).toMatch(/fields marked above/i));
    expect(within(summary).getByRole('list', { name: /about the setup as a whole/i })).toBeTruthy();
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

    typeInto('Exit time', formatClockText(CLOSE - 1));
    typeInto('Entry time', formatClockText(OPEN + 1));

    expect(entry.getAttribute('aria-invalid')).toBe('false');
    await waitFor(() => expect(service.describe.mock.lastCall?.[0]).toMatchObject({ entry: { anchor: 'open', offset_ms: 60_000 }, exit: { anchor: 'close', offset_ms: 60_000 } }));
  });

  it('an entry at the open and an exit at the close are refused at their own fields with the rule, not left to the server', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    await waitFor(() => expect(service.describe).toHaveBeenCalled());
    const calls = service.describe.mock.calls.length;

    typeInto('Entry time', formatClockText(OPEN));
    typeInto('Exit time', formatClockText(CLOSE));

    const card = within(screen.getByRole('region', { name: 'Entry settings' }));
    expect(screen.getByLabelText('Entry time').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByLabelText('Exit time').getAttribute('aria-invalid')).toBe('true');
    expect(card.getByText(`The entry must be at least a minute after the open: ${formatClockText(OPEN + 1)} or later.`).getAttribute('role')).toBe('alert');
    expect(card.getByText(`The exit must be at least a minute before the close: ${formatClockText(CLOSE - 1)} or earlier.`).getAttribute('role')).toBe('alert');
    expect(service.describe.mock.calls.length).toBe(calls);
    const summary = screen.getByRole('region', { name: 'In plain words' });
    expect(summary.textContent).toMatch(/fields marked above/i);
    expect(within(summary).queryByRole('list', { name: /about the setup as a whole/i })).toBeNull();
  });

  it('are put in order at the exit when a loaded setup counts both from the open and the entry is moved to the exit', async () => {
    const bothFromTheOpen = servedSaved('Both from the open', { ...exampleBody(), entry: minutes(5, 'open'), exit: minutes(60, 'open') });
    const service = fakeService(vi, { list: vi.fn(async () => [bothFromTheOpen]) });
    await renderPage(service);
    fireEvent.click(await within(screen.getByRole('complementary', { name: 'Saved setups' })).findByRole('button', { name: /^Both from the open/ }));
    await screen.findByRole('article', { name: 'Leg 2' });
    expect((screen.getByLabelText('Exit time') as HTMLInputElement).value).toBe(formatClockText(OPEN + 60));
    await waitFor(() => expect(service.describe).toHaveBeenCalled());
    const calls = service.describe.mock.calls.length;

    typeInto('Entry time', formatClockText(OPEN + 60));

    const card = within(screen.getByRole('region', { name: 'Entry settings' }));
    expect(card.getByText(/The exit must be after the entry/).getAttribute('role')).toBe('alert');
    expect(screen.getByLabelText('Exit time').getAttribute('aria-invalid')).toBe('true');
    expect(service.describe.mock.calls.length).toBe(calls);

    typeInto('Exit time', formatClockText(OPEN + 61));

    expect(screen.getByLabelText('Exit time').getAttribute('aria-invalid')).toBe('false');
    await waitFor(() => expect(service.describe.mock.lastCall?.[0]).toMatchObject({ entry: minutes(60, 'open'), exit: minutes(CLOSE - OPEN - 61, 'close') }));
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
    typeInto('Entry time', '10:01');
    typeInto('Exit time', '15:01');

    expect(screen.getByLabelText('Exit time').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByLabelText('Entry time').getAttribute('aria-invalid')).toBe('false');
    typeInto('Entry time', '10:00');
    expect(within(screen.getByRole('region', { name: 'Entry settings' })).getByText(/10:01 or later/)).toBeTruthy();
  });
});

describe('the read-back beside the leg cards', () => {
  const blocks = (summary: HTMLElement) => [...summary.querySelectorAll<HTMLElement>('li[data-leg-id]')];
  const numbered = (summary: HTMLElement) => blocks(summary).map((block) => [block.querySelector('.number')?.textContent, block.dataset['legId']]);

  it('numbers each leg’s box as its card is numbered, and follows a leg that is deleted', async () => {
    await renderPage(fakeService(vi));
    addLeg('Call');
    addLeg('Put');
    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(blocks(summary)).toHaveLength(2));

    expect(numbered(summary)).toEqual([['1', 'call'], ['2', 'put']]);

    press('Delete Leg 1');

    await waitFor(() => expect(blocks(summary)).toHaveLength(1));
    expect(numbered(summary)).toEqual([['1', 'put']]);
  });

  it('shows where the server says a leg when the keyboard goes to its card, and moves only the rail', async () => {
    await renderPage(fakeService(vi));
    addLeg('Call');
    addLeg('Put');
    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(blocks(summary)).toHaveLength(2));
    // jsdom has no layout: say that the read-back is 200px high, holds 1000px, and has the second leg below its edge.
    const pane = summary.closest<HTMLElement>('.rail__read');
    if (pane === null) throw new Error('the read-back has no scrolling pane');
    Object.defineProperty(pane, 'scrollHeight', { value: 1000, configurable: true });
    Object.defineProperty(pane, 'clientHeight', { value: 200, configurable: true });
    pane.getBoundingClientRect = () => ({ top: 0, bottom: 200 }) as DOMRect;
    const [, second] = blocks(summary);
    if (second === undefined) throw new Error('no block for the second leg');
    second.getBoundingClientRect = () => ({ top: 500, bottom: 560 }) as DOMRect;
    const pageScroll = vi.spyOn(window, 'scrollTo');

    fireEvent.focusIn(screen.getByRole('switch', { name: 'Leg 2 stop loss' }));

    expect(pane.scrollTop).toBe(360);
    expect(pageScroll).not.toHaveBeenCalled();
  });

  it('shows a leg that took the keyboard while the read-back was being refreshed once the refreshed sentences are there', async () => {
    let hold = false;
    const gate: { release: (() => void) | null } = { release: null };
    const service = fakeService(vi, {
      describe: vi.fn((body: unknown) =>
        hold
          ? new Promise<SetupSummary>((resolve) => {
              gate.release = () => resolve(sentencesFor(body as SetupRequest));
            })
          : Promise.resolve(sentencesFor(body as SetupRequest)),
      ),
    });
    await renderPage(service);
    addLeg('Call');
    addLeg('Put');
    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(blocks(summary)).toHaveLength(2));
    const pane = summary.closest<HTMLElement>('.rail__read');
    if (pane === null) throw new Error('the read-back has no scrolling pane');
    Object.defineProperty(pane, 'scrollHeight', { value: 1000, configurable: true });
    Object.defineProperty(pane, 'clientHeight', { value: 200, configurable: true });
    // jsdom has no layout: the pane is 200px high and the second leg's box lies below its edge.
    const geometry = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      if (this === pane) return { top: 0, bottom: 200 } as DOMRect;
      if (this instanceof HTMLElement && this.dataset['legId'] === 'put') return { top: 500, bottom: 560 } as DOMRect;
      return { top: 0, bottom: 0 } as DOMRect;
    });
    try {
      hold = true;
      typeInto('Slippage (percent)', '0.5');
      await waitFor(() => expect(blocks(summary)).toHaveLength(0));

      fireEvent.focusIn(screen.getByRole('switch', { name: 'Leg 2 stop loss' }));
      expect(pane.scrollTop).toBe(0);
      gate.release?.();

      await waitFor(() => expect(pane.scrollTop).toBe(360));
    } finally {
      geometry.mockRestore();
    }
  });
});
