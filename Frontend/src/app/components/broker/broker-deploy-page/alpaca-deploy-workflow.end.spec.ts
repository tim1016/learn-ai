import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, onTestFinished, vi } from 'vitest';

import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { resourceTarget, withAccount } from '../../../fleet/resource-target';
import { localWallClock } from '../../../shared/date/local-wall-clock';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { formatTimestampDisplay } from '../../../shared/timestamp/timestamp-display';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import { panelRefusalBody } from '../../../testing/bot-panel-fixtures';
import {
  BrokerV2PanelService,
  type BotEndPreviewRequest,
  type BotEndView,
  type DeployBotBody,
  type DeployBotView,
  type DeploySubmissionBody,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';
import { DEFAULT_END, DEPLOY_VIEW, EMA_STRATEGY, previewedEnd, scheduledEndView } from './alpaca-deploy-workflow.fixtures';
import { DEPLOY_TYPING_SETTLE_MS } from './deploy-money-step.component';

/**
 * The Deploy form's end (#2607): pre-filled from the account's default end,
 * a date and a time in the viewer's zone or "no end", Sell or Keep (never
 * Keep for a Dry Run), the backend's words for it, and its refusal. Split
 * from the workflow spec, as the symbol-scope spec is, for its size.
 */

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const FENCE = { bindingGeneration: 3, routingEpoch: 4 };
const DV_NOTICE = 'Experimental validation only — not a trading strategy';

/** `bot_end.resolved_bot_end_view` for 15:00 ET on a half-day, moved before its early close. */
const HALF_DAY_END: BotEndView = {
  ...DEFAULT_END,
  end_at_ms: Date.UTC(2023, 10, 24, 17, 59),
  headline: 'Ends Fri Nov 24, 12:59 ET · sells',
  explanation: 'At Fri Nov 24, 12:59 ET the Clerk stops the bot, cancels its working orders and sells its shares at market.',
  notice: 'Fri Nov 24 closes early at 13:00 ET, so this bot ends at 12:59 ET.',
};

/** The panel's refusal of an end (400), exactly as `_raise_panel_error` sends it. */
const REFUSED = new HttpErrorResponse({
  status: 400,
  error: panelRefusalBody({
    message: 'The end must fall within regular hours.',
    why: 'On Tue Nov 14 the market is open from 09:30 to 16:00 ET, so the latest end is 15:59 ET.',
    next_action: 'Choose a time from 09:30 to 15:59 ET.',
    reason_code: 'BOT_END_REFUSED',
  }),
});

/** An end whose minute has gone by while the page stood open. */
const PASSED = new HttpErrorResponse({
  status: 400,
  error: panelRefusalBody({
    message: 'That end time has already passed.',
    why: 'A bot\'s end must be later than now.',
    next_action: 'Choose a time later than now.',
    reason_code: 'BOT_END_REFUSED',
  }),
});

/** A check that could not be read at all: the runner is down (`PanelUnavailableError`, 503, no code). */
const UNREADABLE = new HttpErrorResponse({
  status: 503,
  error: panelRefusalBody({
    message: 'The bot runner is not available.',
    why: 'The service is still starting or has shut down.',
    next_action: 'Wait for the data plane to become healthy, then refresh.',
  }),
});

function mockService() {
  return {
    getDeployView: vi.fn<() => Promise<DeployBotView>>().mockResolvedValue(DEPLOY_VIEW),
    previewStartAdmission: vi.fn().mockResolvedValue({ allowed: true }),
    previewBotEnd: vi.fn<(target: unknown, body: BotEndPreviewRequest) => Promise<BotEndView>>()
      .mockImplementation(async (_target, body) => previewedEnd(body)),
    previewBudget: vi.fn().mockImplementation(async (_target, body: DeployBotBody) => ({
      state: 'ready', detail: 'Money is ready.', world: body.execution_mode === 'dry_run' ? 'synthetic' : 'real_paper',
      custody_account_id: 'PA9', risk_revision: 1, minimum_budget_usd: '500.00', unreserved_usd: '1000.00',
      shortcuts: [], money_after: fakeAccountMoney(), review_token: body.budget ? 'reviewed-money' : null,
      budget_usd: body.budget?.amount_usd ?? null,
    })),
    deployBudgetBot: vi.fn().mockResolvedValue({
      status: 'deployed', outcome: 'success', receipt_id: 'r-1', command_id: 'c-1', recorded_at_ms: 1,
      first_deployed_at_ms: 1, strategy_instance_id: 'spy-dv-20260929-0931', run_id: 'run-1', account_id: 'PA9',
      world: 'real_paper', committed_usd: '1000.00', message: 'deployed', explanation: 'set aside',
      next_action: 'Open it.', replaces_strategy_instance_id: null,
    }),
  };
}
type ServiceDouble = ReturnType<typeof mockService>;

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => { resolvePromise = resolve; });
  return { promise, resolve: resolvePromise };
}

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
    componentInputs: { fence: FENCE, target: withAccount(TARGET, 'PA9'), accountId: 'PA9' },
  });
  await screen.findByText('What', { selector: 'h2' });
  await rendered.fixture.whenStable();
  return rendered;
}

