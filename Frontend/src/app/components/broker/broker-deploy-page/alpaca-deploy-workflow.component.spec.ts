import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import { ActivatedRoute, convertToParamMap, provideRouter, Router, type ParamMap } from '@angular/router';
import axe from 'axe-core';
import { BehaviorSubject, of } from 'rxjs';
import { describe, expect, it, onTestFinished, vi } from 'vitest';

import {
  BrokerV2PanelService,
  type AccountMoneyView,
  type BotDeployPrefill,
  type BudgetDeployReceipt,
  type DeployBotBody,
  type DeployBotView,
  type DeploymentBudgetPreview,
  type DeploySubmissionBody,
  type DeploySubmissionUncommitted,
  type RunAdmissionDecision,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent, SUBMISSION_KEY_RE } from './alpaca-deploy-workflow.component';
import { DEPLOY_TYPING_SETTLE_MS } from './deploy-money-step.component';
import {
  DEPLOY_VIEW,
  EMA_STRATEGY,
  LIVE_DEPLOY_VIEW,
  SHADOW_DEPLOY_VIEW,
  SMA_OVERRIDE_STRATEGY,
  VALIDATION_STRATEGY,
} from './alpaca-deploy-workflow.fixtures';
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { resourceTarget, withAccount } from '../../../fleet/resource-target';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import {
  fakePickerWorld,
  pickSymbol,
  symbolPicker,
} from '../../../shared/symbol-picker/testing/fake-picker-world';

const DEPLOY_TARGET = resourceTarget('alpaca', 'clrk_spec', {
  accountId: 'PA9',
  bindingGeneration: 3,
  routingEpoch: 4,
});
const FENCE = { bindingGeneration: DEPLOY_TARGET.bindingGeneration, routingEpoch: DEPLOY_TARGET.routingEpoch };
/** These specs are not about the typing settle (the symbol-scope and money
 * step specs run with the real one), so symbol and amount act on the next tick. */
const INSTANT_SETTLE = { provide: DEPLOY_TYPING_SETTLE_MS, useValue: 0 };

const BLOCKED_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'rsi_mean_reversion',
  label: 'RSI Mean Reversion',
  explanation: 'Validated RSI mean-reversion entries with ADX confirmation.',
  validation_case_symbol: 'SPY',
  validation_case_parameters: {},
  golden_validation_scope: false,
  evidence_status: 'blocked',
  paper_access_state: 'enabled',
  selectable: false,
  admissible_modes: ['dry_run'],
  override_explanation: null,
  blocked_explanation: "The audit copy at 'docs/references/rsi-mean-reversion-signal.md' no longer matches its recorded hash.",
};
const BLOCKED_EXPLANATION = BLOCKED_STRATEGY.blocked_explanation ?? '';

// #1703: a validated strategy with no registered runtime admits neither mode.
const NO_RUNTIME_STRATEGY: DeployBotView['strategies'][number] = {
  ...BLOCKED_STRATEGY,
  strategy_key: 'spy_strategy_b',
  label: 'Strategy B',
  explanation: 'Validated RSI-range strategy with no registered runtime yet.',
  admissible_modes: [],
  blocked_explanation: 'This strategy has no registered live-decision runtime yet.',
};

const GOLDEN_TSLA_STRATEGY: DeployBotView['strategies'][number] = {
  ...EMA_STRATEGY,
  validation_case_symbol: 'TSLA',
  validation_case_parameters: { gap: 0.75 },
  golden_validation_scope: true,
};

const ADMISSION: RunAdmissionDecision = {
  operation: 'START',
  allowed: true,
  reason_code: 'START_ADMITTED',
  explanation: 'The process slot is absent, market data is ready, and the Clerk proves flat custody.',
  next_step: null,
  strategy_instance_id: 'spy-dv-20260929-0931',
  proposed_run_id: 'run-1',
  configuration_hash: 'a'.repeat(64),
  account_id: 'PA9',
  evaluated_at_ms: 1_700_000_000_000,
  fact_ages_ms: { runtime: 5, process: 10, market_data: 20, market_liveness: 20, clerk: 30, program_build: 15 },
  evidence_refs: ['test-admission'],
};

const RECEIPT: BudgetDeployReceipt = {
  status: 'deployed',
  outcome: 'success',
  receipt_id: 'command-spy-dv-20260929-0931',
  command_id: 'command-1',
  recorded_at_ms: 1_700_000_000_000,
  first_deployed_at_ms: 1_700_000_000_000,
  strategy_instance_id: 'spy-dv-20260929-0931',
  run_id: 'run-1',
  account_id: 'PA9',
  world: 'real_paper',
  committed_usd: '1000.00',
  message: 'spy-dv-20260929-0931 is deployed',
  explanation: '$1000.00 is set aside for it.',
  next_action: 'Open the bot’s page to watch it trade.',
  replaces_strategy_instance_id: null,
};

const BOT_NAME_NOTE = 'Named at Deploy: spy-dv-YYYYMMDD-HHMM, from the symbol, strategy code and New York minute.';

function worldOf(mode: DeployBotBody['execution_mode']): DeploymentBudgetPreview['world'] {
  if (mode === 'dry_run') return 'synthetic';
  if (mode === 'shadow') return 'shadow';
  if (mode === 'live') return 'real_live';
  return 'real_paper';
}

/** The account with a NEW slice for `amount`, as the backend draws it. */
function moneyWithNew(amount: string): AccountMoneyView {
  const now = fakeAccountMoney();
  return fakeAccountMoney({
    segments: [
      ...(now.segments ?? []).filter((segment) => segment.kind !== 'free'),
      { kind: 'new', label: 'new bot', amount_usd: amount, share_bps: 100 },
      { kind: 'free', label: 'free to deploy', amount_usd: '97329.57', share_bps: 9732 },
    ],
  });
}

/** A budget preview exactly as the backend would author one for `body`. */
function previewFor(view: DeployBotView, body: DeployBotBody): DeploymentBudgetPreview {
  const world = worldOf(body.execution_mode);
  const dryRun = world === 'synthetic';
  const amount = body.budget?.amount_usd ?? null;
  return {
    state: 'ready',
    detail: 'This is an entry-admission budget. Market fills and losses can exceed it.',
    world,
    custody_account_id: dryRun ? null : view.account_id,
    risk_revision: dryRun ? 0 : 1,
    minimum_budget_usd: '500.00',
    unreserved_usd: dryRun ? null : '98329.57',
    estimated_price_usd: '500.00',
    shortcuts: [{
      key: 'position_headroom', label: '1.2 × one position', amount_usd: '600.00',
      explanation: '1.2 × the $500.00 estimated position, including modelled fees. Market fills can cost more.',
    }],
    bot_name_note: BOT_NAME_NOTE,
    risk_limits_summary: dryRun
      ? 'Private simulated starting cash. Real-account daily loss limits and holds do not apply.'
      : 'Daily loss limit: the smaller of 2% of prior-close equity and $500.00. Existing exit terms stay fixed.',
    money_after: dryRun ? null : amount === null ? fakeAccountMoney() : moneyWithNew(amount),
    review_token: amount === null ? null : `review-${amount}`,
    budget_usd: amount,
    confirmation_text: amount !== null && world === 'real_live' ? `DEPLOY ${view.account_id} $${amount}` : null,
  };
}

function mockService(view: DeployBotView = DEPLOY_VIEW, result: BudgetDeployReceipt | HttpErrorResponse = RECEIPT) {
  return {
    accountId: view.account_id,
    getDeployView: vi.fn().mockResolvedValue(view),
    previewStartAdmission: vi.fn().mockResolvedValue(ADMISSION),
    previewBudget: vi.fn().mockImplementation(async (_target, body: DeployBotBody) => previewFor(view, body)),
    getDeploySubmission: vi.fn().mockRejectedValue(new HttpErrorResponse({
      status: 404,
      error: { detail: 'No Deploy was committed for this submission. Nothing was set aside and nothing started.' },
    })),
    getDeployPrefill: vi.fn<(target: unknown, sid: string) => Promise<BotDeployPrefill>>(),
    deployBudgetBot: result instanceof HttpErrorResponse
      ? vi.fn().mockRejectedValue(result)
      : vi.fn().mockResolvedValue({ ...result, account_id: view.account_id }),
  };
}
type ServiceDouble = ReturnType<typeof mockService>;

async function renderWorkflow(service: ServiceDouble = mockService()) {
  const rendered = await render(AlpacaDeployWorkflowComponent, {
    providers: [
      ...fakePickerWorld().providers,
      provideFleetDirectory(),
      INSTANT_SETTLE,
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: service },
    ],
    componentInputs: { fence: FENCE, target: withAccount(DEPLOY_TARGET, service.accountId), accountId: service.accountId },
  });
  await formShown();
  await rendered.fixture.whenStable();
  return rendered;
}

async function renderWithQuery(service: ServiceDouble, query: Record<string, string>) {
  const initial = convertToParamMap(query);
  const queryParamMap = new BehaviorSubject(initial);
  const rendered = await render(AlpacaDeployWorkflowComponent, {
    providers: [
      ...fakePickerWorld().providers,
      provideFleetDirectory(),
      INSTANT_SETTLE,
      provideRouter([]),
      { provide: ActivatedRoute, useValue: { queryParamMap, snapshot: { queryParamMap: initial } } },
      { provide: BrokerV2PanelService, useValue: service },
    ],
    componentInputs: { fence: FENCE, target: withAccount(DEPLOY_TARGET, service.accountId), accountId: service.accountId },
  });
  return { ...rendered, queryParamMap };
}

/** The page opened at `query`, as a reload of that address opens it. Its
 * router double merges every rewrite into `url.query`: the address a later
 * reload would open. */
async function openAt(service: ServiceDouble, query: Record<string, string>) {
  const rendered = await renderWithQuery(service, query);
  const url = { query: { ...query } };
  vi.spyOn(rendered.fixture.debugElement.injector.get(Router), 'navigate').mockImplementation(async (_commands, extras) => {
    const merged: Record<string, unknown> = { ...url.query, ...extras?.queryParams };
    const next = Object.fromEntries(
      Object.entries(merged).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
    );
    arriveAt({ url, queryParamMap: rendered.queryParamMap }, next);
    return true;
  });
  return { ...rendered, url };
}

/** A link into the page at `query` — the header's Deploy a bot is `{}`. */
function arriveAt(
  page: { url: { query: Record<string, string> }; queryParamMap: BehaviorSubject<ParamMap> },
  query: Record<string, string>,
): void {
  page.url.query = { ...query };
  page.queryParamMap.next(convertToParamMap(query));
}

/** A full reload: a fresh root injector, so no session draft, at `query`. */
async function reloadAt(service: ServiceDouble, query: Record<string, string>) {
  TestBed.resetTestingModule();
  return openAt(service, query);
}

/** Type a dollar amount into Money and wait for its preview to answer. */
async function chooseMoney(amount = '1000.00'): Promise<void> {
  fireEvent.input(screen.getByLabelText(/\(USD\)$/), { target: { value: amount } });
  await screen.findByText(/would be set aside for this bot|of simulated starting cash for this bot/, {}, { timeout: 3000 });
}

