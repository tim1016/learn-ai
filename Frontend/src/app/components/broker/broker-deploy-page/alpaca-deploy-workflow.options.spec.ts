import { HttpErrorResponse } from '@angular/common/http';
import type { ComponentFixture } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
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
  type BotDeployPrefill,
  type BotEndPreviewRequest,
  type DeployBotBody,
  type DeploymentBudgetPreview,
  type OptionStructurePreviewRequest,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';
import { DEPLOY_VIEW, LIVE_DEPLOY_VIEW, SHADOW_DEPLOY_VIEW, previewedEnd } from './alpaca-deploy-workflow.fixtures';
import { DEPLOY_TYPING_SETTLE_MS } from './deploy-money-step.component';
import {
  NOT_PRICED_REFUSAL,
  NO_SEALED_TERMS_REFUSAL,
  OPTIONS_WARNING,
  OPTIONS_WARNING_TO_ACCEPT,
  OPTION_PREFILL,
  OPTION_REVIEW_BLOCKED,
  OPTION_REVIEW_NO_FIX,
  OPTION_REVIEW_PRICED,
  OPTION_REVIEW_THAT_PAYS,
  OPTION_REVIEW_WITH_BUDGET,
  OPTION_TRADES_SUMMARY,
} from './option-deploy.fixtures';
import { BULL_CALL_SPREAD_LEGS, OPTION_CATALOGUE, resolvedPreview } from './option-structure/option-structure.fixtures';

/**
 * Deploy's Options branch (#2913, #2915): the third choice of "What it
 * trades", the structure's summary in What, the option order terms that
 * stand beside the custody exit seal in How, and the Deploy of an option
 * bot: what it sends, the server's money review, blockers, acceptance and
 * refusals, and Deploy again. Split from the workflow spec, as its end and
 * symbol-scope specs are, for its size.
 */

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const FENCE = { bindingGeneration: 3, routingEpoch: 4 };
const TRADES_SHARES = 'Trades SPY shares, the symbol it listens to';

/** What the money review answers for a bot in shares, and what it adds for an option bot. */
type OptionAnswer = (body: DeployBotBody) => Partial<DeploymentBudgetPreview>;

/** The server's answer for an option bot whose structure is priced: reviewed once an amount rides the request. */
const PRICED: OptionAnswer = (body) => ({
  trades_summary: OPTION_TRADES_SUMMARY,
  traded_asset_warning: body.execution_mode === 'dry_run' ? OPTIONS_WARNING : OPTIONS_WARNING_TO_ACCEPT,
  option_review: body.budget ? OPTION_REVIEW_WITH_BUDGET : OPTION_REVIEW_PRICED,
});

/** A review something stops: no token, and the first blocker's sentence as the budget's own. */
function stopped(review: typeof OPTION_REVIEW_BLOCKED): OptionAnswer {
  return () => ({
    state: 'unavailable', review_token: null, detail: review.blockers?.[0]?.headline ?? '',
    trades_summary: OPTION_TRADES_SUMMARY, option_review: review,
  });
}