function step(name: string): HTMLElement {
  const heading = screen.getByText(name, { selector: 'h2' });
  const section = heading.closest<HTMLElement>(`section[aria-labelledby="${heading.id}"]`);
  if (section === null) throw new Error(`No step ${name}.`);
  return section;
}

/** How's end, as the column shows it. */
function endSummary(): HTMLElement {
  return screen.getByRole('region', { name: 'End · changeable later' });
}

/** The end's fields, which open over the page from How. */
function endEditor(): HTMLElement {
  return screen.getByRole('dialog', { name: 'This bot’s end' });
}

/** The line under the typed time that says it in market time. */
function marketTime(): string {
  return within(endEditor()).getByText(/^Market time/).textContent ?? '';
}

/** The column's "your time · ET" line. */
function timeLine(): string {
  return within(endSummary()).getByText(/your time/).textContent ?? '';
}

const etTime = (ms: number | null): string => formatTimestampDisplay(ms, { mode: 'et', granularity: 'time' });
const localTime = (ms: number | null): string => formatTimestampDisplay(ms, { mode: 'local', granularity: 'time' });

function lastPreview(service: ServiceDouble): BotEndPreviewRequest {
  const calls = service.previewBotEnd.mock.calls;
  return calls[calls.length - 1][1];
}

function deployButton(): HTMLButtonElement {
  return within(step('Confirm')).getByText<HTMLButtonElement>(/^Deploy/, { selector: 'button' });
}

/** Types a budget and waits for its review. */
async function chooseMoney(): Promise<void> {
  fireEvent.input(screen.getByLabelText(/\(USD\)$/), { target: { value: '1000.00' } });
  await screen.findByText(/would be set aside for this bot|of simulated starting cash for this bot/, {}, { timeout: 3000 });
}

async function deploy(service: ServiceDouble): Promise<DeploySubmissionBody> {
  await chooseMoney();
  const button = deployButton();
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
  await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
  return service.deployBudgetBot.mock.calls[0][1] as DeploySubmissionBody;
}