// `formShown`, `deployButton`, `openStep` and `stepRegion` find the form's
// parts by their words and structure, not by role and name. A named role query
// computes the accessible name of every candidate on the page through jsdom's
// slow getComputedStyle, and these helpers run hundreds of times, many inside
// `vi.waitFor` polls (#2592). The first test pins that each part they find is
// the one assistive technology finds by role and name.

/** Waits for the form's four steps to render. */
function formShown(): Promise<HTMLElement> {
  return screen.findByText('What', { selector: 'h2' });
}

function deployButton(): HTMLButtonElement {
  return within(stepRegion('Confirm')).getByText<HTMLButtonElement>(/^Deploy/, { selector: 'button' });
}

/** The recovery read's answers for a claimed key that has not committed. */
const IN_FLIGHT: DeploySubmissionUncommitted = {
  status: 'in_flight',
  submission_key: 'claimed-submission-1',
  strategy_instance_id: 'spy-dv-20260929-0931',
  claimed_at_ms: 1_700_000_000_000,
  message: 'spy-dv-20260929-0931 is being deployed now',
  explanation: 'Nothing is committed for it yet.',
  next_action: 'Check again in a moment; checking never starts a second bot.',
};
const NOT_COMMITTED: DeploySubmissionUncommitted = {
  ...IN_FLIGHT,
  status: 'not_committed',
  message: 'spy-dv-20260929-0931 was not deployed',
  explanation: 'Its Deploy never committed, so nothing was set aside for it.',
  next_action: 'Deploy again when ready; the bot is named from the minute you do.',
};

/** A $1,000 Deploy whose outcome is not settled, then its amount changed to $1,200. */
async function lostThenEdited(service: ServiceDouble) {
  const rendered = await renderWorkflow(service);
  await chooseMoney('1000.00');
  fireEvent.click(deployButton());
  await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(1));
  await screen.findByRole('alert', { name: 'Outcome unknown' });
  await chooseMoney('1200.00');
  await rendered.fixture.whenStable();
  return rendered;
}

/** A $1,000 Deploy whose answer was lost, on a page opened with no query. */
async function lostOnAPage(service: ServiceDouble) {
  const page = await openAt(service, {});
  await formShown();
  await page.fixture.whenStable();
  await chooseMoney('1000.00');
  fireEvent.click(deployButton());
  await screen.findByRole('alert', { name: 'Outcome unknown' });
  return { page, sent: submittedBody(service).submission_key };
}

function submittedBody(service: ServiceDouble, call = 0): DeploySubmissionBody {
  return service.deployBudgetBot.mock.calls[call][1] as DeploySubmissionBody;
}

async function openStep(name: 'What' | 'How'): Promise<void> {
  const edit = within(stepRegion(name)).queryByLabelText(new RegExp(`^Edit step \\d, ${name}$`), { selector: 'button' });
  if (edit !== null) fireEvent.click(edit);
  await vi.waitFor(() => expect(within(stepRegion(name)).getByLabelText(/^Done/, { selector: 'button' })).toBeTruthy());
}

/** A step: the section its `h2` heading labels. */
function stepRegion(name: string): HTMLElement {
  const heading = screen.getByText(name, { selector: 'h2' });
  const section = heading.closest<HTMLElement>(`section[aria-labelledby="${heading.id}"]`);
  if (section === null) throw new Error(`No section is labelled by the step heading ${name}.`);
  return section;
}

/** A window wide enough for the steps to stand side by side. The test setup's
 * stub answers every media query `false`: a phone, where the steps stack. */
function roomyViewport(): void {
  const spy = vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }));
  onTestFinished(() => spy.mockRestore());
}

