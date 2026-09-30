import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, onTestFinished, vi } from 'vitest';

import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { resourceTarget, withAccount } from '../../../fleet/resource-target';
import { localWallClock } from '../../../shared/date/local-wall-clock';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import {
  BrokerV2PanelService,
  type BotEndPreviewRequest,
  type BotEndView,
  type DeployBotBody,
  type DeploySubmissionBody,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';
import { DEFAULT_END, DEPLOY_VIEW, EMA_STRATEGY, previewedEnd } from './alpaca-deploy-workflow.fixtures';
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
  end_at_ms: Date.UTC(2026, 10, 27, 17, 59),
  headline: 'Ends Fri Nov 27 12:59 ET · sells',
  notice: 'Fri Nov 27 closes early at 13:00 ET, so this bot ends at 12:59 ET.',
};

/** The panel's refusal of an end, as `bot_end_panel._refused` words it. */
const REFUSED = new HttpErrorResponse({
  status: 400,
  error: {
    detail: {
      message: 'The end must fall within regular hours.',
      why: 'On Tue Nov 14 the market is open from 09:30 to 16:00 ET, so the latest end is 15:59 ET.',
      next_action: 'Choose a time from 09:30 to 15:59 ET.',
      reason_code: 'BOT_END_REFUSED',
    },
  },
});

function mockService() {
  return {
    getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW),
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

function lastPreview(service: ServiceDouble): BotEndPreviewRequest {
  const calls = service.previewBotEnd.mock.calls;
  return calls[calls.length - 1][1];
}

async function deploy(service: ServiceDouble): Promise<DeploySubmissionBody> {
  fireEvent.input(screen.getByLabelText(/\(USD\)$/), { target: { value: '1000.00' } });
  await screen.findByText(/would be set aside for this bot|of simulated starting cash for this bot/, {}, { timeout: 3000 });
  const button = within(step('Confirm')).getByText<HTMLButtonElement>(/^Deploy/, { selector: 'button' });
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
  await vi.waitFor(() => expect(service.deployBudgetBot).toHaveBeenCalledOnce());
  return service.deployBudgetBot.mock.calls[0][1] as DeploySubmissionBody;
}

describe('AlpacaDeployWorkflowComponent — the bot’s end (#2607)', () => {
  it('pre-fills the account’s default end in the viewer’s own time and shows it with market time', async () => {
    const service = mockService();
    await renderWorkflow(service);

    await within(endSummary()).findByText(DEFAULT_END.headline);
    expect(within(endSummary()).getByText(/your time/).textContent).toContain('ET');
    const wall = localWallClock(DEFAULT_END.end_at_ms as number);
    expect(within(endEditor()).getByLabelText<HTMLInputElement>('End date').value).toBe(wall.date);
    expect(within(endEditor()).getByLabelText<HTMLInputElement>(/^End time/).value).toBe(wall.time);
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
    await vi.waitFor(() => expect(lastPreview(service).end).toEqual({ end_at_ms: chosen, end_action: 'KEEP' }));
    await within(endSummary()).findByText('Ends at the chosen minute · keeps its shares');
    expect((await deploy(service)).end).toEqual({ end_at_ms: chosen, end_action: 'KEEP' });
  });

  it('sends "no end" as an explicit null end, never an absent one', async () => {
    const service = mockService();
    await renderWorkflow(service);

    fireEvent.click(within(endEditor()).getByRole('checkbox', { name: 'No end — run until I stop it' }));

    await within(endSummary()).findByText('No end · runs until you stop it');
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

  it('shows a refused end in the backend’s words, its code as a label, and holds the Deploy until it is fixed', async () => {
    const service = mockService();
    service.previewBotEnd.mockRejectedValue(REFUSED);
    await renderWorkflow(service);

    const how = step('How');
    const alert = await within(endSummary()).findByRole('alert');
    expect(alert.textContent).toContain('The end must fall within regular hours.');
    expect(alert.textContent).toContain('Bot End Refused');
    expect(within(endEditor()).getByText('Next: Choose a time from 09:30 to 15:59 ET.')).toBeTruthy();
    expect(within(how).getByText('Check its end')).toBeTruthy();
    fireEvent.input(screen.getByLabelText(/\(USD\)$/), { target: { value: '1000.00' } });
    await screen.findByText(/would be set aside for this bot/, {}, { timeout: 3000 });
    const button = within(step('Confirm')).getByText<HTMLButtonElement>(/^Deploy/, { selector: 'button' });
    expect(button.disabled).toBe(true);
    expect(within(step('Confirm')).getByText('Fix this bot’s end in How.')).toBeTruthy();

    service.previewBotEnd.mockImplementation(async (_target, body) => previewedEnd(body));
    fireEvent.click(within(endEditor()).getByRole('checkbox', { name: 'No end — run until I stop it' }));
    await within(endSummary()).findByText('No end · runs until you stop it');
    await vi.waitFor(() => expect(button.disabled).toBe(false));
  });

  it('asks for a date and time before it checks an end it cannot read', async () => {
    const service = mockService();
    await renderWorkflow(service);
    await vi.waitFor(() => expect(service.previewBotEnd).toHaveBeenCalled());
    const checked = service.previewBotEnd.mock.calls.length;

    fireEvent.input(within(endEditor()).getByLabelText('End date'), { target: { value: '' } });

    expect(await within(endEditor()).findByRole('alert')).toHaveProperty('textContent', 'Enter the end’s date and time.');
    expect(within(step('How')).getByText('Needs an end time')).toBeTruthy();
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

  it('passes AXE with the end and its fields on the page', async () => {
    await renderWorkflow();
    await within(endSummary()).findByText(DEFAULT_END.headline);

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