describe('AlpacaDeployWorkflowComponent — the bot’s end (#2607)', () => {
  it('pre-fills the account’s default end in the viewer’s own time, with market time beside it', async () => {
    const service = mockService();
    await renderWorkflow(service);

    await within(endSummary()).findByText(DEFAULT_END.headline);
    expect(timeLine()).toContain(localTime(DEFAULT_END.end_at_ms));
    expect(timeLine()).toContain(etTime(DEFAULT_END.end_at_ms));
    const wall = localWallClock(DEFAULT_END.end_at_ms as number);
    const time = within(endEditor()).getByLabelText<HTMLInputElement>(/^End time/);
    expect(within(endEditor()).getByLabelText<HTMLInputElement>('End date').value).toBe(wall.date);
    expect(time.value).toBe(wall.clock);
    // The typed time is said in market time beside it, and the time field names that line.
    const market = within(endEditor()).getByText(/^Market time/);
    expect(market.textContent).toContain(etTime(DEFAULT_END.end_at_ms));
    expect(time.getAttribute('aria-describedby')?.split(' ')).toContain(market.id);
    expect(within(endEditor()).getByRole<HTMLInputElement>('radio', { name: 'Sell its shares' }).checked).toBe(true);
    await vi.waitFor(() => expect(lastPreview(service)).toEqual({
      execution_mode: 'paper', end: { end_at_ms: DEFAULT_END.end_at_ms, end_action: 'SELL' },
    }));
  });

  it('turns a chosen date and time into the instant from the viewer’s own wall clock, with Keep', async () => {
    const service = mockService();
    await renderWorkflow(service);

    const editor = endEditor();
    fireEvent.input(within(editor).getByLabelText('End date'), { target: { value: '2023-11-16' } });
    fireEvent.input(within(editor).getByLabelText(/^End time/), { target: { value: '10:30' } });
    fireEvent.click(within(editor).getByRole('radio', { name: 'Keep its shares' }));

    const chosen = new Date(2023, 10, 16, 10, 30).getTime();
    expect(marketTime()).toContain(etTime(chosen));
    await vi.waitFor(() => expect(lastPreview(service).end).toEqual({ end_at_ms: chosen, end_action: 'KEEP' }));
    await within(endSummary()).findByText(scheduledEndView({ end_at_ms: chosen, end_action: 'KEEP' }).headline);
    expect((await deploy(service)).end).toEqual({ end_at_ms: chosen, end_action: 'KEEP' });
  });

  it('sends "no end" as an explicit null end with Sell, never an absent one', async () => {
    const service = mockService();
    await renderWorkflow(service);
    fireEvent.click(within(endEditor()).getByRole('radio', { name: 'Keep its shares' }));

    fireEvent.click(within(endEditor()).getByRole('checkbox', { name: 'No end — run until I stop it' }));

    await within(endSummary()).findByText('No end · runs until you stop it');
    expect(within(endSummary()).queryByText(/your time/)).toBeNull();
    expect(within(endEditor()).getByLabelText<HTMLInputElement>('End date').disabled).toBe(true);
    expect((await deploy(service)).end).toEqual({ end_at_ms: null, end_action: 'SELL' });
  });

  it('offers no Keep for a Dry Run, which always sells at its end', async () => {
    const service = mockService();
    await renderWorkflow(service);
    fireEvent.click(within(endEditor()).getByRole('radio', { name: 'Keep its shares' }));
    await vi.waitFor(() => expect(lastPreview(service).end?.end_action).toBe('KEEP'));

    fireEvent.click(within(step('How')).getByRole('radio', { name: /Dry Run/ }));

    await vi.waitFor(() => expect(lastPreview(service)).toEqual({
      execution_mode: 'dry_run', end: { end_at_ms: DEFAULT_END.end_at_ms, end_action: 'SELL' },
    }));
    expect(within(endEditor()).queryByRole('radio', { name: 'Keep its shares' })).toBeNull();
    expect((await deploy(service)).end).toEqual({ end_at_ms: DEFAULT_END.end_at_ms, end_action: 'SELL' });
  });

  it('shows the backend’s notice when the end moves before an early close', async () => {
    const service = mockService();
    service.previewBotEnd.mockResolvedValue(HALF_DAY_END);
    await renderWorkflow(service);

    await within(endSummary()).findByText(HALF_DAY_END.headline);
    expect(within(endSummary()).getByText(HALF_DAY_END.notice as string)).toBeTruthy();
  });

  it.each([
    ['Keep is ticked', () => fireEvent.click(within(endEditor()).getByRole('radio', { name: 'Keep its shares' }))],
    ['Dry Run is chosen', () => fireEvent.click(within(step('How')).getByRole('radio', { name: /Dry Run/ }))],
  ])('never shows the earlier end’s words once %s, and holds the Deploy until the new end is checked', async (_change, change) => {
    const service = mockService();
    await renderWorkflow(service);
    await within(endSummary()).findByText(DEFAULT_END.headline);
    await chooseMoney();
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
    const pending = deferred<BotEndView>();
    service.previewBotEnd.mockImplementationOnce(() => pending.promise);

    change();

    await vi.waitFor(() => expect(within(endSummary()).queryByText(DEFAULT_END.headline)).toBeNull());
    expect(within(endSummary()).getByRole('status').textContent).toContain('Checking its end…');
    // What is sent is on screen while it is checked, in both times.
    expect(timeLine()).toContain(etTime(DEFAULT_END.end_at_ms));
    expect(deployButton().disabled).toBe(true);
    expect(within(step('Confirm')).getByText('Checking this bot’s end…')).toBeTruthy();

    pending.resolve(previewedEnd(lastPreview(service)));
    await within(endSummary()).findByText(previewedEnd(lastPreview(service)).headline);
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
  });

  it('shows a refused end in the backend’s words and code, holds the Deploy, and drops it the moment the end changes', async () => {
    const service = mockService();
    service.previewBotEnd.mockRejectedValue(REFUSED);
    await renderWorkflow(service);

    const alert = await within(endSummary()).findByRole('alert');
    expect(alert.textContent).toContain('The end must fall within regular hours.');
    expect(alert.textContent).toContain('Bot End Refused');
    expect(timeLine()).toContain(etTime(DEFAULT_END.end_at_ms));
    const editor = endEditor();
    expect(within(editor).getByText('On Tue Nov 14 the market is open from 09:30 to 16:00 ET, so the latest end is 15:59 ET.')).toBeTruthy();
    expect(within(editor).getByText('Next: Choose a time from 09:30 to 15:59 ET.')).toBeTruthy();
    expect(within(step('How')).getByText('Check its end')).toBeTruthy();
    await chooseMoney();
    expect(deployButton().disabled).toBe(true);
    expect(within(step('Confirm')).getByText('Fix this bot’s end in How.')).toBeTruthy();

    const pending = deferred<BotEndView>();
    service.previewBotEnd.mockImplementation(() => pending.promise);
    fireEvent.click(within(editor).getByRole('checkbox', { name: 'No end — run until I stop it' }));

    // The refusal answered the end before; it says nothing about this one.
    await vi.waitFor(() => expect(within(endSummary()).queryByRole('alert')).toBeNull());
    expect(within(editor).queryByText(/Choose a time from/)).toBeNull();
    expect(deployButton().disabled).toBe(true);
    expect(within(step('Confirm')).getByText('Checking this bot’s end…')).toBeTruthy();

    pending.resolve(previewedEnd(lastPreview(service)));
    await within(endSummary()).findByText('No end · runs until you stop it');
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
  });

  it('holds nothing when the check itself cannot be read, and says so plainly, not as a refusal', async () => {
    const service = mockService();
    service.previewBotEnd.mockRejectedValue(UNREADABLE);
    await renderWorkflow(service);

    const status = await within(endSummary()).findByText('This end could not be checked. Deploy checks it again.');
    expect(status.getAttribute('role')).toBe('status');
    expect(within(endSummary()).queryByRole('alert')).toBeNull();
    expect(timeLine()).toContain(localTime(DEFAULT_END.end_at_ms));
    expect(timeLine()).toContain(etTime(DEFAULT_END.end_at_ms));
    expect(within(step('How')).queryByText('Check its end')).toBeNull();
    await chooseMoney();
    await vi.waitFor(() => expect(deployButton().disabled).toBe(false));
  });

  it('says what the date needs once the owner leaves it, beside the field, and checks nothing it cannot read', async () => {
    const service = mockService();
    await renderWorkflow(service);
    await within(endSummary()).findByText(DEFAULT_END.headline);
    const checked = service.previewBotEnd.mock.calls.length;
    const date = within(endEditor()).getByLabelText<HTMLInputElement>('End date');
    const time = within(endEditor()).getByLabelText<HTMLInputElement>(/^End time/);

    fireEvent.input(date, { target: { value: '' } });

    // Nothing is said while the owner is still in the field …
    expect(within(endEditor()).queryByRole('alert')).toBeNull();
    // … but the earlier end's words go at once, and How says what it needs.
    expect(within(endSummary()).queryByText(DEFAULT_END.headline)).toBeNull();
    expect(within(step('How')).getByText('Needs an end time')).toBeTruthy();
    expect(marketTime()).not.toContain('ET');

    fireEvent.blur(date);

    const alert = await within(endEditor()).findByRole('alert');
    expect(alert.textContent?.trim()).toBe('Enter the end’s date.');
    expect(date.getAttribute('aria-invalid')).toBe('true');
    expect(date.getAttribute('aria-describedby')).toBe(alert.id);
    expect(time.getAttribute('aria-invalid')).toBe('false');
    expect(time.getAttribute('aria-describedby')?.split(' ')).not.toContain(alert.id);
    expect(service.previewBotEnd).toHaveBeenCalledTimes(checked);
  });

  it('goes back to the account’s default end on request', async () => {
    const service = mockService();
    await renderWorkflow(service);
    fireEvent.click(within(endEditor()).getByRole('checkbox', { name: 'No end — run until I stop it' }));
    await within(endSummary()).findByText('No end · runs until you stop it');

    fireEvent.click(within(endEditor()).getByRole('button', { name: 'Use the default end' }));

    await within(endSummary()).findByText(DEFAULT_END.headline);
    expect(within(endEditor()).queryByRole('button', { name: 'Use the default end' })).toBeNull();
  });

  it('takes a fresh default end when the one the page opened with has passed', async () => {
    const tomorrow = scheduledEndView({ end_at_ms: (DEFAULT_END.end_at_ms as number) + 86_400_000, end_action: 'SELL' });
    let view: DeployBotView = DEPLOY_VIEW;
    const service = mockService();
    service.getDeployView.mockImplementation(async () => view);
    service.previewBotEnd.mockImplementation(async (_target, body) => {
      if (body.end?.end_at_ms === DEFAULT_END.end_at_ms) throw PASSED;
      return previewedEnd(body);
    });
    await renderWorkflow(service);
    expect((await within(endSummary()).findByRole('alert')).textContent).toContain('That end time has already passed.');
    const reads = service.getDeployView.mock.calls.length;

    view = { ...DEPLOY_VIEW, default_end: tomorrow };
    fireEvent.click(within(endEditor()).getByRole('button', { name: 'Use the default end' }));

    await within(endSummary()).findByText(tomorrow.headline);
    expect(service.getDeployView.mock.calls.length).toBeGreaterThan(reads);
    expect(within(endSummary()).queryByRole('alert')).toBeNull();
    expect(lastPreview(service).end).toEqual({ end_at_ms: tomorrow.end_at_ms, end_action: 'SELL' });
  });

  it('says loud and clear that Deployment Validation is experimental while it is chosen', async () => {
    const view = { ...DEPLOY_VIEW, strategies: [...DEPLOY_VIEW.strategies] };
    const service = mockService();
    service.getDeployView.mockResolvedValue(view);
    await renderWorkflow(service);

    const what = step('What');
    expect(within(what).getByRole('note').textContent).toContain(DV_NOTICE);

    fireEvent.change(within(what).getByRole('combobox', { name: 'Deployment strategy' }), {
      target: { value: EMA_STRATEGY.strategy_key },
    });
    await vi.waitFor(() => expect(within(what).queryByText(DV_NOTICE)).toBeNull());
  });

  it('passes AXE with the end, its open fields, their market time and a date they still need', async () => {
    await renderWorkflow();
    await within(endSummary()).findByText(DEFAULT_END.headline);
    const date = within(endEditor()).getByLabelText('End date');
    fireEvent.input(date, { target: { value: '' } });
    fireEvent.blur(date);
    await within(endEditor()).findByRole('alert');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