describe('AlpacaDeployWorkflowComponent — four steps (PRD #2560 D8)', () => {
  it('lays out What → How → Money → Confirm and folds complete steps to one line with Edit', async () => {
    await renderWorkflow();

    const steps = screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent);
    expect(steps).toEqual(['What', 'How', 'Money', 'Confirm']);
    for (const name of ['What', 'How', 'Money', 'Confirm']) {
      expect(screen.getByRole('region', { name })).toBe(stepRegion(name));
    }
    expect(screen.getByRole('button', { name: /^Deploy/ })).toBe(deployButton());

    const what = stepRegion('What');
    expect(within(what).getByText('Deployment Validation on SPY · Allowed on this Paper account')).toBeTruthy();
    expect(within(what).getByRole('button', { name: 'Edit step 1, What' }).getAttribute('aria-expanded')).toBe('false');
    const how = stepRegion('How');
    expect(within(how).getByText(
      'Paper · 1 share · Exit allowance 20 bps, band 2×, spread cap 50 bps · fixed for this bot’s life',
    )).toBeTruthy();

    // The readiness rail and its heading are gone (H8); the name is the backend's.
    expect(screen.queryByText(/Live admission/i)).toBeNull();
    expect(screen.queryByLabelText('Bot name')).toBeNull();
    expect(screen.queryByRole('link', { name: 'Open account recovery' })).toBeNull();
  });

  it('stands the steps side by side where the page has room: every step open, none folding, each headed by its state', async () => {
    roomyViewport();
    await renderWorkflow(mockService({ ...DEPLOY_VIEW, default_exit_terms: null }));

    for (const name of ['What', 'How']) {
      expect(within(stepRegion(name)).queryByRole('button', { name: /^(Edit|Done with) step/ })).toBeNull();
    }
    expect(within(stepRegion('What')).getByRole('combobox', { name: 'Deployment strategy' })).toBeTruthy();
    expect(within(stepRegion('How')).getByRole('spinbutton', { name: 'Exit allowance (bps)' })).toBeTruthy();
    expect(within(stepRegion('What')).getByText('Ready')).toBeTruthy();
    expect(within(stepRegion('How')).getByText('Needs exit terms')).toBeTruthy();
    expect(within(stepRegion('Money')).queryByText('Reviewed')).toBeNull();

    fireEvent.input(within(stepRegion('How')).getByRole('spinbutton', { name: 'Exit allowance (bps)' }), { target: { value: '20' } });
    fireEvent.input(within(stepRegion('How')).getByRole('spinbutton', { name: 'Band multiple' }), { target: { value: '2' } });
    fireEvent.input(within(stepRegion('How')).getByRole('spinbutton', { name: 'Spread cap (bps)' }), { target: { value: '50' } });
    await chooseMoney();

    expect(within(stepRegion('How')).getByText('Ready')).toBeTruthy();
    expect(within(stepRegion('Money')).getByText('Reviewed')).toBeTruthy();
    // Complete, and still open beside the others: nothing folds on a wide page.
    expect(within(stepRegion('How')).getByRole('spinbutton', { name: 'Band multiple' })).toBeTruthy();
  });

  it('opens a folded step into focus on Edit and returns focus to Edit on Done', async () => {
    const { fixture } = await renderWorkflow();

    fireEvent.click(screen.getByRole('button', { name: 'Edit step 1, What' }));
    await fixture.whenStable();

    const done = within(stepRegion('What')).getByRole('button', { name: 'Done with step 1, What' });
    expect(done.getAttribute('aria-expanded')).toBe('true');
    await vi.waitFor(() => expect(document.activeElement?.id).toBe(done.getAttribute('aria-controls')));
    expect(screen.getByLabelText('Deployment strategy')).toBeTruthy();

    fireEvent.click(done);
    await fixture.whenStable();
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Edit step 1, What' })));
  });

  it('keeps a step the owner is working in open until they press Done', async () => {
    const { fixture } = await renderWorkflow(mockService(LIVE_DEPLOY_VIEW));
    const how = stepRegion('How');
    const done = within(how).getByRole<HTMLButtonElement>('button', { name: 'Done with step 2, How' });
    expect(done.disabled).toBe(true);

    fireEvent.click(within(how).getByRole('radio', { name: /Dry Run/ }));
    await fixture.whenStable();

    // Complete now, and still open: it never folds from under the keyboard.
    expect(within(how).getByRole('radio', { name: /Dry Run/ })).toBeTruthy();
    expect(done.disabled).toBe(false);
    fireEvent.click(done);
    await fixture.whenStable();
    expect(within(stepRegion('How')).getByText(/^Dry Run · 1 share/)).toBeTruthy();
  });

  it('grows the NEW slice on the account bar only after the typed amount’s preview answers', async () => {
    const service = mockService();
    let answer!: (preview: DeploymentBudgetPreview) => void;
    service.previewBudget.mockImplementation((_target, body: DeployBotBody) =>
      body.budget
        ? new Promise<DeploymentBudgetPreview>((resolve) => { answer = resolve; })
        : Promise.resolve(previewFor(DEPLOY_VIEW, body)));
    await renderWorkflow(service);
    const money = stepRegion('Money');
    await within(money).findByRole('list', { name: 'Where the money is now' });

    fireEvent.input(within(money).getByLabelText('Dollar budget (USD)'), { target: { value: '1000.00' } });
    await vi.waitFor(() => expect(service.previewBudget.mock.calls.some(([, body]) => body.budget)).toBe(true));
    expect(within(money).queryByText('new bot')).toBeNull();

    answer(previewFor(DEPLOY_VIEW, { ...service.previewBudget.mock.calls.at(-1)?.[1], budget: { amount_usd: '1000.00', risk_revision: 1 } }));
    const after = await within(money).findByRole('list', { name: 'Account money after this Deploy' });
    expect(within(after).getByText('new bot')).toBeTruthy();
    expect(within(after).getByText('$1,000.00')).toBeTruthy();
  });

  it('asks for no typed phrase on Paper, and names the world and the amount on the button', async () => {
    await renderWorkflow();

    expect(deployButton().disabled).toBe(true);
    await chooseMoney();

    expect(screen.queryByLabelText(/type/i)).toBeNull();
    expect(deployButton().textContent?.trim()).toBe('Deploy paper bot · set aside $1,000.00');
    expect(deployButton().disabled).toBe(false);
  });

  it('gates Live on the exact typed phrase, and asks for it again when the amount changes', async () => {
    const service = mockService(LIVE_DEPLOY_VIEW);
    const { fixture } = await renderWorkflow(service);
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Live/ }));
    await fixture.whenStable();
    await chooseMoney('800.00');

    const phrase = screen.getByLabelText(/To deploy with real money, type/);
    expect(screen.getByText('DEPLOY 9LIVE0001 $800.00')).toBeTruthy();
    expect(deployButton().disabled).toBe(true);
    fireEvent.input(phrase, { target: { value: 'DEPLOY 9LIVE0001 $800.00' } });
    await fixture.whenStable();
    expect(deployButton().textContent?.trim()).toBe('Deploy live bot · set aside $800.00');
    expect(deployButton().disabled).toBe(false);

    await chooseMoney('900.00');
    expect((screen.getByLabelText(/To deploy with real money, type/) as HTMLInputElement).value).toBe('');
    expect(deployButton().disabled).toBe(true);

    fireEvent.input(screen.getByLabelText(/To deploy with real money, type/), { target: { value: 'DEPLOY 9LIVE0001 $900.00' } });
    await fixture.whenStable();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).budget).toEqual({
      amount_usd: '900.00', risk_revision: 1, review_token: 'review-900.00', live_confirmation: 'DEPLOY 9LIVE0001 $900.00',
    });
    expect(submittedBody(service).execution_mode).toBe('live');
  });

  it('never preselects Live: the owner chooses Dry Run or Live (H17)', async () => {
    await renderWorkflow(mockService(LIVE_DEPLOY_VIEW));

    const how = stepRegion('How');
    expect(within(how).getByRole<HTMLInputElement>('radio', { name: /Live/ }).checked).toBe(false);
    expect(within(how).getByRole<HTMLInputElement>('radio', { name: /Dry Run/ }).checked).toBe(false);
    expect(deployButton().textContent?.trim()).toBe('Deploy bot');
    expect(screen.getByText('Choose where this bot trades in How.')).toBeTruthy();
  });

  it('keeps a strategy not yet allowed on this account open in What, with Allow one click away (D9, story 52)', async () => {
    const notYetAllowed: DeployBotView['strategies'][number] = {
      ...VALIDATION_STRATEGY,
      paper_access_state: 'available',
      selectable: false,
      admissible_modes: ['dry_run'],
      blocked_explanation: 'Broker trading is not enabled for this strategy on this account yet. Allow it in What.',
    };
    const { fixture } = await renderWorkflow(mockService({ ...DEPLOY_VIEW, strategies: [notYetAllowed] }));

    const what = stepRegion('What');
    expect(within(what).getByRole('button', { name: 'Allow on Paper' })).toBeTruthy();
    const done = within(what).getByRole<HTMLButtonElement>('button', { name: 'Done with step 1, What' });
    expect(done.disabled).toBe(true);
    // The chip beside the heading says what What still needs, and Done is described by it.
    const chip = within(what).getByText('Needs permission');
    expect(done.getAttribute('aria-describedby')).toBe(chip.id);
    expect(screen.getByText('Allow this strategy on Paper in What, or choose Dry Run in How.')).toBeTruthy();

    // Dry Run needs no permission, so What may fold once it is chosen.
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Dry Run/ }));
    await fixture.whenStable();
    expect(done.disabled).toBe(false);
  });

  it('shows no “Can’t deploy yet” while every check passes, and opens every check in a popover', async () => {
    await renderWorkflow();

    expect(screen.queryByText(/Can’t deploy yet/)).toBeNull();
    const open = screen.getByRole('button', { name: 'Every check and the last Start check' });
    const checks = document.getElementById(open.getAttribute('popovertarget') ?? '');
    expect(checks?.hasAttribute('popover')).toBe(true);
    expect(within(checks as HTMLElement).getByRole('heading', { name: 'Every check and the last Start check' })).toBeTruthy();
    expect(within(checks as HTMLElement).getByText('Strategy validation')).toBeTruthy();
  });

  it('lists each failing check with its fix', async () => {
    const blockedView: DeployBotView = {
      ...DEPLOY_VIEW,
      eligibility: { ...DEPLOY_VIEW.eligibility, eligible: false, next_action: 'Restore the market-data channel.' },
      readiness_checks: DEPLOY_VIEW.readiness_checks.map((check) =>
        check.gate_id === 'broker.channel'
          ? { ...check, ready: false, headline: 'Market data is unhealthy.', recovery: 'Restore the market-data channel.' }
          : check),
    };
    await renderWorkflow(mockService(blockedView));

    const blockers = screen.getByRole('region', { name: 'Can’t deploy yet · 1 thing' });
    expect(within(blockers).getByText('Broker channel.')).toBeTruthy();
    expect(within(blockers).getByText('Fix: Restore the market-data channel.')).toBeTruthy();
    expect(within(blockers).queryByText('Strategy validation.')).toBeNull();
  });

  it('judges Dry Run by its own verdict, never by the broker checks', async () => {
    const view: DeployBotView = {
      ...DEPLOY_VIEW,
      eligibility: { ...DEPLOY_VIEW.eligibility, eligible: false },
      readiness_checks: DEPLOY_VIEW.readiness_checks.map((check) => ({ ...check, ready: false })),
    };
    const { fixture } = await renderWorkflow(mockService(view));
    await openStep('How');
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Dry Run/ }));
    await fixture.whenStable();

    expect(screen.queryByText(/Can’t deploy yet/)).toBeNull();
    await chooseMoney();
    expect(deployButton().textContent?.trim()).toBe('Deploy Dry Run bot · $1,000.00 of simulated cash');
    expect(deployButton().disabled).toBe(false);
  });

  it('words Dry Run money as simulated cash and never shows the real account number (H18)', async () => {
    const { fixture } = await renderWorkflow(mockService(LIVE_DEPLOY_VIEW));
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Dry Run/ }));
    await fixture.whenStable();
    await chooseMoney();

    expect(screen.getByLabelText('Simulated starting cash (USD)')).toBeTruthy();
    expect(screen.queryByText('Free to deploy')).toBeNull();
    expect(screen.queryByText(/Unreserved cash/)).toBeNull();
    expect(screen.queryByText(/9LIVE0001/)).toBeNull();
    const account = within(screen.getByLabelText('Deploy review')).getByText('Account').nextElementSibling;
    expect(account?.textContent?.trim()).toBe('The bot’s own account · DRY RUN · simulated cash');
  });

  it('pre-fills exit terms from the account’s defaults, and points at Settings when it has none (H3)', async () => {
    await renderWorkflow(mockService({ ...DEPLOY_VIEW, default_exit_terms: null }));

    const how = stepRegion('How');
    expect(within(how).getByRole('button', { name: 'Done with step 2, How' })).toBeTruthy();
    expect(within(how).getByRole<HTMLInputElement>('spinbutton', { name: 'Exit allowance (bps)' }).value).toBe('');
    expect(within(how).getByRole('link', { name: 'Set defaults for new bots in Settings' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/settings');
    expect(screen.getByText('Set this bot’s exit allowance, band multiple and spread cap in How.')).toBeTruthy();
  });

  it('reviews the name the backend will author, the trade, the account, the exits, the budget and the loss limit', async () => {
    await renderWorkflow();
    // Before a preview the name is only promised, in the owner's words.
    expect(within(screen.getByLabelText('Deploy review')).getByText('Bot').nextElementSibling?.textContent)
      .toBe('Named when you deploy.');
    await chooseMoney();

    const text = screen.getByLabelText('Deploy review').textContent ?? '';
    expect(text).toContain(BOT_NAME_NOTE);
    expect(text).toContain('Deployment Validation · SPY · 1 share');
    expect(text).toContain('PA9 · PAPER · practice money');
    expect(text).toContain('Exit allowance 20 bps');
    expect(text).toContain('$1,000.00');
    expect(text).toContain('Daily loss limit: the smaller of 2% of prior-close equity and $500.00.');
  });

  it('warns beside the trade, in the backend’s words, when another bot in this account already trades the symbol (#2622)', async () => {
    const note = 'Also traded here by spy-ema-20260929-0931.';
    const service = mockService();
    service.previewBudget.mockImplementation(async (_target, body: DeployBotBody) => ({
      ...previewFor(DEPLOY_VIEW, body), same_symbol_note: note,
    }));
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();
    await fixture.whenStable();

    const warning = within(screen.getByLabelText('Deploy review')).getByText(note, { exact: false });
    expect(warning.closest('div')?.querySelector('dt')?.textContent).toBe('Trades');
    // A warning, never a block: the Deploy stays open to the owner.
    expect(deployButton().disabled).toBe(false);
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('shows no same-symbol warning when no other bot trades the symbol', async () => {
    await renderWorkflow();
    await chooseMoney();

    expect(screen.getByLabelText('Deploy review').textContent).not.toContain('Also traded here');
  });
});

describe('AlpacaDeployWorkflowComponent — submission (#2551)', () => {
  it('sends an opaque submission key and never a bot name, and names the key in the URL before sending', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    const router = fixture.debugElement.injector.get(Router);
    await chooseMoney();

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());

    const body = submittedBody(service);
    expect(body).toEqual({
      budget: { amount_usd: '1000.00', risk_revision: 1, review_token: 'review-1000.00', live_confirmation: null },
      exit_terms: DEPLOY_VIEW.default_exit_terms,
      strategy_key: 'deployment_validation',
      symbol: 'SPY',
      sizing: { preset: 'safe_canary', quantity: 1 },
      execution_mode: 'paper',
      carryover_policy: 'FORBID',
      parameters: {},
      submission_key: expect.stringMatching(SUBMISSION_KEY_RE),
    });
    expect(body).not.toHaveProperty('strategy_instance_id');
    expect(router.url).toContain(`submission=${body.submission_key}`);
    // The Start plan judges the settings alone.
    expect(service.previewStartAdmission.mock.calls[0][1]).not.toHaveProperty('submission_key');
  });

  it('retries an uncertain Deploy under the same submission key and command', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('alert', { name: 'Outcome unknown' })).toBeTruthy();
    expect(screen.getByText('Checking its status, or pressing Deploy again, never starts a second bot.')).toBeTruthy();
    await fixture.whenStable();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));

    const [firstTarget, first] = service.deployBudgetBot.mock.calls[0];
    const [secondTarget, second] = service.deployBudgetBot.mock.calls[1];
    expect(second.submission_key).toBe(first.submission_key);
    expect(secondTarget).toEqual(firstTarget);
    expect(firstTarget).toMatchObject({ clerkId: 'clrk_spec', accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
  });

  it('mints a new submission key after a refusal the backend marks as settling it', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({
      status: 409,
      error: { detail: {
        outcome: 'conflict', message: 'Deployment budget is unavailable.', why: 'The budget is more than this account can set aside.',
        next_action: 'Review the current budget and account evidence, then retry.', admission: null, reason_code: null,
        submission_settled: true,
      } },
    }));
    const { fixture } = await renderWorkflow(service);
    await chooseMoney('1000.00');
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(1));
    await screen.findByText('The budget is more than this account can set aside.');
    expect(screen.queryByRole('button', { name: 'Check deployment status' })).toBeNull();

    await chooseMoney('1200.00');
    await fixture.whenStable();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));

    expect(submittedBody(service, 1).submission_key).not.toBe(submittedBody(service, 0).submission_key);
    expect(service.deployBudgetBot.mock.calls[1][0].idempotencyKey)
      .not.toBe(service.deployBudgetBot.mock.calls[0][0].idempotencyKey);
    expect(service.getDeploySubmission).not.toHaveBeenCalled();
  });

  it('titles a bot that cannot be named as a refused Deploy, not an account change', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({
      status: 409,
      error: { detail: {
        outcome: 'conflict', receipt_id: null, recorded_at_ms: 1_700_000_000_002, message: 'This bot cannot be named.',
        why: 'More than 99 SPY bots of this strategy were deployed this minute.', next_action: 'Try again in a minute.',
        admission: null, reason_code: 'deploy_bot_name_unavailable', submission_settled: true,
      } },
    }));
    await renderWorkflow(service);
    await chooseMoney();
    fireEvent.click(deployButton());

    const refusal = await screen.findByRole('alert', { name: 'Deploy refused' });
    expect(within(refusal).getByText('This bot cannot be named.')).toBeTruthy();
    expect(within(refusal).getByText('More than 99 SPY bots of this strategy were deployed this minute.')).toBeTruthy();
    expect(within(refusal).getByText('Next: Try again in a minute.')).toBeTruthy();
    expect(within(refusal).queryByRole('button', { name: 'Check deployment status' })).toBeNull();
  });

  // Each of these is a refusal the backend leaves unsettled: the first bot
  // may have started, or the key's earlier Deploy could not be read. The
  // page keeps the key, says the outcome is unknown in the backend's words,
  // and offers the status read (B2-3).
  it.each([
    {
      kind: 'the outcome-not-readable 409',
      status: 409,
      detail: {
        outcome: 'conflict', message: 'Deployment budget is unavailable.',
        why: 'Deployment outcome is not yet readable. Recover this command before trying again.',
        next_action: 'Review the current budget and account evidence, then retry.', admission: null, reason_code: null,
        submission_settled: false,
      },
    },
    {
      kind: 'the custody-read 409',
      status: 409,
      detail: {
        outcome: 'conflict', message: 'Deployment budget is unavailable.',
        why: 'Deployment command evidence is unavailable. Resolve custody recovery before continuing.',
        next_action: 'Review the current budget and account evidence, then retry.', admission: null, reason_code: null,
        submission_settled: false,
      },
    },
    {
      kind: 'the runner’s 503',
      status: 503,
      detail: {
        outcome: 'blocked', message: 'The bot runner is not available.',
        why: 'The service is still starting or has shut down.',
        next_action: 'Wait for the data plane to become healthy, then refresh.', admission: null, reason_code: null,
        submission_settled: false,
      },
    },
  ])('keeps the submission key after $kind, even when the amount changes', async ({ status, detail }) => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status, error: { detail } }));
    await lostThenEdited(service);

    const refusal = screen.getByRole('alert', { name: 'Outcome unknown' });
    expect(within(refusal).getByText(detail.message)).toBeTruthy();
    expect(within(refusal).getByText(detail.why)).toBeTruthy();
    expect(within(refusal).getByText(`Next: ${detail.next_action}`)).toBeTruthy();
    expect(within(refusal).getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));
    expect(submittedBody(service, 1).submission_key).toBe(submittedBody(service, 0).submission_key);
  });

  it('keeps the submission key of a Deploy still being sent when the owner leaves and returns', async () => {
    const service = mockService();
    service.deployBudgetBot.mockReturnValueOnce(new Promise(() => undefined));
    const { fixture } = await render(DeployHostComponent, {
      providers: [
        ...fakePickerWorld().providers,
        provideFleetDirectory(),
        INSTANT_SETTLE,
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: service },
      ],
    });
    await formShown();
    await fixture.whenStable();
    await chooseMoney('1000.00');
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(1));

    fixture.componentInstance.shown.set(false);
    await fixture.whenStable();
    fixture.componentInstance.shown.set(true);
    await fixture.whenStable();
    await formShown();

    const refusal = await screen.findByRole('alert', { name: 'Outcome unknown' });
    expect(within(refusal).getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
    await chooseMoney('1200.00');
    await fixture.whenStable();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));
    expect(submittedBody(service, 1).submission_key).toBe(submittedBody(service, 0).submission_key);
  });

  it('keeps the submission key after a lost response, even when the amount changes (stories 63, 66)', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    await lostThenEdited(service);

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));

    expect(submittedBody(service, 1).submission_key).toBe(submittedBody(service, 0).submission_key);
    expect(submittedBody(service, 1).budget?.amount_usd).toBe('1200.00');
    expect(service.getDeploySubmission).not.toHaveBeenCalled();
  });

  it.each([
    {
      answer: 'This Deploy is already being sent.',
      detail: {
        outcome: 'conflict', message: 'This Deploy is already being sent.', why: 'A second copy of it was not started.',
        next_action: 'Check its status in a moment; checking never starts a second bot.', reason_code: 'deploy_submission_in_flight',
      },
    },
    {
      answer: 'This Deploy was already sent with other settings.',
      detail: {
        outcome: 'conflict', message: 'This Deploy was already sent with other settings.',
        why: 'This Deploy was already sent with different settings as spy-dv-20260929-0931. Nothing new was set aside or started.',
        next_action: 'Check its status; checking never starts a second bot.', reason_code: 'deploy_submission_settings_conflict',
      },
    },
  ])('keeps the submission key when Deploy answers “$answer”', async ({ detail }) => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 409, error: { detail } }));
    await lostThenEdited(service);

    const refusal = screen.getByRole('alert', { name: 'Outcome unknown' });
    expect(within(refusal).getByText(detail.message)).toBeTruthy();
    expect(within(refusal).getByText(detail.why)).toBeTruthy();
    expect(within(refusal).getByText(`Next: ${detail.next_action}`)).toBeTruthy();
    expect(within(refusal).getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));
    expect(submittedBody(service, 1).submission_key).toBe(submittedBody(service, 0).submission_key);
  });

  it('shows the receipt when the status read finds the lost Deploy committed, and sends nothing again', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    service.getDeploySubmission.mockResolvedValue({ ...RECEIPT, account_id: 'PA9' });
    const { fixture } = await lostThenEdited(service);

    fireEvent.click(screen.getByRole('button', { name: 'Check deployment status' }));
    const receipt = await screen.findByRole('status', { name: RECEIPT.message });
    await fixture.whenStable();

    expect(service.getDeploySubmission.mock.calls[0][1]).toBe(submittedBody(service, 0).submission_key);
    await vi.waitFor(() => expect(document.activeElement).toBe(receipt));
    expect(service.deployBudgetBot).toHaveBeenCalledOnce();
  });

  it.each([
    {
      kind: 'in_flight',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockResolvedValue(IN_FLIGHT),
      title: 'Still being deployed',
      shows: [IN_FLIGHT.message, IN_FLIGHT.explanation, `Next: ${IN_FLIGHT.next_action}`],
      keepsKey: true,
    },
    {
      kind: 'not_committed',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockResolvedValue(NOT_COMMITTED),
      title: 'Not deployed',
      shows: [NOT_COMMITTED.message, NOT_COMMITTED.explanation, `Next: ${NOT_COMMITTED.next_action}`],
      keepsKey: false,
    },
    {
      kind: 'no record (404)',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockRejectedValue(new HttpErrorResponse({
        status: 404,
        error: { detail: 'No Deploy was committed for this submission. Nothing was set aside and nothing started.' },
      })),
      title: 'Not deployed',
      shows: ['No Deploy was committed for this submission. Nothing was set aside and nothing started.'],
      keepsKey: false,
    },
    {
      kind: 'no bot runner (503)',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockRejectedValue(new HttpErrorResponse({
        status: 503,
        error: { detail: { outcome: 'blocked', message: 'The bot runner is not available.', why: 'The service is still starting or has shut down.' } },
      })),
      title: 'Outcome unknown',
      shows: ['The bot runner is not available.', 'Whether the bot started is still not known; checking never starts a second bot.'],
      keepsKey: true,
    },
  ])('after a lost response, a $kind status read decides whether the next Deploy keeps its key', async ({ read, title, shows, keepsKey }) => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    read(service);
    const { fixture } = await lostThenEdited(service);

    fireEvent.click(screen.getByRole('button', { name: 'Check deployment status' }));
    const refusal = await screen.findByRole('alert', { name: title });
    for (const line of shows) expect(within(refusal).getByText(line)).toBeTruthy();
    expect(within(refusal).queryByRole('button', { name: 'Check deployment status' }) !== null).toBe(keepsKey);

    await fixture.whenStable();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));
    expect(submittedBody(service, 1).submission_key === submittedBody(service, 0).submission_key).toBe(keepsKey);
  });

  it('reads nothing back after its own Deploy succeeds', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();

    fireEvent.click(deployButton());
    await screen.findByRole('status', { name: RECEIPT.message });
    await fixture.whenStable();

    expect(service.getDeploySubmission).not.toHaveBeenCalled();
  });

  it('checks its own pending Deploy without sending it again', async () => {
    const pending: BudgetDeployReceipt = {
      ...RECEIPT, status: 'pending', outcome: 'pending',
      message: 'spy-dv-20260929-0931 is committed; its launch is not confirmed yet',
    };
    const service = mockService(DEPLOY_VIEW, pending);
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();
    fireEvent.click(deployButton());
    await screen.findByRole('heading', { name: pending.message });
    await fixture.whenStable();

    service.getDeploySubmission.mockResolvedValue({ ...RECEIPT, account_id: 'PA9' });
    fireEvent.click(screen.getByRole('button', { name: 'Check deployment status' }));

    await screen.findByRole('heading', { name: RECEIPT.message });
    expect(service.getDeploySubmission.mock.calls.at(-1)?.[1]).toBe(submittedBody(service).submission_key);
    expect(service.deployBudgetBot).toHaveBeenCalledOnce();
  });

  it('moves focus to the receipt, which names the bot, the money, the world and the bot’s page', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();

    fireEvent.click(deployButton());
    const receipt = await screen.findByRole('status', { name: RECEIPT.message });
    await fixture.whenStable();

    await vi.waitFor(() => expect(document.activeElement).toBe(receipt));
    expect(within(receipt).getByText('spy-dv-20260929-0931')).toBeTruthy();
    expect(within(receipt).getByText('$1,000.00')).toBeTruthy();
    expect(within(receipt).getByText('PAPER · practice money')).toBeTruthy();
    expect(within(receipt).getByRole('link', { name: 'Open spy-dv-20260929-0931' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/spy-dv-20260929-0931');
    expect(within(receipt).queryByText(/returned unchanged/)).toBeNull();
  });

  it('moves focus to the refusal when the backend declines the Deploy', async () => {
    const service = mockService();
    service.previewStartAdmission.mockResolvedValue({
      ...ADMISSION,
      allowed: false,
      reason_code: 'SIGNAL_PROGRAM_UNSEALED',
      explanation: 'Golden Validation evidence for this strategy could not be read.',
      next_step: 'Open Strategy Validation so the research store is brought up to date, then deploy again.',
    });
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();

    fireEvent.click(deployButton());
    const refusal = await screen.findByRole('alert', { name: 'Start refused' });
    await fixture.whenStable();

    await vi.waitFor(() => expect(document.activeElement).toBe(refusal));
    // H11: the real cause, verbatim — never "no v2 seal".
    expect(within(refusal).getByText('Golden Validation evidence for this strategy could not be read.')).toBeTruthy();
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  it('never words an unknown outcome in internal terms', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    await renderWorkflow(service);
    await chooseMoney();
    fireEvent.click(deployButton());

    const alert = await screen.findByRole('alert', { name: 'Outcome unknown' });
    expect(alert.textContent).not.toMatch(/control boundary|data.plane|SQLite|lens/i);
  });

  it('reads a pending submission after a reload without sending another Deploy', async () => {
    const service = mockService();
    const pending: BudgetDeployReceipt = {
      ...RECEIPT, status: 'pending', outcome: 'pending', command_id: 'original-command',
      message: 'spy-dv-20260929-0931 is committed; its launch is not confirmed yet',
    };
    service.getDeploySubmission.mockResolvedValue(pending);
    service.getDeployView.mockRejectedValue(new Error('Deploy view unavailable'));
    await renderWithQuery(service, { submission: 'reloaded-submission-1' });

    await screen.findByRole('heading', { name: pending.message });
    expect(service.getDeploySubmission.mock.calls[0][1]).toBe('reloaded-submission-1');
    expect(screen.getByText('Pending')).toBeTruthy();
    service.getDeploySubmission.mockResolvedValue({ ...pending, status: 'deployed', outcome: 'success', message: RECEIPT.message });
    fireEvent.click(screen.getByRole('button', { name: 'Check deployment status' }));
    await screen.findByRole('heading', { name: RECEIPT.message });
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  it.each([
    { answer: IN_FLIGHT, offersCheck: true },
    { answer: NOT_COMMITTED, offersCheck: false },
  ])('shows a reloaded $answer.status Deploy in the backend’s own words', async ({ answer, offersCheck }) => {
    const service = mockService();
    service.getDeploySubmission.mockResolvedValue(answer);
    await renderWithQuery(service, { submission: 'reloaded-submission-1' });

    const recovery = await screen.findByRole('status', { name: 'Checking the recorded deployment' });
    expect(await within(recovery).findByText(answer.message)).toBeTruthy();
    expect(within(recovery).getByText(answer.explanation)).toBeTruthy();
    expect(within(recovery).getByText(`Next: ${answer.next_action}`)).toBeTruthy();
    expect(within(recovery).queryByRole('button', { name: 'Check deployment status' }) !== null).toBe(offersCheck);
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  /** A reload onto `?submission=reloaded-submission-1`. */
  function reloadOnto(service: ServiceDouble) {
    return openAt(service, { submission: 'reloaded-submission-1' });
  }

  /** Deploy from the form. No `whenStable`: a read left unanswered keeps the
   * page's pending task open for good. */
  async function deployFromTheForm(service: ServiceDouble) {
    await formShown();
    await chooseMoney('1000.00');
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(1));
  }

  /** Leave a recovery read that shows `message` for the form. */
  async function prepareFromTheRecovery(message: string) {
    const recovery = await screen.findByRole('status', { name: 'Checking the recorded deployment' });
    await within(recovery).findByText(message);
    fireEvent.click(within(recovery).getByRole('button', { name: 'Prepare a new deployment' }));
    await screen.findByRole('alert', { name: 'Outcome unknown' });
  }

  /** A reload whose key reads `in_flight`, prepared from, then reloaded again. */
  async function reloadAfterPreparing() {
    const service = mockService();
    service.getDeploySubmission.mockResolvedValue(IN_FLIGHT);
    const { url } = await reloadOnto(service);
    await prepareFromTheRecovery(IN_FLIGHT.message);
    const reloaded = mockService();
    reloaded.getDeploySubmission.mockResolvedValue(IN_FLIGHT);
    await reloadAt(reloaded, url.query);
    return reloaded;
  }

  // B2-1 (final): the address is the only record of an unsettled key a
  // reload keeps, so no way back to the form drops it.
  it('sends the reloaded key after Prepare a new deployment and a second reload', async () => {
    const reloaded = await reloadAfterPreparing();

    await vi.waitFor(() => expect(reloaded.getDeploySubmission).toHaveBeenCalledWith(expect.anything(), 'reloaded-submission-1'));
    await prepareFromTheRecovery(IN_FLIGHT.message);
    await deployFromTheForm(reloaded);

    expect(submittedBody(reloaded).submission_key).toBe('reloaded-submission-1');
  });

  it('sends a lost Deploy’s key after the header’s Deploy a bot and a reload', async () => {
    const { page, sent } = await lostOnAPage(mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 })));
    arriveAt(page, {});
    await page.fixture.whenStable();
    await screen.findByRole('alert', { name: 'Outcome unknown' });

    const reloaded = mockService();
    reloaded.getDeploySubmission.mockResolvedValue(IN_FLIGHT);
    await reloadAt(reloaded, page.url.query);
    await vi.waitFor(() => expect(reloaded.getDeploySubmission).toHaveBeenCalledWith(expect.anything(), sent));
    await prepareFromTheRecovery(IN_FLIGHT.message);
    await deployFromTheForm(reloaded);

    expect(submittedBody(reloaded).submission_key).toBe(sent);
  });

  it('writes a lost Deploy’s key back into the address when the header’s Deploy a bot drops it', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    const { page, sent } = await lostOnAPage(service);

    arriveAt(page, {});

    await vi.waitFor(() => expect(page.url.query).toEqual({ submission: sent }));
    expect(within(await screen.findByRole('alert', { name: 'Outcome unknown' }))
      .getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
    expect(screen.queryByRole('status', { name: 'Checking the recorded deployment' })).toBeNull();
    expect(service.getDeploySubmission).not.toHaveBeenCalled();
  });

  it('sends the reloaded key after a second reload that kept it in the address', async () => {
    const service = mockService();
    service.getDeploySubmission.mockResolvedValue(IN_FLIGHT);
    await reloadOnto(service);
    await screen.findByRole('status', { name: 'Checking the recorded deployment' });
    const reloaded = mockService();
    reloaded.getDeploySubmission.mockResolvedValue(IN_FLIGHT);
    await reloadAt(reloaded, { submission: 'reloaded-submission-1' });

    await prepareFromTheRecovery(IN_FLIGHT.message);
    await deployFromTheForm(reloaded);

    expect(submittedBody(reloaded).submission_key).toBe('reloaded-submission-1');
  });

  // B2-1: the reload's key is the only memory of a Deploy the page may have
  // started. Until its read settles it, every way back to the form resends it.
  it.each([
    {
      kind: 'in_flight',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockResolvedValue(IN_FLIGHT),
      shows: IN_FLIGHT.message,
    },
    {
      kind: 'unreadable (503)',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockRejectedValue(new HttpErrorResponse({
        status: 503, error: { detail: { outcome: 'blocked', message: 'The bot runner is not available.' } },
      })),
      shows: 'The deployment result could not be read. Check again before preparing a fresh deployment.',
    },
    {
      kind: 'unanswered',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockReturnValue(new Promise(() => undefined)),
      shows: 'Checking never starts a second bot. The recorded result shows whether money was set aside and the bot started.',
    },
  ])('prepares a new deployment on the reloaded key, kept in the address, while its read is $kind', async ({ read, shows }) => {
    const service = mockService();
    read(service);
    const { url } = await reloadOnto(service);

    const recovery = await screen.findByRole('status', { name: 'Checking the recorded deployment' });
    await within(recovery).findByText(shows);
    fireEvent.click(within(recovery).getByRole('button', { name: 'Prepare a new deployment' }));
    // The fresh form holds the reloaded Deploy's key, with its status read,
    // and the address a reload opens still names it.
    await formShown();
    expect(within(await screen.findByRole('alert', { name: 'Outcome unknown' }))
      .getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
    expect(url.query).toEqual({ submission: 'reloaded-submission-1' });
    expect(screen.queryByRole('status', { name: 'Checking the recorded deployment' })).toBeNull();
    await deployFromTheForm(service);

    expect(submittedBody(service).submission_key).toBe('reloaded-submission-1');
  });

  it('keeps the reloaded key, and writes it back into the address, when the owner leaves the recovery by the header’s Deploy link', async () => {
    const service = mockService();
    service.getDeploySubmission.mockResolvedValue(IN_FLIGHT);
    const page = await reloadOnto(service);
    const recovery = await screen.findByRole('status', { name: 'Checking the recorded deployment' });
    await within(recovery).findByText(IN_FLIGHT.message);

    arriveAt(page, {});
    await vi.waitFor(() => expect(page.url.query).toEqual({ submission: 'reloaded-submission-1' }));
    await screen.findByRole('alert', { name: 'Outcome unknown' });
    expect(screen.queryByRole('status', { name: 'Checking the recorded deployment' })).toBeNull();
    await deployFromTheForm(service);

    expect(submittedBody(service).submission_key).toBe('reloaded-submission-1');
  });

  it.each([
    {
      kind: 'no record (404)',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockRejectedValue(new HttpErrorResponse({
        status: 404, error: { detail: 'No Deploy was committed for this submission. Nothing was set aside and nothing started.' },
      })),
      shows: 'No Deploy was committed for this submission. Nothing was set aside and nothing started.',
    },
    {
      kind: 'not_committed',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockResolvedValue(NOT_COMMITTED),
      shows: NOT_COMMITTED.message,
    },
    {
      kind: 'a committed receipt',
      read: (service: ServiceDouble) => service.getDeploySubmission.mockResolvedValue(RECEIPT),
      shows: RECEIPT.message,
    },
  ])('prepares a new deployment on a new key once the reloaded read is settled as $kind', async ({ read, shows }) => {
    const service = mockService();
    read(service);
    const { url } = await reloadOnto(service);

    await screen.findByText(shows);
    fireEvent.click(screen.getByRole('button', { name: 'Prepare a new deployment' }));
    await vi.waitFor(() => expect(url.query).toEqual({}));
    await deployFromTheForm(service);

    expect(submittedBody(service).submission_key).not.toBe('reloaded-submission-1');
  });

  it('offers no fresh form for another unsettled Deploy while this form holds its own', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    service.getDeploySubmission.mockResolvedValue({ ...IN_FLIGHT, submission_key: 'older-submission-1' });
    const { fixture, queryParamMap } = await renderWithQuery(service, {});
    await formShown();
    await fixture.whenStable();
    await chooseMoney('1000.00');
    fireEvent.click(deployButton());
    await screen.findByRole('alert', { name: 'Outcome unknown' });
    // A changed ticket drops the sent command, as any edit would.
    await openStep('How');
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Custom shares/ }));
    await fixture.whenStable();

    queryParamMap.next(convertToParamMap({ submission: 'older-submission-1' }));
    const recovery = await screen.findByRole('status', { name: 'Checking the recorded deployment' });
    await within(recovery).findByText(IN_FLIGHT.message);

    expect(within(recovery).queryByRole('button', { name: 'Prepare a new deployment' })).toBeNull();
    expect(within(recovery).getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
  });

  it('says nothing was committed when the recovery read finds no Deploy for the key', async () => {
    const service = mockService();
    await renderWithQuery(service, { submission: 'never-committed-1' });

    expect(await screen.findByText('No Deploy was committed for this submission. Nothing was set aside and nothing started.'))
      .toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Check deployment status' })).toBeNull();
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  // A key the backend refuses (422) can never be read or settled: adopted, it
  // would hold the form on "Outcome unknown" for the rest of the session.
  it.each(['_abcdefgh', '-abcdefgh'])('ignores ?submission=%s, a key the backend refuses, and deploys on a fresh key', async key => {
    const service = mockService();
    service.getDeploySubmission.mockRejectedValue(new HttpErrorResponse({
      status: 422, error: { detail: [{ loc: ['path', 'submission_key'], msg: 'String should match pattern' }] },
    }));
    const { fixture } = await openAt(service, { submission: key });
    await fixture.whenStable();

    expect(service.getDeploySubmission).not.toHaveBeenCalled();
    expect(screen.queryByRole('status', { name: 'Checking the recorded deployment' })).toBeNull();
    expect(screen.queryByRole('alert', { name: 'Outcome unknown' })).toBeNull();
    await deployFromTheForm(service);

    expect(submittedBody(service).submission_key).not.toBe(key);
    expect(submittedBody(service).submission_key).toMatch(SUBMISSION_KEY_RE);
  });

  it('admits exactly the submission keys the backend admits (the OpenAPI contract’s pattern)', () => {
    const patterns = contractSubmissionKeyPatterns();

    // The Deploy body's field and the recovery read's path parameter.
    expect(patterns.length).toBeGreaterThanOrEqual(2);
    expect(new Set(patterns)).toEqual(new Set([SUBMISSION_KEY_RE.source]));
  });
});

/** Every `pattern` the committed OpenAPI contract declares for a `submission_key`. */
function contractSubmissionKeyPatterns(): string[] {
  const contract = join(__dirname, '..', '..', '..', '..', '..', '..', 'contracts', 'openapi', 'python-data-service.openapi.json');
  const found: string[] = [];
  const patternOf = (schema: unknown): unknown =>
    typeof schema === 'object' && schema !== null ? new Map(Object.entries(schema)).get('pattern') : undefined;
  const visit = (node: unknown): void => {
    if (typeof node !== 'object' || node === null) return;
    const fields = new Map(Object.entries(node));
    // A schema property named `submission_key`, and a parameter named so.
    const declared = [fields.get('submission_key'), fields.get('name') === 'submission_key' ? fields.get('schema') : undefined];
    for (const pattern of declared.map(patternOf)) {
      if (typeof pattern === 'string') found.push(pattern);
    }
    fields.forEach(visit);
  };
  visit(JSON.parse(readFileSync(contract, 'utf8')));
  return found;
}

describe('AlpacaDeployWorkflowComponent — Deploy again', () => {
  const PREFILL: BotDeployPrefill = {
    source_strategy_instance_id: 'spy-ema-20260925-1402',
    strategy_key: 'ema_crossover_signal',
    symbol: 'QQQ',
    sizing: { preset: 'custom', quantity: 3 },
    parameters: { gap: 0.4 },
    exit_terms: { exit_allowance_bps: 15, band_multiple: 3, spread_cap_bps: 40 },
  };

  it('pre-fills strategy, symbol, size, settings and exit terms, never money or consent, and names the replaced bot', async () => {
    const service = mockService();
    service.getDeployPrefill.mockResolvedValue(PREFILL);
    const { fixture } = await renderWithQuery(service, { from: PREFILL.source_strategy_instance_id });
    await screen.findByText(/Prefilled from/);
    await fixture.whenStable();

    expect(service.getDeployPrefill.mock.calls[0][1]).toBe('spy-ema-20260925-1402');
    expect(screen.getByRole('region', { name: 'Deploy again' }).textContent)
      .toContain('Choose fresh money and confirm.');
    await vi.waitFor(() => expect(symbolPicker(fixture).symbol()).toBe('QQQ'));
    await openStep('What');
    expect((screen.getByLabelText('Deployment strategy') as HTMLSelectElement).value).toBe('ema_crossover_signal');
    expect((screen.getByRole('textbox', { name: 'Crossover gap' }) as HTMLInputElement).value).toBe('0.4');
    expect(within(stepRegion('How')).getByText(/3 shares · Exit allowance 15 bps, band 3×, spread cap 40 bps/)).toBeTruthy();
    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('');

    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    const body = submittedBody(service);
    expect(body.replaces_strategy_instance_id).toBe('spy-ema-20260925-1402');
    expect(body).toMatchObject({ strategy_key: 'ema_crossover_signal', symbol: 'QQQ', sizing: { preset: 'custom', quantity: 3 },
      parameters: { gap: 0.4 }, exit_terms: PREFILL.exit_terms });
  });

  it('names the replaced bot on the receipt', async () => {
    const service = mockService(DEPLOY_VIEW, { ...RECEIPT, replaces_strategy_instance_id: 'spy-ema-20260925-1402' });
    service.getDeployPrefill.mockResolvedValue(PREFILL);
    const { fixture } = await renderWithQuery(service, { from: PREFILL.source_strategy_instance_id });
    await screen.findByText(/Prefilled from/);
    await fixture.whenStable();
    await chooseMoney();
    fireEvent.click(deployButton());

    const receipt = await screen.findByRole('status', { name: RECEIPT.message });
    expect(within(receipt).getByText('Replaces').nextElementSibling?.textContent).toBe('spy-ema-20260925-1402');
  });

  it('Clear drops the prefill: the earlier bot is no longer named and the form starts fresh', async () => {
    const service = mockService();
    service.getDeployPrefill.mockResolvedValue(PREFILL);
    const { fixture } = await renderWithQuery(service, { from: PREFILL.source_strategy_instance_id });
    await screen.findByText(/Prefilled from/);
    await fixture.whenStable();

    fireEvent.click(screen.getByRole('button', { name: 'Clear the prefilled settings' }));
    await fixture.whenStable();

    expect(screen.queryByText(/Prefilled from/)).toBeNull();
    await vi.waitFor(() => expect(screen.getByText(/^Deployment Validation on SPY/)).toBeTruthy());
    // The fresh form's exit terms are the account's defaults, never the replaced bot's 15/3/40.
    expect(within(stepRegion('How')).getByText(
      'Paper · 1 share · Exit allowance 20 bps, band 2×, spread cap 50 bps · fixed for this bot’s life',
    )).toBeTruthy();
    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service)).not.toHaveProperty('replaces_strategy_instance_id');
    expect(submittedBody(service).exit_terms).toEqual(DEPLOY_VIEW.default_exit_terms);
  });

  it('Clear keeps a lost Deploy’s key until its status is read', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    service.getDeployPrefill.mockResolvedValue(PREFILL);
    const { fixture } = await renderWithQuery(service, { from: PREFILL.source_strategy_instance_id });
    await screen.findByText(/Prefilled from/);
    await fixture.whenStable();
    await chooseMoney();
    fireEvent.click(deployButton());
    await screen.findByRole('alert', { name: 'Outcome unknown' });

    fireEvent.click(screen.getByRole('button', { name: 'Clear the prefilled settings' }));
    await fixture.whenStable();
    await vi.waitFor(() => expect(screen.getByText(/^Deployment Validation on SPY/)).toBeTruthy());
    expect(screen.getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));

    expect(submittedBody(service, 1).submission_key).toBe(submittedBody(service, 0).submission_key);
  });

  it('keeps a lost Deploy’s key in the address, and sends it, when Deploy again opens the form', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    service.getDeployPrefill.mockResolvedValue(PREFILL);
    const { page, sent } = await lostOnAPage(service);

    arriveAt(page, { from: PREFILL.source_strategy_instance_id });
    await screen.findByText(/Prefilled from/);
    await vi.waitFor(() => expect(page.url.query).toEqual({ from: PREFILL.source_strategy_instance_id, submission: sent }));
    await page.fixture.whenStable();
    await vi.waitFor(() => expect(symbolPicker(page.fixture).symbol()).toBe('QQQ'));
    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));

    expect(submittedBody(service, 1)).toMatchObject({
      submission_key: sent,
      strategy_key: PREFILL.strategy_key,
      symbol: PREFILL.symbol,
      replaces_strategy_instance_id: PREFILL.source_strategy_instance_id,
    });
  });

  it('says when the earlier bot’s settings cannot be read, and offers a fresh form', async () => {
    const service = mockService();
    service.getDeployPrefill.mockRejectedValue(new HttpErrorResponse({
      status: 404, error: { detail: { message: 'No deployed bot spy-gone exists on this account.' } },
    }));
    await renderWithQuery(service, { from: 'spy-gone' });

    const failure = await screen.findByRole('alert', { name: 'Deploy again' });
    expect(within(failure).getByText('No deployed bot spy-gone exists on this account.')).toBeTruthy();
    expect(within(failure).getByRole('button', { name: 'Start from a fresh form' })).toBeTruthy();
  });
});

