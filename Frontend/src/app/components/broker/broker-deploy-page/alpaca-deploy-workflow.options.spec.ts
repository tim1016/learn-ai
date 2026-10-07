import type { ComponentFixture } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, onTestFinished, vi } from 'vitest';

import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { resourceTarget } from '../../../fleet/resource-target';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import {
  BrokerV2PanelService,
  type BotEndPreviewRequest,
  type DeployBotBody,
  type OptionStructurePreviewRequest,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';
import { DEPLOY_VIEW, previewedEnd } from './alpaca-deploy-workflow.fixtures';
import { DEPLOY_TYPING_SETTLE_MS } from './deploy-money-step.component';
import { BULL_CALL_SPREAD_LEGS, OPTION_CATALOGUE, resolvedPreview } from './option-structure/option-structure.fixtures';

/**
 * Deploy's Options branch (#2913, #2915): the third choice of "What it
 * trades", the structure's summary in What, the option order terms that
 * stand where the stock exit terms stood in How, and the rule that nothing
 * is deployed while Options is chosen. Split from the workflow spec, as its
 * end and symbol-scope specs are, for its size.
 */

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const FENCE = { bindingGeneration: 3, routingEpoch: 4 };
const TRADES_SHARES = 'Trades SPY shares, the symbol it listens to';

function mockService() {
  return {
    getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW),
    previewStartAdmission: vi.fn().mockResolvedValue({ allowed: true }),
    previewBotEnd: vi.fn().mockImplementation(async (_target, body: BotEndPreviewRequest) => previewedEnd(body)),
    previewBudget: vi.fn().mockImplementation(async (_target, body: DeployBotBody) => ({
      state: 'ready', detail: 'Money is ready.', world: 'real_paper', custody_account_id: 'PA9', risk_revision: 1,
      minimum_budget_usd: '500.00', unreserved_usd: '1000.00', shortcuts: [], money_after: fakeAccountMoney(),
      review_token: body.budget ? 'reviewed-money' : null, budget_usd: body.budget?.amount_usd ?? null,
      trades_summary: `${body.symbol} · 1 share`,
    })),
    getDeployTradedAsset: vi.fn().mockResolvedValue({ refusal: null, warning: null }),
    deployBudgetBot: vi.fn().mockResolvedValue({
      status: 'deployed', outcome: 'success', receipt_id: 'r-1', command_id: 'c-1', recorded_at_ms: 1,
      first_deployed_at_ms: 1, strategy_instance_id: 'spy-dv-20260929-0931', run_id: 'run-1', account_id: 'PA9',
      world: 'real_paper', committed_usd: '1000.00', message: 'deployed', explanation: 'set aside',
      next_action: 'Open it.', replaces_strategy_instance_id: null,
    }),
    getOptionStructures: vi.fn().mockResolvedValue(OPTION_CATALOGUE),
    previewOptionStructure: vi.fn<(target: unknown, body: OptionStructurePreviewRequest) => Promise<unknown>>()
      .mockResolvedValue(resolvedPreview()),
  };
}
type ServiceDouble = ReturnType<typeof mockService>;

/** Wide enough for the steps to stand side by side, every one open. */
function roomyViewport(): void {
  const spy = vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
    matches: true, media: query, onchange: null, addListener: () => undefined, removeListener: () => undefined,
    addEventListener: () => undefined, removeEventListener: () => undefined, dispatchEvent: () => false,
  }));
  onTestFinished(() => spy.mockRestore());
}

async function renderWorkflow(service: ServiceDouble = mockService()) {
  roomyViewport();
  const rendered = await render(AlpacaDeployWorkflowComponent, {
    providers: [
      ...fakePickerWorld().providers,
      provideFleetDirectory(),
      provideRouter([]),
      { provide: DEPLOY_TYPING_SETTLE_MS, useValue: 0 },
      { provide: BrokerV2PanelService, useValue: service },
    ],
    componentInputs: { fence: FENCE, target: TARGET, accountId: 'PA9' },
  });
  await screen.findByText('What', { selector: 'h2' });
  await rendered.fixture.whenStable();
  return { ...rendered, service };
}

