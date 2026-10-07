import { fireEvent, render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest';

import { resourceTarget } from '../../../../fleet/resource-target';
import { provideFakeDateWorld } from '../../../../shared/date-picker/testing/fake-trading-calendar';
import {
  BrokerV2PanelService,
  type OptionStructurePreview,
  type OptionStructurePreviewRequest,
} from '../../v2-panel/lib/broker-v2-panel.service';
import { DEPLOY_TYPING_SETTLE_MS } from '../deploy-money-step.component';
import { startingOrderTerms, type OptionLegRules } from './option-structure';
import { OptionStructureBuilderComponent, type OptionStructureChoice } from './option-structure-builder.component';
import { PREVIEW_ASK_AGAIN_LIMIT } from './option-structure-preview';
import {
  BULL_CALL_SPREAD_LEGS,
  OPTION_CATALOGUE,
  RED_VERDICT,
  previewWithoutFigures,
  resolvedPreview,
} from './option-structure.fixtures';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });

function previewService(answer: OptionStructurePreview = resolvedPreview()) {
  return { previewOptionStructure: vi.fn<(target: unknown, body: OptionStructurePreviewRequest) => Promise<OptionStructurePreview>>().mockResolvedValue(answer) };
}

async function renderBuilder(
  { legs = BULL_CALL_SPREAD_LEGS, service = previewService() }: { legs?: readonly OptionLegRules[]; service?: ReturnType<typeof previewService> } = {},
) {
  const use = vi.fn<(choice: OptionStructureChoice) => void>();
  const cancelled = vi.fn();
  const rendered = await render(OptionStructureBuilderComponent, {
    inputs: {
      catalogue: OPTION_CATALOGUE, target: TARGET, previewKey: 'preview-key-1', legs, underlying: 'SPY', listensTo: 'SPY',
      structures: 1, terms: startingOrderTerms(OPTION_CATALOGUE.order_terms),
    },
    on: { use, cancelled },
    providers: [
      ...provideFakeDateWorld(),
      { provide: DEPLOY_TYPING_SETTLE_MS, useValue: 0 },
      { provide: BrokerV2PanelService, useValue: service },
    ],
  });
  return { ...rendered, service, use, cancelled };
}

/** The last structure the preview was asked about. */
function lastAsk(service: ReturnType<typeof previewService>): OptionStructurePreviewRequest {
  const call = service.previewOptionStructure.mock.calls.at(-1);
  if (call === undefined) throw new Error('The preview was never asked.');
  return call[1];
}

function box(name: string): HTMLInputElement {
  return screen.getByLabelText<HTMLInputElement>(name);
}

/** Each term of the description lists under `element`, with what it says. */
function termsIn(element: HTMLElement): Record<string, string> {
  return Object.fromEntries(Array.from(element.querySelectorAll('dl > div')).map((row) => [
    row.querySelector('dt')?.textContent?.trim() ?? '', row.querySelector('dd')?.textContent?.replace(/\s+/g, ' ').trim() ?? '',
  ]));
}

/** A state that passes by itself: the server says to ask about it again after `wait` ms, every time it is asked. */
function waitingService(wait = 1_000) {
  return previewService(previewWithoutFigures('quotes_arriving', 'Stand-in: quotes are arriving.', { ask_again_in_ms: wait }));
}

/** The panel's own note that the page no longer asks by itself. */
const STOPPED_ASKING = 'Stopped asking. Refresh asks again.';
/** Longer than a whole run of asks takes at the one second the server names. */
const PAST_A_WHOLE_RUN_MS = 5 * 60_000;

function chosenIn(name: string): string {
  const select = screen.getByLabelText<HTMLSelectElement>(name);
  return select.selectedOptions[0]?.textContent?.trim() ?? '';
}

afterEach(() => vi.useRealTimers());