@Component({
  selector: 'app-deploy-host',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaDeployWorkflowComponent],
  template: `@if (shown()) { <app-alpaca-deploy-workflow [target]="target" accountId="PA9" [fence]="fence" /> }`,
})
class DeployHostComponent {
  readonly shown = signal(true);
  readonly target = withAccount(DEPLOY_TARGET, 'PA9');
  readonly fence = FENCE;
}

describe('AlpacaDeployWorkflowComponent — the session draft (H9)', () => {
  it('keeps the form across leaving Deploy and coming back, with money re-previewed and consent fresh', async () => {
    const service = mockService();
    const { fixture } = await render(DeployHostComponent, {
      providers: [
        ...fakePickerWorld().providers,
        provideFleetDirectory(),
        INSTANT_SETTLE,
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: service },
      ],
    });
    await formShown();
    await fixture.whenStable();
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'ema_crossover_signal' } });
    await openStep('How');
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Custom shares/ }));
    await chooseMoney('750.00');

    fixture.componentInstance.shown.set(false);
    await fixture.whenStable();
    expect(screen.queryByRole('heading', { name: 'What' })).toBeNull();
    const previewsBefore = service.previewBudget.mock.calls.length;
    fixture.componentInstance.shown.set(true);
    await fixture.whenStable();
    await formShown();

    expect((screen.getByLabelText('Deployment strategy') as HTMLSelectElement).value).toBe('ema_crossover_signal');
    expect(within(stepRegion('How')).getByRole<HTMLInputElement>('radio', { name: /Custom shares/ }).checked).toBe(true);
    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('750.00');
    await screen.findByText(/would be set aside for this bot/, {}, { timeout: 3000 });
    expect(service.previewBudget.mock.calls.length).toBeGreaterThan(previewsBefore);
  });
});