function mockService(optionAnswer: OptionAnswer = PRICED) {
  return {
    getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW),
    previewStartAdmission: vi.fn().mockResolvedValue({
      allowed: true, reason_code: 'ADMITTED', explanation: 'Start is allowed.', next_step: null,
      evaluated_at_ms: 1, fact_ages_ms: { runtime: 0, process: 0, market_data: 0, clerk: 0 },
    }),
    previewBotEnd: vi.fn().mockImplementation(async (_target, body: BotEndPreviewRequest) => previewedEnd(body)),
    previewBudget: vi.fn().mockImplementation(async (_target, body: DeployBotBody) => ({
      state: 'ready', detail: 'Money is ready.',
      world: body.execution_mode === 'live' ? 'real_live' : body.execution_mode === 'shadow' ? 'shadow' : body.execution_mode === 'dry_run' ? 'dry_run' : 'real_paper',
      risk_revision: 1,
      minimum_budget_usd: '500.00', unreserved_usd: '1000.00', shortcuts: [], money_after: fakeAccountMoney(),
      review_token: body.budget ? 'reviewed-money' : null, budget_usd: body.budget?.amount_usd ?? null,
      confirmation_text: body.budget && body.execution_mode === 'live' ? 'Deploy PA9 with $1000.00' : null,
      trades_summary: `${body.symbol} · 1 share`,
      // Only a bot that trades options is answered with a review of them.
      ...(body.trades ? optionAnswer(body) : {}),
    })),
    getDeployTradedAsset: vi.fn().mockResolvedValue({ refusal: null, warning: null }),
    getDeployOptionsWarning: vi.fn().mockResolvedValue({ refusal: null, warning: OPTIONS_WARNING }),
    getDeployPrefill: vi.fn<(target: unknown, sid: string) => Promise<BotDeployPrefill>>(),
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

async function renderWorkflow(service: ServiceDouble = mockService(), query: Record<string, string> = {}) {
  roomyViewport();
  const initial = convertToParamMap(query);
  const rendered = await render(AlpacaDeployWorkflowComponent, {
    providers: [
      ...fakePickerWorld().providers,
      provideFleetDirectory(),
      provideRouter([]),
      { provide: ActivatedRoute, useValue: { queryParamMap: new BehaviorSubject(initial), snapshot: { queryParamMap: initial } } },
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

/** The bull call spread as a Deploy names it: the catalogue's legs under the catalogue's starting terms. */
const SPREAD_TRADES = {
  kind: 'options', underlying: 'SPY', structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS,
  order_terms: { gap_limit_percent: '8', entry_wait_seconds: 20, cost_limit_percent: '10', expiry_close_lead_minutes: 45 },
};
/** The account's defaults for new bots, which every bot is sealed with. */
const EXIT_TERMS = DEPLOY_VIEW.default_exit_terms;

/** The settings the money review was last asked about. */
function lastBudgetAsk(service: ServiceDouble): DeployBotBody {
  const call = service.previewBudget.mock.calls.at(-1);
  if (call === undefined) throw new Error('The money review was never asked.');
  return call[1] as DeployBotBody;
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

  it('shows a built structure in every step: its picks in What, its money review, its sizes in How and its lines at Confirm', async () => {
    const { service } = await renderWorkflow();
    await tradeOptions();

    await useBullCallSpread();

    const built = 'shows the structure the owner built, and what it would pick right now';
    const what = step('What');
    expect.soft(within(what).getByText('Stand-in: what a bull call spread is.'), built).toBeTruthy();
    const picks = within(what).getByRole('region', { name: 'Right now this picks' });
    expect.soft(await within(picks).findByLabelText("SPY Oct 9 '26 585 Call"), built).toBeTruthy();
    expect.soft(within(what).getByText('Stand-in: it can be deployed.'), built).toBeTruthy();
    expect.soft(service.previewOptionStructure.mock.calls.at(-1)?.[1], built).toMatchObject({
      underlying: 'SPY', listens_to: 'SPY', structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS,
      structures: 1, gap_limit_percent: '8', cost_limit_percent: '10',
    });

    const confirm = 'says what it trades and how its orders go at Confirm in the server’s lines, never shares or exit terms';
    const review = within(step('Confirm')).getByLabelText('Deploy review');
    await vi.waitFor(() => expect(review.textContent, confirm).toContain(OPTION_TRADES_SUMMARY));
    expect.soft(within(review).getByText('Orders'), confirm).toBeTruthy();
    expect.soft(within(review).queryByText('Exits'), confirm).toBeNull();
    expect.soft(review.textContent, confirm).toContain(OPTION_REVIEW_PRICED.orders_summary);
    expect.soft(review.textContent, confirm).not.toContain('Exit allowance');
    expect.soft(review.textContent, confirm).not.toContain('1 share');

    const reviews = 'reviews the money of the structure with its four order terms and exit terms, sized in structures, and shows the server’s review';
    const money = step('Money');
    expect.soft(await within(money).findByText('Set aside for 2 structures'), reviews).toBeTruthy();
    expect.soft(within(money).getByRole('list', { name: 'Right now this picks' }), reviews).toBeTruthy();
    // The option review's own set-aside row stands where a share bot's minimum does.
    expect.soft(within(money).queryByText('Minimum for one position'), reviews).toBeNull();
    expect.soft(lastBudgetAsk(service), reviews)
      .toMatchObject({ symbol: 'SPY', trades: SPREAD_TRADES, exit_terms: EXIT_TERMS, sizing: { preset: 'safe_canary', quantity: 1 } });
    expect.soft(lastBudgetAsk(service).traded_symbol, reviews).toBeUndefined();

    await within(step('Money')).findByText('Set aside for 2 structures');
    expect.soft(step('How').querySelector('.deploy-option-end'), 'says nothing in How about a scheduled close for a structure with no sold leg')
      .toBeNull();

    const explains = 'explains each size option in the server’s words for structures, once it has reviewed the structure';
    const how = step('How');
    await vi.waitFor(() => expect(
      within(how).getByRole('button', { name: `About One structure: ${OPTION_REVIEW_PRICED.one_structure_explanation}` }),
      explains,
    ).toBeTruthy());
    expect.soft(
      within(how).getByRole('button', { name: `About Custom structures: ${OPTION_REVIEW_PRICED.custom_structures_explanation}` }),
      explains,
    ).toBeTruthy();
  });

  it('offers separate option order terms and the custody exit seal, sized in whole structures', async () => {
    await renderWorkflow();
    const how = step('How');
    expect(within(how).getByRole('group', { name: /^Exit terms/ })).toBeTruthy();
    expect(within(how).getByRole('radio', { name: 'One share' })).toBeTruthy();

    await tradeOptions();

    expect(within(how).getByRole('group', { name: /^Exit terms/ })).toBeTruthy();
    const terms = await within(how).findByRole('group', { name: 'Option order terms · fixed for this bot’s life' });
    await vi.waitFor(() => expect(termValues(terms)).toEqual(['8', '20', '10', '45']));
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
    expect(within(how).getByRole('alert').textContent).toContain(OPTION_CATALOGUE.order_terms.gap_limit_percent.bounds.sentence);

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
    await vi.waitFor(() => expect(termValues(terms)).toEqual(['8', '20', '10', '45']));
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

  it.each(['paper', 'shadow', 'live'] as const)('deploys an option bot in %s after acceptance and the mode’s consent, sending the same choice to the Start check and the Deploy', async (mode) => {
    const service = mockService();
    const view = mode === 'live' ? LIVE_DEPLOY_VIEW : mode === 'shadow' ? SHADOW_DEPLOY_VIEW : DEPLOY_VIEW;
    service.getDeployView.mockResolvedValue({ ...view, account_id: 'PA9' });
    await renderWorkflow(service);
    await tradeOptions();
    await useBullCallSpread();
    if (mode === 'live') fireEvent.click(within(step('How')).getByRole('radio', { name: /Live/ }));
    chooseMoney();
    await screen.findByText(/would be set aside for this bot/, {}, { timeout: 3000 });

    // Every broker mode asks the tick and the reason, and Deploy waits for both.
    const acceptance = await within(step('Confirm')).findByRole('region', { name: 'Untested traded asset' });
    expect(within(acceptance).getByText(OPTIONS_WARNING_TO_ACCEPT.headline)).toBeTruthy();
    expect(deployButton().disabled).toBe(true);
    fireEvent.click(within(acceptance).getByRole('checkbox', { name: OPTIONS_WARNING_TO_ACCEPT.acceptance_label ?? '' }));
    expect(deployButton().disabled).toBe(true);
    fireEvent.input(within(acceptance).getByLabelText('Your reason'), { target: { value: 'A spread on SPY is what I mean to try.' } });
    if (mode === 'live') {
      expect(deployButton().disabled).toBe(true);
      const phrase = within(step('Confirm')).getByLabelText(/To deploy with real money, type/);
      fireEvent.input(phrase, { target: { value: 'Deploy PA9 with $1000.01' } });
      expect(deployButton().disabled).toBe(true);
      fireEvent.input(phrase, { target: { value: 'Deploy PA9 with $1000.00' } });
    }
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    fireEvent.click(deployButton());

    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    const checked = service.previewStartAdmission.mock.calls[0][1] as DeployBotBody;
    const deployed = service.deployBudgetBot.mock.calls[0][1] as DeployBotBody;
    for (const sent of [checked, deployed]) {
      expect(sent).toMatchObject({ execution_mode: mode, symbol: 'SPY', trades: SPREAD_TRADES, exit_terms: EXIT_TERMS, sizing: { preset: 'safe_canary', quantity: 1 } });
      expect(sent.traded_symbol).toBeUndefined();
    }
    expect(deployed.traded_asset_acknowledgement).toEqual({
      acknowledgement: 'I_ACCEPT_UNTESTED_TRADED_ASSET_RISK', reason: 'A spread on SPY is what I mean to try.',
    });
  });

  it('shows a Dry Run the warning with no tick and no reason, and deploys it without them', async () => {
    const { service } = await renderWorkflow();
    await tradeOptions();
    await useBullCallSpread();
    fireEvent.click(within(step('How')).getByRole('radio', { name: /Dry Run/ }));
    chooseMoney();
    await screen.findByText(/of simulated starting cash for this bot/, {}, { timeout: 3000 });

    const acceptance = await within(step('Confirm')).findByRole('region', { name: 'Untested traded asset' });
    expect(within(acceptance).getByText(OPTIONS_WARNING.headline)).toBeTruthy();
    expect(within(acceptance).queryByRole('checkbox')).toBeNull();
    expect(within(acceptance).queryByLabelText('Your reason')).toBeNull();
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    fireEvent.click(deployButton());

    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    const deployed = service.deployBudgetBot.mock.calls[0][1] as DeployBotBody;
    expect(deployed).toMatchObject({ execution_mode: 'dry_run', trades: SPREAD_TRADES });
    expect(deployed.traded_asset_acknowledgement).toBeUndefined();
  });

  it('says the server’s warning in What the moment Options is chosen, before any money is reviewed', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    const answer = service.previewBudget.getMockImplementation();
    let release!: (value: Awaited<ReturnType<NonNullable<typeof answer>>>) => void;
    service.previewBudget.mockImplementation(() => new Promise((resolve) => { release = resolve; }));

    await tradeOptions();

    expect(await within(step('What')).findByText(OPTIONS_WARNING.headline)).toBeTruthy();
    expect(service.getDeployOptionsWarning.mock.calls.at(-1)?.slice(1)).toEqual(['SPY', 'SPY']);
    // The stock's own read is not asked about options.
    expect(service.getDeployTradedAsset).not.toHaveBeenCalled();
    if (answer === undefined) throw new Error('No budget answer was configured.');
    release(await answer(TARGET, lastBudgetAsk(service)));
    await fixture.whenStable();
  });

  it('lists what stops the Deploy in “Can’t deploy yet”, each with the server’s fix, and keeps Deploy off', async () => {
    const service = mockService(stopped(OPTION_REVIEW_BLOCKED));
    await renderWorkflow(service);
    await tradeOptions();
    await useBullCallSpread();

    const blockers = OPTION_REVIEW_BLOCKED.blockers ?? [];
    const list = await within(step('Confirm')).findByRole('region', { name: `Can’t deploy yet · ${blockers.length} things` });
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(blockers.length);
    blockers.forEach((blocker, index) => {
      expect(items[index].textContent).toContain(blocker.headline);
      expect(items[index].textContent).toContain(`Fix: ${blocker.fix}`);
    });
    chooseMoney();
    await settle();
    expect(deployButton().disabled).toBe(true);
    // Under the button, what to do about the first of them, in the server's words.
    expect(document.getElementById('deploy-submit-guidance')?.textContent?.trim()).toBe(blockers[0].fix);
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  it('lists a blocker the server gives no fix for with its sentence alone', async () => {
    await renderWorkflow(mockService(stopped(OPTION_REVIEW_NO_FIX)));
    await tradeOptions();
    await useBullCallSpread();

    const list = await within(step('Confirm')).findByRole('region', { name: 'Can’t deploy yet · 1 thing' });
    const [blocker] = OPTION_REVIEW_NO_FIX.blockers ?? [];
    expect(within(list).getByRole('listitem').textContent).toContain(blocker.headline);
    expect(within(list).queryByText(/^Fix:/)).toBeNull();
    expect(document.getElementById('deploy-submit-guidance')?.textContent?.trim()).toBe(blocker.headline);
  });

  it('shows a refused click in the server’s sentence and next step, as a refusal and never as a changed account, and reads the money again', async () => {
    const service = mockService();
    service.deployBudgetBot.mockRejectedValue(new HttpErrorResponse({ status: 409, error: NOT_PRICED_REFUSAL }));
    await renderWorkflow(service);
    await tradeOptions();
    await useBullCallSpread();
    fireEvent.click(within(step('How')).getByRole('radio', { name: /Dry Run/ }));
    chooseMoney();
    await screen.findByText(/of simulated starting cash for this bot/, {}, { timeout: 3000 });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    const reviewed = service.previewBudget.mock.calls.length;
    fireEvent.click(deployButton());

    const refusal = await within(step('Confirm')).findByRole('alert');
    expect(within(refusal).getByRole('heading', { name: 'Deploy refused' })).toBeTruthy();
    expect(refusal.textContent).toContain(NOT_PRICED_REFUSAL.detail.message);
    expect(refusal.textContent).toContain(`Next: ${NOT_PRICED_REFUSAL.detail.next_action}`);
    // What stopped it is read again, so Money shows the review as it now stands.
    await vi.waitFor(() => expect(service.previewBudget.mock.calls.length).toBeGreaterThan(reviewed));
  });

  it('says in How when a structure with a sold leg is closed, in the server’s sentence, and nothing of it otherwise', async () => {
    const service = mockService((body) => ({ ...PRICED(body), option_review: OPTION_REVIEW_THAT_PAYS }));
    await renderWorkflow(service);
    await tradeOptions();
    await useBullCallSpread();

    expect(await within(step('How')).findByText(OPTION_REVIEW_THAT_PAYS.end_sentence ?? '')).toBeTruthy();
  });

  it('lets an option bot seal its required exit terms without switching its traded asset', async () => {
    const service = mockService();
    service.getDeployView.mockImplementation(async (_target, _symbol, terms) => ({
      ...DEPLOY_VIEW, default_exit_terms: terms ?? null,
    }));
    await renderWorkflow(service);
    await tradeOptions();
    await useBullCallSpread();
    await settle();

    const how = step('How');
    const allowance = within(how).getByRole('spinbutton', { name: 'Exit allowance (bps)' });
    fireEvent.input(allowance, { target: { value: '100' } });
    fireEvent.input(within(how).getByRole('spinbutton', { name: 'Band multiple' }), { target: { value: '2' } });
    fireEvent.input(within(how).getByRole('spinbutton', { name: 'Spread cap (bps)' }), { target: { value: '50' } });
    await settle();
    await vi.waitFor(() => expect(service.previewBudget.mock.calls.some(([, body]) =>
      body.trades && body.exit_terms?.exit_allowance_bps === 100)).toBe(true));
    expect(within(how).getByRole('link', { name: 'Set defaults for new bots in Settings' })).toBeTruthy();
    fireEvent.blur(allowance);
    await vi.waitFor(() => expect(service.getDeployView.mock.calls.at(-1)?.[2]).toMatchObject({ exit_allowance_bps: 100 }));
    await settle();
    const editableAllowance = within(how).getByRole<HTMLInputElement>('spinbutton', { name: 'Exit allowance (bps)' });
    fireEvent.input(editableAllowance, { target: { value: '75' } });
    await settle();
    await vi.waitFor(() => expect(service.previewBudget.mock.calls.at(-1)?.[1].exit_terms?.exit_allowance_bps).toBe(75));
    expect(within(step('What')).getByRole<HTMLInputElement>('radio', { name: 'Options' }).checked).toBe(true);
  });

  it('fills the form from an earlier option bot on Deploy again: its legs, its sealed terms and its size', async () => {
    const service = mockService();
    service.getDeployPrefill.mockResolvedValue(OPTION_PREFILL);
    await renderWorkflow(service, { from: OPTION_PREFILL.source_strategy_instance_id });
    await screen.findByText(/Prefilled from/);

    const sealed = OPTION_PREFILL.trades;
    if (!sealed) throw new Error('The prefill fixture is an option bot’s.');
    expect(await within(step('What')).findByRole('region', { name: 'Bull call spread' })).toBeTruthy();
    const terms = await within(step('How')).findByRole('group', { name: /^Option order terms/ });
    await vi.waitFor(() => expect(termValues(terms)).toEqual(['12.5', '7', '4', '90']));
    expect(within(step('How')).getByLabelText<HTMLInputElement>('Whole structures per entry').value).toBe('3');
    await vi.waitFor(() => expect(lastBudgetAsk(service)).toMatchObject({
      symbol: 'SPY',
      trades: { kind: 'options', underlying: 'SPY', structure: 'bull_call_spread', legs: sealed.legs, order_terms: sealed.order_terms },
      sizing: { preset: 'custom', quantity: 3 },
    }));
    expect(lastBudgetAsk(service).traded_symbol).toBeUndefined();
  });

  it('says in the server’s words why an option bot with no sealed terms cannot be deployed again, and offers a fresh form', async () => {
    const service = mockService();
    service.getDeployPrefill.mockRejectedValue(new HttpErrorResponse({ status: 409, error: NO_SEALED_TERMS_REFUSAL }));
    await renderWorkflow(service, { from: 'spy-ema-20260101-0932' });

    const failure = await screen.findByRole('alert', { name: 'Deploy again' });
    expect(within(failure).getByText(NO_SEALED_TERMS_REFUSAL.detail.message)).toBeTruthy();
    expect(within(failure).getByRole('button', { name: 'Start from a fresh form' })).toBeTruthy();
    // Nothing was filled in for it: the form still trades the signal's own shares.
    expect(within(step('What')).getByRole('button', { name: TRADES_SHARES })).toBeTruthy();
  });

  it('cannot deploy a share bot by accident with a budget that was reviewed before Options was chosen', async () => {
    const { fixture, service } = await renderWorkflow();
    chooseMoney();
    await screen.findByText(/would be set aside for this bot/, {}, { timeout: 3000 });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));

    await tradeOptions();

    // No structure is built yet: there is nothing to review, and the share bot's review is no review of it.
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
    expect(service.getDeployOptionsWarning).not.toHaveBeenCalled();
  });
});