describe('OptionStructureBuilderComponent', () => {
  it('offers the named structures by how many legs they have, and Custom', async () => {
    await renderBuilder({ legs: [] });

    const start = screen.getByRole('navigation', { name: 'Start from' });
    expect(within(start).getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual(['One leg', 'Two legs', 'Your own']);
    expect(within(start).getAllByRole('button').map((button) => button.textContent)).toEqual(['Long call', 'Bull call spread', 'Custom']);
    expect(within(start).getByRole('button', { name: 'Custom', pressed: true })).toBeTruthy();
    expect(screen.getByText('Stand-in: what Custom starts from.')).toBeTruthy();
  });

  it('fills the legs of a named structure when it is chosen', async () => {
    const { service } = await renderBuilder({ legs: [] });

    await userEvent.click(screen.getByRole('button', { name: 'Bull call spread' }));

    expect(screen.getByRole('heading', { level: 3, name: 'Bull call spread' })).toBeTruthy();
    expect(screen.getByText('Stand-in: what a bull call spread is.')).toBeTruthy();
    expect(chosenIn('Leg 1 side')).toBe('Buy');
    expect(chosenIn('Leg 1 type')).toBe('Call');
    expect(box('Leg 1 quantity').value).toBe('1');
    expect(chosenIn('Leg 1 which strike')).toBe('Delta target');
    expect(box('Leg 1 Delta target, delta').value).toBe('0.5');
    expect(box('Leg 1 within, delta').value).toBe('0.05');
    expect(chosenIn('Leg 1 expiry')).toBe('Nearest weekly');
    expect(chosenIn('Leg 2 side')).toBe('Sell');
    expect(chosenIn('Leg 2 which strike')).toBe('Distance from another leg');
    expect(box('Leg 2 Distance from another leg, dollars').value).toBe('5');
    expect(chosenIn('Leg 2 measured from')).toBe('from leg 1');
    expect(box('Leg 2 within, dollars').value).toBe('0');
    await vi.waitFor(() => expect(lastAsk(service)).toMatchObject({ structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS }));
  });

  it('gives a strike rule its own number box and its own “Within”, each in the rule’s unit', async () => {
    await renderBuilder();

    await userEvent.selectOptions(screen.getByLabelText('Leg 1 which strike'), 'Percent from price');
    expect(box('Leg 1 Percent from price, percent').value).toBe('');
    expect(box('Leg 1 within, percent').value).toBe('0.5');
    expect(screen.queryByLabelText('Leg 1 within, delta')).toBeNull();

    // A rule with no tolerance has no "Within" box at all.
    await userEvent.selectOptions(screen.getByLabelText('Leg 1 which strike'), 'Steps from the money');
    expect(box('Leg 1 Steps from the money, strikes').value).toBe('');
    expect(screen.queryByLabelText(/^Leg 1 within/)).toBeNull();
  });

  it('picks a fixed expiry date with the shared date field, and days with a number', async () => {
    const { service } = await renderBuilder({ legs: [BULL_CALL_SPREAD_LEGS[0]] });

    await userEvent.selectOptions(screen.getByLabelText('Leg 1 expiry'), 'At least N days out');
    fireEvent.change(box('Leg 1 expiry, days'), { target: { value: '7' } });
    await vi.waitFor(() => expect(lastAsk(service).legs[0].expiry).toEqual({ selector: 'min_dte', days: 7 }));

    await userEvent.selectOptions(screen.getByLabelText('Leg 1 expiry'), 'Fixed date');
    const date = box('Leg 1 expiry date');
    expect(date.placeholder).toBe('YYYY-MM-DD');
    fireEvent.change(date, { target: { value: '2026-10-09' } });

    await vi.waitFor(() => expect(lastAsk(service).legs[0].expiry).toEqual({
      selector: 'absolute', expiration_ms: Date.UTC(2026, 9, 9, 20, 0, 0),
    }));
  });

  it('offers no day before today in New York for a fixed expiry date', async () => {
    // 23:30 on October 6 in New York, and already October 7 by the UTC clock.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(Date.UTC(2026, 9, 7, 3, 30));
    await renderBuilder({ legs: [BULL_CALL_SPREAD_LEGS[0]] });
    await userEvent.selectOptions(screen.getByLabelText('Leg 1 expiry'), 'Fixed date');

    await userEvent.click(screen.getByRole('button', { name: 'Open the calendar' }));

    const calendar = await screen.findByRole('group', { name: 'Calendar' });
    // PrimeNG's months are zero-based.
    const offered = (day: number): boolean => {
      const cell = calendar.querySelector(`span[data-date="2026-9-${day}"]`);
      if (cell === null) throw new Error(`October ${day} is not on the calendar.`);
      return !cell.classList.contains('p-disabled');
    };
    expect([offered(5), offered(6), offered(7)]).toEqual([false, true, true]);
  });

  it('calls an edited structure Custom, and by its name again once its legs match again', async () => {
    const { service } = await renderBuilder();
    expect(screen.getByRole('button', { name: 'Bull call spread', pressed: true })).toBeTruthy();

    fireEvent.change(box('Leg 1 Delta target, delta'), { target: { value: '0.45' } });

    expect(screen.getByRole('button', { name: 'Custom', pressed: true })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Bull call spread', pressed: false })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 3, name: 'Custom' })).toBeTruthy();
    await vi.waitFor(() => expect(lastAsk(service).structure).toBeNull());

    fireEvent.change(box('Leg 1 Delta target, delta'), { target: { value: '0.50' } });

    expect(screen.getByRole('button', { name: 'Bull call spread', pressed: true })).toBeTruthy();
    await vi.waitFor(() => expect(lastAsk(service).structure).toBe('bull_call_spread'));
  });

  it('adds legs up to the catalogue’s limit and removes one', async () => {
    await renderBuilder();
    const add = screen.getByRole('button', { name: 'Add a leg' });

    await userEvent.click(add);
    await userEvent.click(add);

    expect(screen.getAllByRole('row')).toHaveLength(5);
    expect(add.hasAttribute('disabled')).toBe(true);
    expect(screen.getByText('Up to 4 legs.')).toBeTruthy();
    // A new leg expires as the one above it does; its strike number is the owner's to type.
    expect(chosenIn('Leg 3 expiry')).toBe('Nearest weekly');
    expect(box('Leg 3 Delta target, delta').value).toBe('');

    await userEvent.click(screen.getByRole('button', { name: 'Remove leg 4' }));

    expect(screen.getAllByRole('row')).toHaveLength(4);
    expect(add.hasAttribute('disabled')).toBe(false);
  });

  it('leaves a distance to be measured again once the leg it was measured from is removed', async () => {
    await renderBuilder();
    await userEvent.click(screen.getByRole('button', { name: 'Add a leg' }));

    await userEvent.click(screen.getByRole('button', { name: 'Remove leg 1' }));

    // What was leg 2 is leg 1 now, and points at no leg until the owner picks one of those left.
    const from = screen.getByLabelText<HTMLSelectElement>('Leg 1 measured from');
    expect(from.value).toBe('');
    expect(from.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('button', { name: 'Use this structure' }).hasAttribute('disabled')).toBe(true);

    await userEvent.selectOptions(from, 'from leg 2');
    expect(from.getAttribute('aria-invalid')).toBe('false');
  });

  it('says under an unfinished row what each box still needs, in the catalogue’s labels and units', async () => {
    await renderBuilder();
    const [, first, second] = screen.getAllByRole('row');
    /** What the row says under its boxes; empty for a finished row. */
    const said = (row: HTMLElement): string[] =>
      Array.from(row.querySelectorAll('.leg-row__problems > span')).map((line) => line.textContent?.trim() ?? '');
    const type = (name: string, value: string): void => { fireEvent.change(box(name), { target: { value } }); };
    expect(said(first)).toEqual([]);

    // A quantity of nothing, or of half a contract, is shown and refused in words.
    for (const quantity of ['0', '1.5', '']) {
      type('Leg 1 quantity', quantity);
      expect(said(first)).toEqual(['Quantity needs a whole number of 1 or more.']);
    }
    const quantity = box('Leg 1 quantity');
    expect(quantity.getAttribute('aria-invalid')).toBe('true');
    expect(document.getElementById(quantity.getAttribute('aria-describedby') ?? '')?.textContent).toContain('Quantity needs');
    type('Leg 1 quantity', '2');
    expect(said(first)).toEqual([]);
    expect(quantity.getAttribute('aria-invalid')).toBe('false');
    expect(quantity.hasAttribute('aria-describedby')).toBe(false);

    type('Leg 1 Delta target, delta', '');
    type('Leg 1 within, delta', '');
    expect(said(first)).toEqual(['Delta target needs a number, in delta.', 'Within needs a number, in delta.']);

    // A rule the server counts in whole steps takes a whole number.
    await userEvent.selectOptions(screen.getByLabelText('Leg 1 which strike'), 'Steps from the money');
    expect(said(first)).toEqual(['Steps from the money needs a whole number of strikes.']);
    type('Leg 1 Steps from the money, strikes', '1.5');
    expect(said(first)).toEqual(['Steps from the money needs a whole number of strikes.']);
    expect(box('Leg 1 Steps from the money, strikes').getAttribute('aria-invalid')).toBe('true');
    type('Leg 1 Steps from the money, strikes', '2');
    expect(said(first)).toEqual([]);

    await userEvent.selectOptions(screen.getByLabelText('Leg 1 expiry'), 'At least N days out');
    expect(said(first)).toEqual(['At least N days out needs a whole number of days.']);
    type('Leg 1 expiry, days', '2.5');
    expect(said(first)).toEqual(['At least N days out needs a whole number of days.']);
    type('Leg 1 expiry, days', '7');
    expect(said(first)).toEqual([]);

    // A fixed date with no date is said by the date field itself, once.
    await userEvent.selectOptions(screen.getByLabelText('Leg 1 expiry'), 'Fixed date');
    expect(within(first).getByRole('alert').textContent).toContain('Fixed date needs a date.');
    expect(said(first)).toEqual([]);
    expect(box('Leg 1 expiry date').getAttribute('aria-invalid')).toBe('true');
    type('Leg 1 expiry date', '2026-10-09');
    await vi.waitFor(() => expect(within(first).queryByRole('alert')).toBeNull());

    // The second leg measures from the first; once that is gone it says so.
    expect(said(second)).toEqual([]);
    await userEvent.click(screen.getByRole('button', { name: 'Remove leg 1' }));
    expect(said(screen.getAllByRole('row')[1])).toEqual(['Distance from another leg needs the leg it is measured from.']);
  });

  it('neither previews nor uses a structure while a row is unfinished', async () => {
    const { service, use } = await renderBuilder();
    await vi.waitFor(() => expect(service.previewOptionStructure).toHaveBeenCalledTimes(1));
    await screen.findByText('Stand-in: it can be deployed.');

    fireEvent.change(box('Leg 1 Delta target, delta'), { target: { value: '' } });

    const useIt = screen.getByRole('button', { name: 'Use this structure' });
    expect(useIt.hasAttribute('disabled')).toBe(true);
    expect(box('Leg 1 Delta target, delta').getAttribute('aria-invalid')).toBe('true');
    // The answer for the legs as they were no longer speaks for what is on screen.
    expect(screen.getByRole('status').textContent).toContain('Not known yet.');
    await userEvent.click(useIt);
    expect(use).not.toHaveBeenCalled();
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);
  });

  it('says the server’s verdict, and will not use a structure the server marks red', async () => {
    const { use } = await renderBuilder({ service: previewService(resolvedPreview({ verdict: RED_VERDICT })) });

    const verdict = await screen.findByText('Stand-in: leg 2 lands on the strike of leg 1.');
    expect(verdict.closest('app-option-verdict')?.classList.contains('option-verdict--red')).toBe(true);
    expect(screen.getByText('Stand-in: it can be deployed.')).toBeTruthy();
    const useIt = screen.getByRole('button', { name: 'Use this structure' });
    expect(useIt.hasAttribute('disabled')).toBe(true);
    // The leg the verdict points at is tied to the verdict's sentence.
    const pointedAt = screen.getAllByRole('row')[2];
    expect(pointedAt.getAttribute('aria-describedby')).toBe(verdict.closest('app-option-verdict')?.id);

    await userEvent.click(useIt);
    expect(use).not.toHaveBeenCalled();
  });

  it('hands over the legs and the name they still carry, or nothing on Cancel', async () => {
    const { use, cancelled } = await renderBuilder();
    await screen.findByText('Stand-in: it can be deployed.');

    expect(screen.getByText('Stand-in: it can be deployed.').closest('app-option-verdict')?.classList.contains('option-verdict--green')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Use this structure' }));
    expect(use).toHaveBeenCalledWith({ structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS });

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(cancelled).toHaveBeenCalledOnce();
  });

  it('shows each picked contract with its quote, both deltas and its age', async () => {
    await renderBuilder();

    const picks = screen.getByRole('region', { name: 'What this picks right now' });
    const [bought, sold] = await within(picks).findAllByRole('listitem');
    expect(within(bought).getByText('Buy 1')).toBeTruthy();
    expect(within(bought).getByRole('button', { name: "Show contract code for SPY Oct 9 '26 585 Call" })).toBeTruthy();
    expect(termsIn(bought)).toEqual({ 'Bid': '3.10', 'Ask': '3.16', 'Age': '1 s', 'Our delta': '0.48', 'IBKR delta': '0.47' });
    expect(within(sold).getByText('Sell 1')).toBeTruthy();
    expect(within(sold).getByRole('button', { name: "Show contract code for SPY Oct 9 '26 590 Call" })).toBeTruthy();
    expect(termsIn(sold)).toEqual({ 'Bid': '1.22', 'Ask': '1.27', 'Age': '2 s', 'Our delta': '0.31', 'IBKR delta': '0.31' });

    expect(within(picks).getByText(/oldest 2 s old/)).toBeTruthy();
    expect(within(picks).getByText(/560 to 610/)).toBeTruthy();
    for (const sentence of [
      'Stand-in: which way it leans.', 'Stand-in: at expiry, if held, before fees.', 'Stand-in: the most it loses at expiry.',
      'Stand-in: the most it gains at expiry.', 'Stand-in: where it is even.', 'Stand-in: a market exit can do worse.',
      'Stand-in: what opening and closing at once would cost.', 'Stand-in: fees are an estimate.',
      'Stand-in: strikes are counted from the live mid.', 'Stand-in: the bot picks again at every entry.',
    ]) {
      expect(within(picks).getByText(sentence)).toBeTruthy();
    }
    const money = picks.querySelector<HTMLElement>('dl.figures__money');
    expect(money && termsIn(money)).toEqual({
      'Premium': '$194.00 paid',
      'Alpaca’s requirement': '$0.00',
      'Fees (estimate)': '$0.37',
      'Set aside, with fees': '$194.37',
      'Open and close at once': '$11.00 · 5.66%',
    });
  });

  it('flags a round trip over the owner’s limit with a mark beside the server’s sentence', async () => {
    const answer = resolvedPreview();
    await renderBuilder({ service: previewService({ ...answer, money: answer.money && { ...answer.money, over_cost_limit: true } }) });

    const cost = (await screen.findByText('Open and close at once')).closest('div');
    expect(cost?.querySelector('dd')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('! $11.00 · 5.66%');
    expect(cost?.classList.contains('figures__over')).toBe(true);
  });

  it.each([
    'plan_refused', 'no_quote_source', 'feed_not_up', 'no_quote_line', 'listing_not_read', 'quotes_arriving',
    'underlying_quote_unusable', 'pricing_inputs_unknown', 'search_not_done', 'not_resolved',
  ] as const)('shows the sentence of “%s” and no figures', async (state) => {
    const notice = `Stand-in: the sentence for ${state}.`;
    // An answer that still carries what was read is shown none of it: only a resolved one has figures.
    const { legs, money, at_expiry, lean, watched, the_money_sentence } = resolvedPreview();
    await renderBuilder({ service: previewService(previewWithoutFigures(state, notice, { legs, money, at_expiry, lean, watched, the_money_sentence })) });

    const picks = screen.getByRole('region', { name: 'What this picks right now' });
    expect(await within(picks).findByText(notice)).toBeTruthy();
    expect(within(picks).queryByRole('list')).toBeNull();
    expect(picks.querySelector('dl')).toBeNull();
    expect(within(picks).queryByText(/IBKR quotes read/)).toBeNull();
    expect(within(picks).queryByText(/Stand-in: (which way|at expiry|the most|strikes are counted)/)).toBeNull();
    expect(within(picks).getByText('Stand-in: the bot picks again at every entry.')).toBeTruthy();
    // A half the server does not know leaves the verdict neither red nor green.
    const verdict = screen.getByText('Stand-in: what it resolves to is not known.').closest('app-option-verdict');
    expect(verdict?.className).not.toMatch(/option-verdict--(red|green)/);
  });

  it('says in the server’s words, or its own, when the preview could not be read at all', async () => {
    const service = previewService();
    service.previewOptionStructure.mockRejectedValue(new Error('offline'));
    await renderBuilder({ service });

    expect((await screen.findByRole('alert')).textContent).toContain('The preview could not be read.');
    expect(screen.getByRole('status').textContent).toContain('Not known yet.');
  });

  it('will not use a structure whose ask failed, until a Refresh brings the server’s verdict', async () => {
    const service = previewService();
    service.previewOptionStructure.mockRejectedValueOnce(new Error('offline'));
    const { use } = await renderBuilder({ service });
    await screen.findByRole('alert');

    const useIt = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this structure' });
    expect(useIt.disabled).toBe(true);
    await userEvent.click(useIt);
    expect(use).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Stand-in: it can be deployed.');
    await vi.waitFor(() => expect(useIt.disabled).toBe(false));
    await userEvent.click(useIt);
    expect(use).toHaveBeenCalledWith({ structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS });
  });

  it('will not use legs the server has not judged yet: a red verdict on the legs as they were does not let an edit through', async () => {
    const service = previewService(resolvedPreview({ verdict: RED_VERDICT }));
    const { use } = await renderBuilder({ service });
    await screen.findByText('Stand-in: leg 2 lands on the strike of leg 1.');
    const useIt = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this structure' });
    // The ask for the edited legs is on its way and has not answered.
    let answerNow: (answer: OptionStructurePreview) => void = () => undefined;
    service.previewOptionStructure.mockReturnValue(new Promise((resolve) => { answerNow = resolve; }));

    fireEvent.change(box('Leg 1 quantity'), { target: { value: '2' } });
    fireEvent.click(useIt);
    await vi.waitFor(() => expect(service.previewOptionStructure).toHaveBeenCalledTimes(2));

    expect(useIt.disabled).toBe(true);
    fireEvent.click(useIt);
    expect(use).not.toHaveBeenCalled();

    answerNow(resolvedPreview());
    await vi.waitFor(() => expect(useIt.disabled).toBe(false));
  });

  it('holds “Use this structure” while the owner’s Refresh is on its way, and not while the page asks again by itself', async () => {
    vi.useFakeTimers();
    const service = waitingService();
    await renderBuilder({ service });
    await vi.advanceTimersByTimeAsync(50);
    const useIt = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this structure' });
    const refresh = screen.getByRole<HTMLButtonElement>('button', { name: 'Refresh' });
    expect(useIt.disabled).toBe(false);

    // The page's own ask, a second later, never answers: the verdict on screen is still the server's on these legs.
    service.previewOptionStructure.mockReturnValue(new Promise(() => undefined));
    await vi.advanceTimersByTimeAsync(1_100);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(2);
    expect(useIt.disabled).toBe(false);
    expect(refresh.disabled).toBe(false);

    // The owner asked for a new verdict: nothing is taken until it comes.
    fireEvent.click(refresh);
    await vi.advanceTimersByTimeAsync(50);
    expect(useIt.disabled).toBe(true);
  });

  it('asks again when the legs change and when the owner refreshes, under the one key', async () => {
    const { service } = await renderBuilder();
    await vi.waitFor(() => expect(service.previewOptionStructure).toHaveBeenCalledTimes(1));
    expect(service.previewOptionStructure.mock.calls[0][0]).toEqual(TARGET);
    expect(lastAsk(service)).toMatchObject({
      preview_key: 'preview-key-1', underlying: 'SPY', listens_to: 'SPY', structures: 1, gap_limit_percent: '8', cost_limit_percent: '10',
    });

    await userEvent.selectOptions(screen.getByLabelText('Leg 2 side'), 'Buy');
    await vi.waitFor(() => expect(service.previewOptionStructure).toHaveBeenCalledTimes(2));
    expect(lastAsk(service).legs[1].position).toBe('long');
    expect(lastAsk(service).preview_key).toBe('preview-key-1');

    await screen.findByText('Stand-in: it can be deployed.');
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.waitFor(() => expect(service.previewOptionStructure).toHaveBeenCalledTimes(3));
  });

  it('asks again after the wait the server names and no sooner, and not at all once it is closed', async () => {
    vi.useFakeTimers();
    const service = previewService(previewWithoutFigures('quotes_arriving', 'Stand-in: quotes are arriving.', { ask_again_in_ms: 1_000 }));
    const { fixture } = await renderBuilder({ service });
    await vi.advanceTimersByTimeAsync(50);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(900);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(200);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(2);

    fixture.destroy();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(2);
  });

  it('stops asking by itself after a bounded run for a state that never passes, and says so beside Refresh', async () => {
    vi.useFakeTimers();
    const service = waitingService();
    await renderBuilder({ service });
    await vi.advanceTimersByTimeAsync(50);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(STOPPED_ASKING)).toBeNull();

    await vi.advanceTimersByTimeAsync(PAST_A_WHOLE_RUN_MS);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1 + PREVIEW_ASK_AGAIN_LIMIT);

    // An hour on, the page has asked nothing more: its hold on quote lines is left to lapse.
    await vi.advanceTimersByTimeAsync(60 * 60_000);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1 + PREVIEW_ASK_AGAIN_LIMIT);
    // The server's sentence stands. The page's own note sits beside its own control and describes it.
    const picks = screen.getByRole('region', { name: 'What this picks right now' });
    expect(within(picks).getByText('Stand-in: quotes are arriving.')).toBeTruthy();
    const note = within(picks).getByText(STOPPED_ASKING);
    const refresh = within(picks).getByRole('button', { name: 'Refresh' });
    expect(refresh.getAttribute('aria-describedby')).toBe(note.id);
    expect(refresh.hasAttribute('disabled')).toBe(false);
  });

  it('asks nothing while the page is hidden, and once when it is shown again', async () => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    onTestFinished(() => visibility.mockRestore());
    const service = waitingService();
    await renderBuilder({ service });
    await vi.advanceTimersByTimeAsync(50);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);

    visibility.mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60 * 60_000);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);

    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(50);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(2);

    // Shown, it goes on as before: again after the wait, and no sooner.
    await vi.advanceTimersByTimeAsync(800);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(300);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(3);
  });

  it.each([0, -1_000])('waits a second before asking again when the server names a wait of %i ms', async (wait) => {
    vi.useFakeTimers();
    const service = waitingService(wait);
    await renderBuilder({ service });
    await vi.advanceTimersByTimeAsync(50);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(900);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(200);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(2);
  });

  it('starts a new run of asks when the owner refreshes, and when the legs change', async () => {
    vi.useFakeTimers();
    const service = waitingService();
    await renderBuilder({ service });
    const wholeRun = 1 + PREVIEW_ASK_AGAIN_LIMIT;
    await vi.advanceTimersByTimeAsync(PAST_A_WHOLE_RUN_MS);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(wholeRun);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.advanceTimersByTimeAsync(50);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(wholeRun + 1);
    expect(screen.queryByText(STOPPED_ASKING)).toBeNull();
    await vi.advanceTimersByTimeAsync(PAST_A_WHOLE_RUN_MS);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(2 * wholeRun);
    expect(screen.getByText(STOPPED_ASKING)).toBeTruthy();

    fireEvent.change(box('Leg 1 Delta target, delta'), { target: { value: '0.45' } });
    await vi.advanceTimersByTimeAsync(PAST_A_WHOLE_RUN_MS);
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(3 * wholeRun);
    expect(lastAsk(service).legs[0].strike).toMatchObject({ target: 0.45 });
  });

  it.each([
    ['a resolved answer', resolvedPreview()],
    ['a red verdict', resolvedPreview({ verdict: RED_VERDICT })],
    ['a state with no figures', previewWithoutFigures('no_quote_line', 'Stand-in: no quote line is spare.')],
  ])('has no accessibility violation with %s, a fixed date and a row left unfinished', async (_what, answer) => {
    await renderBuilder({ service: previewService(answer) });
    await screen.findByText('Stand-in: the bot picks again at every entry.');
    await userEvent.click(screen.getByRole('button', { name: 'Add a leg' }));
    await userEvent.selectOptions(screen.getByLabelText('Leg 3 expiry'), 'Fixed date');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });

  it('does not ask again by itself for an answer that names no wait', async () => {
    vi.useFakeTimers();
    const { service } = await renderBuilder();
    await vi.advanceTimersByTimeAsync(50);

    await vi.advanceTimersByTimeAsync(60_000);
    // Coming back to the page asks nothing either: no ask fell due while it was away.
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60_000);

    expect(service.previewOptionStructure).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(STOPPED_ASKING)).toBeNull();
  });
});