describe('AlpacaDeployWorkflowComponent — a lost Deploy across leaving and coming back', () => {
  it('still offers its status read and keeps its key when the owner returns and edits', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    const { fixture } = await render(DeployHostComponent, {
      providers: [
        ...fakePickerWorld().providers,
        provideFleetDirectory(),
        INSTANT_SETTLE,
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: service },
      ],
    });
    await formShown();
    await fixture.whenStable();
    await chooseMoney('1000.00');
    fireEvent.click(deployButton());
    await screen.findByRole('alert', { name: 'Outcome unknown' });

    fixture.componentInstance.shown.set(false);
    await fixture.whenStable();
    fixture.componentInstance.shown.set(true);
    await fixture.whenStable();
    await formShown();

    const refusal = await screen.findByRole('alert', { name: 'Outcome unknown' });
    expect(within(refusal).getByRole('button', { name: 'Check deployment status' })).toBeTruthy();
    await chooseMoney('1200.00');
    await fixture.whenStable();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(2));

    expect(submittedBody(service, 1).submission_key).toBe(submittedBody(service, 0).submission_key);
    expect(service.getDeploySubmission).not.toHaveBeenCalled();
  });
});

describe('AlpacaDeployWorkflowComponent — an unsettled key in the address', () => {
  it('writes a lost Deploy’s key back into the address when the owner returns without it', async () => {
    const service = mockService(DEPLOY_VIEW, new HttpErrorResponse({ status: 0 }));
    const { fixture } = await render(DeployHostComponent, {
      providers: [
        ...fakePickerWorld().providers,
        provideFleetDirectory(),
        INSTANT_SETTLE,
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: service },
      ],
    });
    const router = fixture.debugElement.injector.get(Router);
    await formShown();
    await fixture.whenStable();
    await chooseMoney('1000.00');
    fireEvent.click(deployButton());
    await screen.findByRole('alert', { name: 'Outcome unknown' });
    const sent = submittedBody(service).submission_key;

    fixture.componentInstance.shown.set(false);
    await fixture.whenStable();
    await router.navigateByUrl('/');
    fixture.componentInstance.shown.set(true);
    await fixture.whenStable();

    await screen.findByRole('alert', { name: 'Outcome unknown' });
    await vi.waitFor(() => expect(router.url).toBe(`/?submission=${sent}`));
  });
});