/** A step: the section its `h2` heading labels. */
function step(name: 'What' | 'How' | 'Money' | 'Confirm'): HTMLElement {
  const heading = screen.getByText(name, { selector: 'h2' });
  const section = heading.closest<HTMLElement>(`section[aria-labelledby="${heading.id}"]`);
  if (section === null) throw new Error(`No section is labelled by the step heading ${name}.`);
  return section;
}

function deployButton(): HTMLButtonElement {
  return within(step('Confirm')).getByText<HTMLButtonElement>(/^Deploy/, { selector: 'button' });
}

/** Open the "What it trades" line and choose one of its three. */
function trade(choice: 'Same as signal' | 'Another stock or ETF' | 'Options'): void {
  const what = step('What');
  const closed = within(what).queryByRole('button', { name: TRADES_SHARES });
  if (closed !== null) fireEvent.click(closed);
  fireEvent.click(within(what).getByRole('radio', { name: choice }));
}

/** Choose Options and wait for what the builder offers. */
async function tradeOptions(): Promise<void> {
  trade('Options');
  await within(step('What')).findByRole('button', { name: 'Edit structure' });
}

/** Build a bull call spread in the drawer and take it into the form. */
async function useBullCallSpread(): Promise<void> {
  fireEvent.click(within(step('What')).getByRole('button', { name: 'Edit structure' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Bull call spread' }));
  // Its legs are on screen, and then the server's verdict on them: only a judged structure is taken.
  await screen.findByRole('heading', { level: 3, name: 'Bull call spread' });
  const useIt = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this structure' });
  await vi.waitFor(() => expect(useIt.disabled).toBe(false));
  fireEvent.click(useIt);
  await within(step('What')).findByRole('region', { name: 'Bull call spread' });
}

/** What the option order term boxes hold, in the order they stand. */
function termValues(terms: HTMLElement): string[] {
  return Array.from(terms.querySelectorAll('input')).map((box) => box.value);
}

function chooseMoney(amount = '1000.00'): void {
  fireEvent.input(screen.getByLabelText(/\(USD\)$/), { target: { value: amount } });
}

function pickers(fixture: ComponentFixture<unknown>): SymbolPickerComponent[] {
  return fixture.debugElement.queryAll(By.directive(SymbolPickerComponent)).map((found) => found.componentInstance as SymbolPickerComponent);
}

/** A beat for anything the form might send to settle. */
function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 30));
}