describe('AlpacaDeployWorkflowComponent — accessibility', () => {
  it('passes AXE with every step shown, a reviewed amount and a failing check', async () => {
    const view: DeployBotView = {
      ...DEPLOY_VIEW,
      readiness_checks: DEPLOY_VIEW.readiness_checks.map((check) =>
        check.gate_id === 'broker.channel' ? { ...check, ready: false, recovery: 'Restore the market-data channel.' } : check),
    };
    const { fixture } = await renderWorkflow(mockService(view));
    await openStep('What');
    await openStep('How');
    await chooseMoney();
    await fixture.whenStable();

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('passes AXE with a lost Deploy offering its status read, and a strategy not yet allowed', async () => {
    const notYetAllowed: DeployBotView['strategies'][number] = {
      ...VALIDATION_STRATEGY, paper_access_state: 'available', selectable: false, admissible_modes: ['dry_run'],
      blocked_explanation: 'Broker trading is not enabled for this strategy on this account yet. Allow it in What.',
    };
    const service = mockService({ ...DEPLOY_VIEW, strategies: [notYetAllowed, EMA_STRATEGY] }, new HttpErrorResponse({ status: 0 }));
    const { fixture } = await renderWorkflow(service);
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'ema_crossover_signal' } });
    await fixture.whenStable();
    await chooseMoney();
    fireEvent.click(deployButton());
    await screen.findByRole('button', { name: 'Check deployment status' });
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'deployment_validation' } });
    await fixture.whenStable();
    await screen.findByRole('button', { name: 'Allow on Paper' });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('passes AXE on Live with the typed phrase showing', async () => {
    const { fixture } = await renderWorkflow(mockService(LIVE_DEPLOY_VIEW));
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Live/ }));
    await fixture.whenStable();
    await chooseMoney('800.00');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});

describe('AlpacaDeployWorkflowComponent — strategy, world and settings', () => {
  it('accepts lowercase routes and keeps the opening fence after a rebind before the first click', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    fixture.componentRef.setInput('accountId', 'pa9');
    fixture.componentRef.setInput('target', resourceTarget('alpaca', DEPLOY_TARGET.clerkId, {
      accountId: 'pa9', bindingGeneration: 999, routingEpoch: 1000,
    }));
    await fixture.whenStable();
    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledTimes(1));
    const [target] = service.deployBudgetBot.mock.calls[0];
    expect(target.bindingGeneration).toBe(DEPLOY_TARGET.bindingGeneration);
    expect(target.routingEpoch).toBe(DEPLOY_TARGET.routingEpoch);
    expect(target.accountId).toBe('PA9');
  });

  it('changes the validated strategy and submits the selected strategy key', async () => {
    const service = mockService();
    await renderWorkflow(service);
    await openStep('What');

    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'ema_crossover_signal' } });

    expect(screen.getByRole('button', { name: `About EMA Crossover Signal: ${EMA_STRATEGY.explanation}` })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'View validation' }).getAttribute('href'))
      .toBe('/strategy-validation?strategy=ema_crossover_signal');
    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).strategy_key).toBe('ema_crossover_signal');
  });

  it('renders a strategy setting seeded from its default, flags an edit, then submits it', async () => {
    const service = mockService();
    await renderWorkflow(service);
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'ema_crossover_signal' } });

    const gap = screen.getByRole<HTMLInputElement>('textbox', { name: 'Crossover gap' });
    expect(gap.value).toBe('0.2');
    fireEvent.change(gap, { target: { value: '5' } });
    expect(await screen.findByText('differs from default')).toBeTruthy();

    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).parameters).toEqual({ gap: 5 });
  });

  it('seeds an accepted Golden scope, shows its settings without an editor in Paper, and submits them exactly', async () => {
    const service = mockService({ ...DEPLOY_VIEW, strategies: [GOLDEN_TSLA_STRATEGY] });
    const { fixture } = await renderWorkflow(service);

    expect(symbolPicker(fixture).symbol()).toBe('TSLA');
    await openStep('What');
    expect(screen.queryByRole('textbox', { name: 'Crossover gap' })).toBeNull();
    expect(screen.getByLabelText('Settings in use').textContent).toContain('0.75');

    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service)).toMatchObject({ symbol: 'TSLA', parameters: { gap: 0.75 } });
  });

  it('opens the settings editor in Dry Run, and keeps other settings out of Paper', async () => {
    const { fixture } = await renderWorkflow(mockService({ ...DEPLOY_VIEW, strategies: [GOLDEN_TSLA_STRATEGY] }));
    await openStep('What');
    fireEvent.click(screen.getByRole('button', { name: 'Try other settings in Dry Run' }));
    await fixture.whenStable();

    fireEvent.change(screen.getByRole('textbox', { name: 'Crossover gap' }), { target: { value: '0.2' } });
    await openStep('How');
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Paper/ }));
    await fixture.whenStable();

    expect(deployButton().disabled).toBe(true);
    expect(screen.getByText(/Paper trades only this strategy’s qualified symbol and settings/)).toBeTruthy();
  });

  it('blocks deployment while a strategy setting shows unparseable text', async () => {
    const service = mockService();
    await renderWorkflow(service);
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'ema_crossover_signal' } });
    await chooseMoney();
    expect(deployButton().disabled).toBe(false);

    fireEvent.change(screen.getByRole('textbox', { name: 'Crossover gap' }), { target: { value: 'not-a-number' } });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(true));
    fireEvent.click(deployButton());
    expect(service.deployBudgetBot).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole('textbox', { name: 'Crossover gap' }), { target: { value: '0.4' } });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
  });

  it('requires the durable override to deploy an evidence-only strategy to Paper, then sends it', async () => {
    const service = mockService();
    await renderWorkflow(service);
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'sma_crossover' } });
    await chooseMoney();

    expect(screen.getByRole('heading', { name: 'Dangerous human override' })).toBeTruthy();
    expect(deployButton().disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('I accept the evidence-only deployment risk for this strategy.'));
    expect(deployButton().disabled).toBe(true);
    fireEvent.input(screen.getByLabelText('Operator reason'), {
      target: { value: 'Paper ceremony run; evidence-only risk accepted.' },
    });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).evidence_override).toEqual({
      acknowledgement: 'I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK',
      reason: 'Paper ceremony run; evidence-only risk accepted.',
    });
  });

  it('still requires the durable override for an evidence-only Shadow deploy', async () => {
    const service = mockService(SHADOW_DEPLOY_VIEW);
    await renderWorkflow(service);
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'sma_crossover' } });
    await chooseMoney();
    expect(deployButton().disabled).toBe(true);

    fireEvent.click(screen.getByLabelText('I accept the evidence-only deployment risk for this strategy.'));
    fireEvent.input(screen.getByLabelText('Operator reason'), {
      target: { value: 'Shadow ceremony run; evidence-only risk accepted.' },
    });
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    fireEvent.click(deployButton());

    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).execution_mode).toBe('shadow');
    expect(submittedBody(service).evidence_override?.reason).toBe('Shadow ceremony run; evidence-only risk accepted.');
  });

  it('initializes the strategy from the strategy-key deep link', async () => {
    const service = mockService();
    await render(AlpacaDeployWorkflowComponent, {
      providers: [
        ...fakePickerWorld().providers,
        provideFleetDirectory(),
        INSTANT_SETTLE,
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: { queryParamMap: of(convertToParamMap({ strategy_key: 'ema_crossover_signal' })),
            snapshot: { queryParamMap: convertToParamMap({ strategy_key: 'ema_crossover_signal' }) } },
        },
        { provide: BrokerV2PanelService, useValue: service },
      ],
      componentInputs: { fence: FENCE, target: DEPLOY_TARGET, accountId: 'PA9' },
    });

    expect(await screen.findByText(/^EMA Crossover Signal on SPY/)).toBeTruthy();
  });

  it('honors a changed strategy deep link on a reused route and clears the prior admission and override', async () => {
    const service = mockService();
    service.previewStartAdmission.mockResolvedValue({ ...ADMISSION, allowed: false, explanation: 'This result belongs only to SMA.' });
    const { queryParamMap, fixture } = await renderWithQuery(service, { strategy_key: 'sma_crossover' });
    await screen.findByRole('heading', { name: 'Dangerous human override' });
    fireEvent.click(screen.getByLabelText('I accept the evidence-only deployment risk for this strategy.'));
    fireEvent.input(screen.getByLabelText('Operator reason'), { target: { value: 'This override belongs only to SMA.' } });
    await chooseMoney();
    fireEvent.click(deployButton());
    await screen.findByRole('alert', { name: 'Start refused' });

    queryParamMap.next(convertToParamMap({ strategy_key: 'ema_crossover_signal' }));
    await fixture.whenStable();

    await screen.findByText(/^EMA Crossover Signal on SPY/);
    expect(screen.queryByRole('alert', { name: 'Start refused' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Dangerous human override' })).toBeNull();

    queryParamMap.next(convertToParamMap({ strategy_key: 'sma_crossover' }));
    await screen.findByRole('heading', { name: 'Dangerous human override' });
    expect(screen.getByLabelText<HTMLInputElement>('I accept the evidence-only deployment risk for this strategy.').checked)
      .toBe(false);
    expect(screen.getByLabelText<HTMLTextAreaElement>('Operator reason').value).toBe('');
  });

  it('offers a blocked strategy for Dry Run while Paper stays disabled with the backend reason', async () => {
    const service = mockService({ ...DEPLOY_VIEW, strategies: [VALIDATION_STRATEGY, EMA_STRATEGY, SMA_OVERRIDE_STRATEGY, BLOCKED_STRATEGY] });
    const { fixture } = await renderWorkflow(service);
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'rsi_mean_reversion' } });
    await fixture.whenStable();

    expect(screen.getByRole<HTMLOptionElement>('option', { name: /RSI Mean Reversion/ }).disabled).toBe(false);
    expect(screen.getAllByText(BLOCKED_EXPLANATION).length).toBeGreaterThanOrEqual(1);
    await openStep('How');
    const how = stepRegion('How');
    expect(within(how).getByRole<HTMLInputElement>('radio', { name: /Paper/ }).disabled).toBe(true);
    expect(within(how).getByRole('button', { name: `About Paper: ${BLOCKED_EXPLANATION}` })).toBeTruthy();

    fireEvent.click(within(how).getByRole('radio', { name: /Dry Run/ }));
    await chooseMoney();
    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).execution_mode).toBe('dry_run');
    expect(submittedBody(service).carryover_policy).toBe('FORBID');
  });

  it('lets the owner inspect a no-runtime strategy while disabling both worlds', async () => {
    const service = mockService({ ...DEPLOY_VIEW, strategies: [VALIDATION_STRATEGY, NO_RUNTIME_STRATEGY] });
    const { fixture } = await renderWorkflow(service);
    await openStep('What');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'spy_strategy_b' } });
    await fixture.whenStable();
    await openStep('How');

    const how = stepRegion('How');
    expect(within(how).getByRole<HTMLInputElement>('radio', { name: /Paper/ }).disabled).toBe(true);
    expect(within(how).getByRole<HTMLInputElement>('radio', { name: /Dry Run/ }).disabled).toBe(true);
    expect(deployButton().disabled).toBe(true);
    expect(screen.getAllByText(NO_RUNTIME_STRATEGY.blocked_explanation ?? '').length).toBeGreaterThanOrEqual(1);
  });

  it('auto-selects the first selectable strategy, skipping a blocked one', async () => {
    await renderWorkflow(mockService({ ...DEPLOY_VIEW, strategies: [BLOCKED_STRATEGY, EMA_STRATEGY, SMA_OVERRIDE_STRATEGY] }));

    expect(screen.getByText(/^EMA Crossover Signal on SPY/)).toBeTruthy();
  });

  it('keeps an owner-picked symbol when the strategy changes', async () => {
    const { fixture } = await renderWorkflow();
    await openStep('What');

    const picker = pickSymbol(fixture, 'QQQ');
    fireEvent.change(screen.getByLabelText('Deployment strategy'), { target: { value: 'ema_crossover_signal' } });

    expect(picker.symbol()).toBe('QQQ');
  });

  it('preselects Shadow on a shadow account and never calls it paper', async () => {
    const service = mockService(SHADOW_DEPLOY_VIEW);
    await renderWorkflow(service);

    await openStep('How');
    const how = stepRegion('How');
    expect(within(how).queryByRole('radio', { name: /Paper/ })).toBeNull();
    expect(within(how).getByRole<HTMLInputElement>('radio', { name: /Shadow/ }).checked).toBe(true);
    await chooseMoney();
    expect(deployButton().textContent?.trim()).toBe('Deploy shadow bot · set aside $1,000.00');
    expect(document.body.textContent).not.toMatch(/paper/i);

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).execution_mode).toBe('shadow');
  });

  it('offers only this account’s broker world, and a blocked strategy makes its card carry the reason', async () => {
    await renderWorkflow(mockService({ ...LIVE_DEPLOY_VIEW, strategies: [BLOCKED_STRATEGY] }));

    const how = stepRegion('How');
    expect(within(how).queryByRole('radio', { name: /Paper/ })).toBeNull();
    const live = within(how).getByRole<HTMLInputElement>('radio', { name: /Live/ });
    expect(live.disabled).toBe(true);
    expect(within(how).getByRole('button', { name: `About Live: ${BLOCKED_EXPLANATION}` })).toBeTruthy();
  });

  it('submits bounded custom whole-share sizing', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    await openStep('How');
    fireEvent.click(within(stepRegion('How')).getByRole('radio', { name: /Custom shares/ }));
    await fixture.whenStable();
    fireEvent.input(screen.getByRole('spinbutton', { name: 'Whole shares per entry' }), { target: { value: '7' } });
    await chooseMoney();

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).sizing).toEqual({ preset: 'custom', quantity: 7 });
  });

  it('submits the carryover opt-in when the account offers it', async () => {
    const service = mockService({ ...DEPLOY_VIEW, carryover_available: true });
    await renderWorkflow(service);
    await openStep('How');
    fireEvent.click(screen.getByRole('checkbox', { name: /Allow Clerk-proven exposure carryover on STOP/i }));
    await chooseMoney();

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
    expect(submittedBody(service).carryover_policy).toBe('ALLOW');
  });

  it('applies the qualified settings together and re-previews the kept amount for them', async () => {
    const view: DeployBotView = { ...DEPLOY_VIEW, strategies: [{ ...EMA_STRATEGY,
      validation_case_parameters: { gap: 5 },
      qualified_configuration: { symbol: 'AAPL', parameters: { gap: 0.75 }, explanation: 'This exact tuple has retained qualified evidence.' },
    }] };
    const service = mockService(view);
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();
    await openStep('What');

    fireEvent.click(screen.getByRole('button', { name: 'Use qualified configuration' }));
    await vi.waitFor(() => expect(symbolPicker(fixture).symbol()).toBe('AAPL'));
    await vi.waitFor(() => expect(service.previewBudget.mock.calls.at(-1)?.[1])
      .toMatchObject({ symbol: 'AAPL', parameters: { gap: 0.75 }, budget: { amount_usd: '1000.00' } }));
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });
});