describe('AlpacaDeployWorkflowComponent — a bot that trades options (#2913)', () => {
  it('offers Options as the third choice, and reads what the builder offers only once it is chosen', async () => {
    const { service } = await renderWorkflow();
    const what = step('What');
    fireEvent.click(within(what).getByRole('button', { name: TRADES_SHARES }));

    expect(within(what).getAllByRole('radio').map((radio) => radio.closest('label')?.textContent?.trim())).toEqual([
      'Same as signal', 'Another stock or ETF', 'Options',
    ]);
    expect(service.getOptionStructures).not.toHaveBeenCalled();

    await tradeOptions();

    expect(service.getOptionStructures).toHaveBeenCalledOnce();
    expect(service.getOptionStructures.mock.calls[0][0]).toMatchObject({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' });
    // The underlying starts as the symbol the bot listens to, in a picker of its own.
    expect(within(what).getByRole('combobox', { name: 'Options on' }).textContent).toContain('SPY');
    // Nothing is built yet: the summary is the catalogue's line for Custom, and nothing is asked.
    expect(within(within(what).getByRole('region', { name: 'Custom' })).getByText('Stand-in: what Custom starts from.')).toBeTruthy();
    expect(service.previewOptionStructure).not.toHaveBeenCalled();
  });

  it('shows the structure the owner built, and what it would pick right now', async () => {
    const { service } = await renderWorkflow();
    await tradeOptions();

    await useBullCallSpread();

    const what = step('What');
    expect(within(what).getByText('Stand-in: what a bull call spread is.')).toBeTruthy();
    const picks = within(what).getByRole('region', { name: 'Right now this picks' });
    expect(await within(picks).findByRole('button', { name: "Show contract code for SPY Oct 9 '26 585 Call" })).toBeTruthy();
    expect(within(what).getByText('Stand-in: it can be deployed.')).toBeTruthy();
    expect(service.previewOptionStructure.mock.calls.at(-1)?.[1]).toMatchObject({
      underlying: 'SPY', listens_to: 'SPY', structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS,
      structures: 1, gap_limit_percent: '8', cost_limit_percent: '10',
    });
  });

  it('puts the option order terms where the stock exit terms were, and sizes in whole structures', async () => {
    await renderWorkflow();
    const how = step('How');
    expect(within(how).getByRole('group', { name: /^Exit terms/ })).toBeTruthy();
    expect(within(how).getByRole('radio', { name: 'One share' })).toBeTruthy();

    await tradeOptions();

    expect(within(how).queryByRole('group', { name: /^Exit terms/ })).toBeNull();
    const terms = await within(how).findByRole('group', { name: 'Option order terms · fixed for this bot’s life' });
    await vi.waitFor(() => expect(termValues(terms)).toEqual(['8', '30', '10', '45']));
    expect(within(how).getByRole('radio', { name: 'One structure' })).toBeTruthy();
    expect(within(how).queryByRole('radio', { name: 'One share' })).toBeNull();

    fireEvent.click(within(how).getByRole('radio', { name: 'Custom structures' }));
    expect(within(how).getByLabelText('Whole structures per entry')).toBeTruthy();
  });

  it('starts the size over when it stops counting shares and counts structures, and back', async () => {
    const { fixture, service } = await renderWorkflow();
    const how = step('How');
    const checked = (name: string): boolean => within(how).getByRole<HTMLInputElement>('radio', { name }).checked;
    const typeSize = (label: string, size: string): void => {
      fireEvent.input(within(how).getByLabelText(label), { target: { value: size } });
    };
    fireEvent.click(within(how).getByRole('radio', { name: 'Custom shares' }));
    typeSize('Whole shares per entry', '50');

    // Fifty shares are not fifty structures.
    await tradeOptions();
    expect(checked('One structure')).toBe(true);
    expect(within(how).queryByLabelText('Whole structures per entry')).toBeNull();
    await useBullCallSpread();
    await vi.waitFor(() => expect(service.previewOptionStructure.mock.calls.at(-1)?.[1]).toMatchObject({ structures: 1 }));
    fireEvent.click(within(how).getByRole('radio', { name: 'Custom structures' }));
    expect(within(how).getByLabelText<HTMLInputElement>('Whole structures per entry').value).toBe('1');

    // And three structures are not three shares, whichever way Options is left.
    typeSize('Whole structures per entry', '3');
    trade('Same as signal');
    fixture.detectChanges();
    expect(checked('One share')).toBe(true);
    fireEvent.click(within(how).getByRole('radio', { name: 'Custom shares' }));
    expect(within(how).getByLabelText<HTMLInputElement>('Whole shares per entry').value).toBe('1');

    await tradeOptions();
    fireEvent.click(within(how).getByRole('radio', { name: 'Custom structures' }));
    typeSize('Whole structures per entry', '3');
    trade('Another stock or ETF');
    fixture.detectChanges();
    expect(checked('One share')).toBe(true);
  });

  it('keeps the size when what the bot trades changes and its unit does not', async () => {
    const { fixture } = await renderWorkflow();
    const how = step('How');
    fireEvent.click(within(how).getByRole('radio', { name: 'Custom shares' }));
    fireEvent.input(within(how).getByLabelText('Whole shares per entry'), { target: { value: '50' } });

    trade('Another stock or ETF');
    pickers(fixture)[1].symbol.set('AAPL');
    fixture.detectChanges();

    expect(within(how).getByLabelText<HTMLInputElement>('Whole shares per entry').value).toBe('50');
  });

  it('lets go of a stock the owner had picked when they choose Options instead', async () => {
    const { fixture } = await renderWorkflow();
    trade('Another stock or ETF');
    pickers(fixture)[1].symbol.set('AAPL');
    fixture.detectChanges();
    const what = step('What');
    expect(within(what).getByRole('combobox', { name: 'Trades' }).textContent).toContain('AAPL');

    await tradeOptions();
    // Options are on what the bot listens to, never on the stock that was picked.
    expect(within(what).getByRole('combobox', { name: 'Options on' }).textContent).toContain('SPY');
    trade('Another stock or ETF');
    fixture.detectChanges();

    // Back on a stock, none is picked: the earlier pick did not ride along under Options.
    expect(within(what).getByRole('combobox', { name: 'Trades' }).textContent).not.toContain('AAPL');
  });

  it('says in How why the order terms could not be read, and no longer that it is reading them', async () => {
    const service = mockService();
    service.getOptionStructures.mockRejectedValue(new Error('offline'));
    await renderWorkflow(service);

    trade('Options');

    // What says it as an alert, beside the retry; How says the same in place of its boxes.
    expect((await within(step('What')).findByRole('alert')).textContent).toContain('The option structures could not be read.');
    const terms = within(step('How')).getByRole('group', { name: /^Option order terms/ });
    await vi.waitFor(() => expect(terms.textContent).toContain('The option structures could not be read.'));
    expect(terms.textContent).not.toContain('Reading the option order terms');
    expect(terms.querySelector('input')).toBeNull();
  });

  it('asks for no preview while an order term holds what the server refuses', async () => {
    const { service } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();
    const how = step('How');
    await within(step('What')).findByText('Stand-in: which way it leans.');
    const asked = service.previewOptionStructure.mock.calls.length;

    // A widest gap of nothing is refused by the route: it is not asked, and How's header says what is missing.
    fireEvent.input(within(how).getByLabelText(/^Widest bid-ask gap/), { target: { value: '0' } });
    fireEvent.blur(within(how).getByLabelText(/^Widest bid-ask gap/));
    await settle();

    expect(service.previewOptionStructure).toHaveBeenCalledTimes(asked);
    expect(within(how).getByRole('alert').textContent).toContain('Must be more than 0.');

    fireEvent.input(within(how).getByLabelText(/^Widest bid-ask gap/), { target: { value: '.3' } });
    await vi.waitFor(() => expect(service.previewOptionStructure.mock.calls.at(-1)?.[1]).toMatchObject({ gap_limit_percent: '.3' }));
    expect(within(how).queryByRole('alert')).toBeNull();
  });

  it('previews the size and the limits the owner set in How', async () => {
    const { service } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();
    const how = step('How');

    fireEvent.click(within(how).getByRole('radio', { name: 'Custom structures' }));
    fireEvent.input(within(how).getByLabelText('Whole structures per entry'), { target: { value: '3' } });
    fireEvent.input(within(how).getByLabelText(/^Whole-structure cost/), { target: { value: '7.5' } });

    await vi.waitFor(() => expect(service.previewOptionStructure.mock.calls.at(-1)?.[1]).toMatchObject({
      structures: 3, cost_limit_percent: '7.5',
    }));
  });

  it('names the structure and its order terms at Confirm, never shares or exit terms', async () => {
    await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();

    const review = within(step('Confirm')).getByLabelText('Deploy review');
    await vi.waitFor(() => expect(review.textContent).toContain('SPY → Bull call spread · SPY · 1 structure'));
    expect(within(review).getByText('Orders')).toBeTruthy();
    expect(within(review).queryByText('Exits')).toBeNull();
    expect(review.textContent).toContain('Widest bid-ask gap, each leg 8% of mid');
    expect(review.textContent).not.toContain('Exit allowance');
    expect(review.textContent).not.toContain('1 share');
  });

  it('has no accessibility violation with Options chosen and a structure built', async () => {
    await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();
    await within(step('What')).findByText('Stand-in: which way it leans.');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });

  it('clears everything set under Options when the owner goes back to the same symbol', async () => {
    const { service } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();
    const how = step('How');
    fireEvent.input(within(how).getByLabelText(/^Entry wait/), { target: { value: '20' } });

    trade('Same as signal');

    const what = step('What');
    expect(within(what).queryByRole('button', { name: 'Edit structure' })).toBeNull();
    expect(within(what).queryByRole('region', { name: 'Right now this picks' })).toBeNull();
    expect(within(how).getByRole('group', { name: /^Exit terms/ })).toBeTruthy();
    expect(within(how).queryByRole('group', { name: /^Option order terms/ })).toBeNull();
    expect(within(how).getByRole('radio', { name: 'One share' })).toBeTruthy();
    // The quote lines are let go: nothing asks for a preview any more.
    const asked = service.previewOptionStructure.mock.calls.length;
    await settle();
    expect(service.previewOptionStructure).toHaveBeenCalledTimes(asked);

    // Chosen again, it starts over: no structure, and the catalogue's own terms.
    await tradeOptions();
    expect(within(what).getByRole('region', { name: 'Custom' })).toBeTruthy();
    const terms = await within(how).findByRole('group', { name: /^Option order terms/ });
    await vi.waitFor(() => expect(termValues(terms)).toEqual(['8', '30', '10', '45']));
  });

  it('leaves Options when the owner chooses another stock or ETF instead', async () => {
    const { fixture } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();

    trade('Another stock or ETF');
    fixture.detectChanges();

    const what = step('What');
    expect(within(what).queryByRole('button', { name: 'Edit structure' })).toBeNull();
    expect(within(what).getByRole('combobox', { name: 'Trades' })).toBeTruthy();
    expect(within(step('How')).getByRole('group', { name: /^Exit terms/ })).toBeTruthy();
  });

  it('keeps the underlying the owner picked when the bot listens to another symbol', async () => {
    const { fixture, service } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();

    pickers(fixture)[0].symbol.set('AAPL');
    fixture.detectChanges();

    await vi.waitFor(() => expect(service.previewOptionStructure.mock.calls.at(-1)?.[1]).toMatchObject({
      underlying: 'SPY', listens_to: 'AAPL',
    }));
  });

  it('cannot deploy anything while Options is chosen, and reviews no share budget for it', async () => {
    const { fixture, service } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();
    await settle();
    const reviewed = service.previewBudget.mock.calls.length;

    chooseMoney();
    await settle();

    // Money prices shares, which this bot will not trade: it is asked nothing.
    expect(service.previewBudget).toHaveBeenCalledTimes(reviewed);
    expect(screen.queryByText(/would be set aside for this bot/)).toBeNull();
    expect(deployButton().disabled).toBe(true);
    // No sentence of this form's own stands in for the server's blockers.
    expect(document.getElementById('deploy-submit-guidance')?.textContent?.trim()).toBe('');

    fireEvent.click(deployButton());
    await fixture.componentInstance['submit']();

    expect(service.previewStartAdmission).not.toHaveBeenCalled();
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  it('cannot deploy a share bot by accident with a budget that was reviewed before Options was chosen', async () => {
    const { fixture, service } = await renderWorkflow();
    chooseMoney();
    await screen.findByText(/would be set aside for this bot/, {}, { timeout: 3000 });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));

    await tradeOptions();

    expect(deployButton().disabled).toBe(true);
    fireEvent.click(deployButton());
    await fixture.componentInstance['submit']();
    expect(service.previewStartAdmission).not.toHaveBeenCalled();
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  it('deploys a share bot as before once the owner leaves Options, with nothing of them in the request', async () => {
    const { service } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();

    trade('Same as signal');
    chooseMoney();
    await screen.findByText(/would be set aside for this bot/, {}, { timeout: 3000 });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    fireEvent.click(deployButton());

    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    const sent = service.deployBudgetBot.mock.calls[0][1] as Record<string, unknown>;
    expect(sent).toMatchObject({ symbol: 'SPY', sizing: { preset: 'safe_canary', quantity: 1 } });
    expect(Object.keys(sent).filter((key) => /option|structure|legs|underlying/.test(key))).toEqual([]);
    expect(sent['traded_symbol']).toBeUndefined();
  });

  it('reads nothing about options for a bot that trades shares', async () => {
    const { service } = await renderWorkflow();
    chooseMoney();
    await screen.findByText(/would be set aside for this bot/, {}, { timeout: 3000 });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    expect(within(step('Confirm')).getByText('Exits')).toBeTruthy();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());

    expect(service.getOptionStructures).not.toHaveBeenCalled();
    expect(service.previewOptionStructure).not.toHaveBeenCalled();
  });
});