describe('AlpacaDeployWorkflowComponent — Start checks and the lane fence', () => {
  it('renders a backend-authored Start refusal without dispatching deployment', async () => {
    const service = mockService();
    service.previewStartAdmission.mockResolvedValue({
      ...ADMISSION, allowed: false, reason_code: 'MARKET_DATA_STALE',
      explanation: 'The required market-data feed is not proven ready for this run.',
      next_step: 'Restore fresh market data before Start.',
    });
    await renderWorkflow(service);
    await chooseMoney();
    fireEvent.click(deployButton());

    const refusal = await screen.findByRole('alert', { name: 'Start refused' });
    expect(within(refusal).getByText('The required market-data feed is not proven ready for this run.')).toBeTruthy();
    expect(within(refusal).getByText('Next: Restore fresh market data before Start.')).toBeTruthy();
    expect(screen.getByText('Runner safety')).toBeTruthy();
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  it('removes an obsolete Start decision when the settings change', async () => {
    const service = mockService();
    service.previewStartAdmission.mockResolvedValue({ ...ADMISSION, allowed: false });
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();
    fireEvent.click(deployButton());
    await screen.findByRole('alert', { name: 'Start refused' });
    await openStep('What');

    pickSymbol(fixture, 'QQQ');

    expect(screen.queryByRole('alert', { name: 'Start refused' })).toBeNull();
  });

  it('discards a Start answer when the settings change while it is in flight', async () => {
    let resolvePreview!: (decision: RunAdmissionDecision) => void;
    const service = mockService();
    service.previewStartAdmission.mockReturnValue(new Promise((resolve) => { resolvePreview = resolve; }));
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();
    await openStep('What');

    fireEvent.click(deployButton());
    await vi.waitFor(() => expect(service.previewStartAdmission).toHaveBeenCalledOnce());
    pickSymbol(fixture, 'QQQ');
    resolvePreview(ADMISSION);
    await fixture.whenStable();

    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  // A Start refused after the bot was named is not settled by the backend,
  // so it is an unknown outcome with its status read; only the settled
  // marker lets the same conflict read as one.
  it.each([
    { settled: true, title: 'The account changed before Deploy', offersCheck: false },
    { settled: false, title: 'Outcome unknown', offersCheck: true },
  ])('words a sent Deploy’s conflict as “$title” when its key is settled: $settled', async ({ settled, title, offersCheck }) => {
    const refusedAdmission = {
      ...ADMISSION, allowed: false, reason_code: 'CUSTODY_HOLD_ACTIVE',
      explanation: 'The account entered a hold after the preview.', next_step: 'Clear the hold before Start.',
    } satisfies RunAdmissionDecision;
    const error = new HttpErrorResponse({
      status: 409,
      error: { detail: {
        outcome: 'conflict', receipt_id: 'deploy-conflict-1', recorded_at_ms: 1_700_000_000_002,
        message: 'Deployment readiness changed.', why: 'The account entered a hold after the page loaded.',
        next_action: 'Reload and clear the hold.', admission: refusedAdmission, submission_settled: settled,
      } },
    });
    await renderWorkflow(mockService(DEPLOY_VIEW, error));
    await chooseMoney();
    fireEvent.click(deployButton());

    const refusal = await screen.findByRole('alert', { name: title });
    expect(within(refusal).getByText('The account entered a hold after the page loaded.')).toBeTruthy();
    expect(within(refusal).getByText('deploy-conflict-1')).toBeTruthy();
    expect(within(refusal).getByText(refusedAdmission.explanation)).toBeTruthy();
    expect(within(refusal).queryByRole('button', { name: 'Check deployment status' }) !== null).toBe(offersCheck);
  });

  it('blocks resubmission, not just the banner, when the account rebinds under a frozen preview', async () => {
    const service = mockService();
    service.previewStartAdmission.mockResolvedValue({ ...ADMISSION, allowed: false });
    const { fixture } = await renderWorkflow(service);
    await chooseMoney();
    fireEvent.click(deployButton());
    await screen.findByRole('alert', { name: 'Start refused' });

    fixture.componentRef.setInput('accountId', 'PA10');
    await fixture.whenStable();

    expect((await screen.findAllByText(/The account changed. Nothing was sent/i)).length).toBeGreaterThan(0);
    expect(deployButton().disabled).toBe(true);
    await fixture.componentInstance['submit']();
    expect(service.previewStartAdmission).toHaveBeenCalledTimes(1);
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
  });

  it('refuses to submit when the page opened against a cold directory, and dispatches nothing', async () => {
    const service = mockService();
    const { fixture } = await render(AlpacaDeployWorkflowComponent, {
      providers: [
        ...fakePickerWorld().providers,
        provideFleetDirectory(),
        INSTANT_SETTLE,
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: service },
      ],
      componentInputs: {
        fence: { bindingGeneration: null, routingEpoch: null },
        target: resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: null, routingEpoch: null }),
        accountId: 'PA9',
      },
    });
    await formShown();
    await chooseMoney();

    await fixture.componentInstance['submit']();

    expect(service.previewStartAdmission).not.toHaveBeenCalled();
    expect(service.deployBudgetBot).not.toHaveBeenCalled();
    expect(screen.getByText(/no known binding when the action was opened/i)).toBeTruthy();
    expect(deployButton().disabled).toBe(true);
  });
});
