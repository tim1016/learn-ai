import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeployBotReceipt,
  type DeployBotView,
  type RunAdmissionDecision,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';

const VALIDATION_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'deployment_validation',
  label: 'Deployment Validation',
  explanation: 'Validated canonical decision kernel.',
  validation_case_symbol: 'SPY',
  evidence_status: 'accepted',
  paper_access_state: 'enabled',
  selectable: true,
  admissible_modes: ['dry_run', 'paper'],
  override_explanation: null,
  blocked_explanation: null,
};

const EMA_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'ema_crossover_signal',
  label: 'EMA Crossover Signal',
  explanation: 'Validated EMA(5), EMA(10), and RSI(14) crossover signal.',
  validation_case_symbol: 'SPY',
  evidence_status: 'accepted',
  paper_access_state: 'enabled',
  selectable: true,
  admissible_modes: ['dry_run', 'paper'],
  override_explanation: null,
  blocked_explanation: null,
  params_schema: {
    properties: {
      gap: { type: 'number', default: 0.2, title: 'Crossover gap', description: 'Minimum EMA gap for entry.' },
    },
  },
};

const SMA_OVERRIDE_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'sma_crossover',
  label: 'SMA Crossover',
  explanation: 'Human-validated SMA crossover with evidence-only parity.',
  validation_case_symbol: 'SPY',
  evidence_status: 'evidence_only',
  paper_access_state: 'enabled',
  selectable: true,
  admissible_modes: ['dry_run', 'paper'],
  override_explanation: 'Behavioral evidence is evidence-only and not accepted for deployment.',
  blocked_explanation: null,
};

const BLOCKED_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'rsi_mean_reversion',
  label: 'RSI Mean Reversion',
  explanation: 'Validated RSI mean-reversion entries with ADX confirmation.',
  validation_case_symbol: 'SPY',
  evidence_status: 'blocked',
  paper_access_state: 'enabled',
  selectable: false,
  admissible_modes: ['dry_run'],
  override_explanation: null,
  blocked_explanation: "The audit copy at 'docs/references/rsi-mean-reversion.md' no longer matches its recorded hash.",
};

const BLOCKED_EXPLANATION = BLOCKED_STRATEGY.blocked_explanation;
if (BLOCKED_EXPLANATION === null || BLOCKED_EXPLANATION === undefined) {
  throw new Error('BLOCKED_STRATEGY fixture must carry a blocked_explanation');
}

// #1703: a validated strategy with no registered runtime — visible, but
// admits neither execution mode (unlike BLOCKED_STRATEGY above, which stays
// Dry-Run-admissible because its block is a stale proof, not a missing
// runtime).
const NO_RUNTIME_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'spy_strategy_b',
  label: 'Strategy B',
  explanation: 'Validated RSI-range strategy with no registered runtime yet.',
  validation_case_symbol: 'SPY',
  evidence_status: 'blocked',
  paper_access_state: 'enabled',
  selectable: false,
  admissible_modes: [],
  override_explanation: null,
  blocked_explanation: 'This strategy has no registered live-decision runtime yet.',
};

const NO_RUNTIME_EXPLANATION = NO_RUNTIME_STRATEGY.blocked_explanation;
if (NO_RUNTIME_EXPLANATION === null || NO_RUNTIME_EXPLANATION === undefined) {
  throw new Error('NO_RUNTIME_STRATEGY fixture must carry a blocked_explanation');
}

const DEPLOY_VIEW: DeployBotView = {
  broker: 'alpaca',
  account_id: 'PA9',
  account_mode: 'paper',
  account_label: 'Alpaca paper · PA9',
  evaluated_at_ms: 1_700_000_000_000,
  eligibility: {
    eligible: true,
    reason_code: 'ALPACA_PAPER_DEPLOY_READY',
    headline: 'This Alpaca paper account is eligible.',
    explanation: 'Every ENTER and EXIT is executed through the Alpaca Clerk.',
    next_action: 'Review the ticket and deploy the paper bot.',
  },
  dry_run_eligibility: {
    eligible: true,
    reason_code: 'ALPACA_DRY_RUN_READY',
    headline: 'A zero-broker-write Dry Run is available.',
    explanation: 'A registered strategy and a healthy market-data channel are present.',
    next_action: 'Complete the deployment ticket, review the summary, then start the Dry Run.',
  },
  strategies: [VALIDATION_STRATEGY, EMA_STRATEGY, SMA_OVERRIDE_STRATEGY],
  execution_modes: [
    {
      mode: 'dry_run',
      label: 'Dry Run',
      availability: 'available',
      explanation: 'Real market data with simulated decisions and fills only.',
    },
    { mode: 'paper', label: 'Paper', availability: 'available', explanation: 'Available through the Alpaca Clerk.' },
    { mode: 'live', label: 'Live', availability: 'planned', explanation: 'Live Alpaca execution is planned.' },
  ],
  readiness_checks: [
    {
      gate_id: 'strategy.validation',
      label: 'Strategy validation',
      ready: true,
      scope: 'strategy',
      authority: 'Strategy validation manifest',
      headline: 'Current accepted evidence is present.',
      explanation: 'The selected strategy has a current human validation receipt.',
      evidence_summary: 'Accepted at the current manifest revision.',
      evidence: { verdict: 'accepted_for_deploy' },
      recovery: null,
    },
    {
      gate_id: 'broker.channel',
      label: 'Broker channel',
      ready: true,
      scope: 'broker',
      authority: 'Alpaca channel health',
      headline: 'Trading and account channels are healthy.',
      explanation: 'The current channel observation admits deployment.',
      evidence_summary: 'Account and trading channels are healthy.',
      evidence: { trading: true, account: true },
      recovery: null,
    },
  ],
  sizing_options: [
    {
      preset: 'safe_canary',
      label: 'Safe canary · 1 share',
      explanation: 'Fixed one-share sizing.',
      min_quantity: 1,
      max_quantity: 1,
      default_quantity: 1,
    },
    {
      preset: 'custom',
      label: 'Bounded custom shares',
      explanation: 'Whole shares from 1 through 100.',
      min_quantity: 1,
      max_quantity: 100,
      default_quantity: 1,
    },
  ],
  action_plan_explanation: 'One long stock ENTER and one matching close-leg EXIT.',
  carryover_available: false,
  carryover_label: 'Allow Clerk-proven exposure carryover on STOP',
  carryover_explanation: 'Account policy currently forbids carried exposure.',
  allowed_actions: ['deploy'],
};

const ADMISSION: RunAdmissionDecision = {
  operation: 'START',
  allowed: true,
  reason_code: 'START_ADMITTED',
  explanation: 'The process slot is absent, market data is ready, and the Clerk proves flat custody.',
  next_step: null,
  strategy_instance_id: 'spy-test-01',
  proposed_run_id: 'run-1',
  configuration_hash: 'a'.repeat(64),
  account_id: 'PA9',
  evaluated_at_ms: 1_700_000_000_000,
  fact_ages_ms: { runtime: 5, process: 10, market_data: 20, market_liveness: 20, clerk: 30, program_build: 15 },
  evidence_refs: ['test-admission'],
};

const RECEIPT: DeployBotReceipt = {
  outcome: 'success',
  status: 'deployed',
  receipt_id: 'alpaca-paper-deploy:PA9:spy-test-01:1700000000000',
  recorded_at_ms: 1_700_000_000_000,
  account_id: 'PA9',
  execution_mode: 'paper',
  sizing: { preset: 'safe_canary', quantity: 1 },
  carryover_policy: 'FORBID',
  message: 'spy-test-01 is on duty in Alpaca paper.',
  explanation: 'The deployment binding is durable and Clerk governed.',
  next_action: 'Open the production bot control page.',
  panel_path: '/brokers/alpaca/accounts/PA9/bots/spy-test-01',
  action_plan: {
    on_enter: [{
      leg_id: 'primary',
      instrument: { kind: 'stock', underlying: 'SPY' },
      position: 'long',
      qty_ratio: 1,
    }],
    on_exit: [{ kind: 'close_leg', entry_leg_id: 'primary' }],
  },
  admission: ADMISSION,
  bot: {
    strategy_instance_id: 'spy-test-01',
    broker: 'alpaca',
    symbol: 'SPY',
    mode: 'trade',
    quantity: 1,
    carryover_policy: 'FORBID',
    carryover_checkpoint_exposure: {},
    carryover_checkpoint_config_matches: false,
    running: true,
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    active_run_id: 'run-1',
    duty_outcome: null,
    binding_created_at_ms: 1_700_000_000_000,
    last_transition_at_ms: 1_700_000_000_001,
  },
};

function mockService(
  result: DeployBotReceipt | HttpErrorResponse = RECEIPT,
  view: DeployBotView = DEPLOY_VIEW,
) {
  return {
    getDeployView: vi.fn().mockResolvedValue(view),
    previewStartAdmission: vi.fn().mockResolvedValue(ADMISSION),
    deployBot: result instanceof HttpErrorResponse
      ? vi.fn().mockRejectedValue(result)
      : vi.fn().mockResolvedValue(result),
  };
}

async function renderWorkflow(service = mockService()) {
  const rendered = await render(AlpacaDeployWorkflowComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: service },
    ],
    componentInputs: { accountId: 'PA9' },
  });
  await screen.findByRole('heading', { name: 'Bot binding' });
  return rendered;
}

describe('AlpacaDeployWorkflowComponent', () => {
  it('defaults to a concise trader view without duplicating strategy provenance', async () => {
    await renderWorkflow();

    expect(screen.getAllByText('Deployment Validation').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('✓ Accepted evidence')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Bot binding' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Trading setup' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: 'One share' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: 'Custom shares' })).toBeTruthy();
    expect(screen.queryByText('Safe canary · 1 share')).toBeNull();
    expect(screen.queryByText('Bounded custom shares')).toBeNull();
    expect(screen.queryByText('Alpaca · broker deploy')).toBeNull();
    expect(screen.queryByText('Strategy provenance')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Every deployment gate' })).toBeNull();
    expect(screen.queryByText('One long stock ENTER and one matching close-leg EXIT.')).toBeNull();
    expect(screen.queryByText('Live Alpaca execution is planned.')).toBeNull();
    expect(screen.getByRole('button', {
      name: 'About the trading symbol: One long stock ENTER and one matching close-leg EXIT.',
    })).toBeTruthy();
    expect(screen.getByRole('button', {
      name: 'About Live: Live Alpaca execution is planned.',
    })).toBeTruthy();
  });

  it('shows admission gates without a lens toggle, ready ones collapsed', async () => {
    await renderWorkflow();

    expect(screen.queryByRole('tab', { name: 'Operator' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Trader' })).toBeNull();

    const readyHeader = screen.getByRole('button', { name: /Strategy validation/ });
    expect(readyHeader.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(readyHeader);

    expect(readyHeader.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('Accepted at the current manifest revision.')).toBeTruthy();
  });

  it('never writes a deployLens query param', async () => {
    const { fixture } = await renderWorkflow();
    const router = fixture.debugElement.injector.get(Router);

    fireEvent.click(screen.getByRole('button', { name: /Strategy validation/ }));

    expect(router.url).not.toContain('deployLens');
  });

  it('opens the failed admission gate and folds the blocker into the account banner', async () => {
    const blockedView: DeployBotView = {
      ...DEPLOY_VIEW,
      eligibility: {
        ...DEPLOY_VIEW.eligibility,
        eligible: false,
        headline: 'Deployment is blocked until Clerk channels are healthy.',
        explanation: 'Market Data is unhealthy, Execution is healthy.',
      },
      readiness_checks: DEPLOY_VIEW.readiness_checks.map((check) =>
        check.gate_id === 'broker.channel'
          ? {
              ...check,
              ready: false,
              recovery: 'Restore the market-data channel and refresh deployment readiness.',
            }
          : check,
      ),
      allowed_actions: [],
    };
    await renderWorkflow(mockService(RECEIPT, blockedView));

    // The blocked verdict now headlines the always-visible admission column.
    expect(screen.getByText('Blocked · 1 of 2 gates')).toBeTruthy();
    expect(screen.getByText('Market Data is unhealthy, Execution is healthy.')).toBeTruthy();

    const readyHeader = screen.getByRole('button', { name: /Strategy validation/ });
    const blockedHeader = screen.getByRole('button', { name: /Broker channel/ });
    expect(readyHeader.getAttribute('aria-expanded')).toBe('false');
    expect(blockedHeader.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('link', { name: 'Open account recovery' }).getAttribute('href'))
      .toBe('/brokers/alpaca?lens=operator');
  });

  it('changes the validated strategy and submits the selected strategy key', async () => {
    const service = mockService();
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'ema-paper-01' },
    });
    fireEvent.change(screen.getByLabelText('Deployment strategy'), {
      target: { value: 'ema_crossover_signal' },
    });

    expect(screen.getAllByText('EMA Crossover Signal').length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(EMA_STRATEGY.explanation)).toBeNull();
    expect(screen.getByRole('button', {
      name: `About EMA Crossover Signal: ${EMA_STRATEGY.explanation}`,
    })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'View validation' }).getAttribute('href'))
      .toBe('/strategy-validation?strategy=ema_crossover_signal');

    fireEvent.click(screen.getByRole('button', { name: 'Deploy paper bot' }));

    await vi.waitFor(() => expect(service.deployBot).toHaveBeenCalledOnce());
    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).strategy_key)
      .toBe('ema_crossover_signal');
  });

  it('renders a strategy parameter seeded from its registered default and flags an edited value, then submits it', async () => {
    const service = mockService();
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'ema-paper-params-01' },
    });
    fireEvent.change(screen.getByLabelText('Deployment strategy'), {
      target: { value: 'ema_crossover_signal' },
    });

    const gapInput = screen.getByRole('textbox', { name: 'Crossover gap' }) as HTMLInputElement;
    expect(gapInput.value).toBe('0.2');
    expect(screen.queryByText('differs from default')).toBeNull();

    fireEvent.change(gapInput, { target: { value: '5' } });

    expect(await screen.findByText('differs from default')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Deploy paper bot' }));

    await vi.waitFor(() => expect(service.deployBot).toHaveBeenCalledOnce());
    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).parameters).toEqual({ gap: 5 });
  });

  it('blocks deployment while a strategy parameter is showing unparseable text', async () => {
    const service = mockService();
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'ema-paper-params-02' },
    });
    fireEvent.change(screen.getByLabelText('Deployment strategy'), {
      target: { value: 'ema_crossover_signal' },
    });

    const deployButton = screen.getByRole('button', { name: 'Deploy paper bot' }) as HTMLButtonElement;
    expect(deployButton.disabled).toBe(false);

    const gapInput = screen.getByRole('textbox', { name: 'Crossover gap' }) as HTMLInputElement;
    fireEvent.change(gapInput, { target: { value: 'not-a-number' } });

    expect(deployButton.disabled).toBe(true);
    fireEvent.click(deployButton);
    expect(service.deployBot).not.toHaveBeenCalled();

    fireEvent.change(gapInput, { target: { value: '0.4' } });
    expect(deployButton.disabled).toBe(false);
  });

  it('deploys an evidence-only strategy to Paper with no override required, verdict shown informationally', async () => {
    // #1702: Paper gates on the human-validated flag alone. The behavioral
    // verdict still renders (informational), but nothing blocks submission
    // and no evidence_override is sent.
    const service = mockService();
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'sma-paper-01' },
    });
    fireEvent.change(screen.getByLabelText('Deployment strategy'), {
      target: { value: 'sma_crossover' },
    });

    expect(screen.queryByRole('heading', { name: 'Dangerous human override' })).toBeNull();
    expect(screen.getAllByText('Evidence only').length).toBeGreaterThanOrEqual(1);
    const deployButton = screen.getByRole('button', { name: 'Deploy paper bot' }) as HTMLButtonElement;
    expect(deployButton.disabled).toBe(false);

    fireEvent.click(deployButton);

    await vi.waitFor(() => expect(service.deployBot).toHaveBeenCalledOnce());
    const body = service.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(body.strategy_key).toBe('sma_crossover');
    expect(body).not.toHaveProperty('evidence_override');
  });

  it('names the invalid bot name instead of asking for already-complete trading inputs', async () => {
    const service = mockService(RECEIPT, {
      ...DEPLOY_VIEW,
      eligibility: {
        ...DEPLOY_VIEW.eligibility,
        next_action: 'Choose the symbol and sizing, then deploy the bot.',
      },
    });
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'RSI MEAN reversion' },
    });
    fireEvent.change(screen.getByLabelText('Deployment strategy'), {
      target: { value: 'sma_crossover' },
    });

    const deployButton = screen.getByRole('button', { name: 'Deploy paper bot' });
    expect(deployButton.hasAttribute('disabled')).toBe(true);
    expect(deployButton.getAttribute('aria-describedby')).toBe('deploy-submit-guidance');
    expect(screen.getByText('Fix the bot name before deployment.')).toBeTruthy();
    expect(screen.queryByText('Choose the symbol and sizing, then deploy the bot.')).toBeNull();

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'rsi-mean-reversion' },
    });

    expect(deployButton.hasAttribute('disabled')).toBe(false);
    expect(screen.getByText('Ready to deploy this bot.')).toBeTruthy();
  });

  it('initializes the validated strategy from the strategy-key deep link', async () => {
    const queryParamMap = convertToParamMap({ strategy_key: 'ema_crossover_signal' });
    await render(AlpacaDeployWorkflowComponent, {
      providers: [
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: { queryParamMap: of(queryParamMap), snapshot: { queryParamMap } },
        },
        { provide: BrokerV2PanelService, useValue: mockService() },
      ],
      componentInputs: { accountId: 'PA9' },
    });
    await screen.findByText(DEPLOY_VIEW.eligibility.headline);

    expect((screen.getByLabelText('Deployment strategy') as HTMLSelectElement).value)
      .toBe('ema_crossover_signal');
    expect(screen.getByRole('link', { name: 'View validation' }).getAttribute('href'))
      .toBe('/strategy-validation?strategy=ema_crossover_signal');
  });

  it('renders a blocked strategy selectable for Dry Run while Paper stays disabled with the backend reason', async () => {
    // #1702: a blocked row is Dry-Run-admissible even though its proof no
    // longer verifies for Paper — the dropdown no longer disables it, and
    // the backend-authored reason surfaces on the Paper option instead.
    const queryParamMap = convertToParamMap({ strategy_key: 'rsi_mean_reversion' });
    const service = mockService(RECEIPT, {
      ...DEPLOY_VIEW,
      strategies: [VALIDATION_STRATEGY, EMA_STRATEGY, SMA_OVERRIDE_STRATEGY, BLOCKED_STRATEGY],
    });
    await render(AlpacaDeployWorkflowComponent, {
      providers: [
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: { queryParamMap: of(queryParamMap), snapshot: { queryParamMap } },
        },
        { provide: BrokerV2PanelService, useValue: service },
      ],
      componentInputs: { accountId: 'PA9' },
    });
    await screen.findByText(DEPLOY_VIEW.eligibility.headline);
    await userEvent.type(screen.getByLabelText('Bot name'), 'rsi-blocked-01');

    const select = screen.getByLabelText<HTMLSelectElement>('Deployment strategy');
    expect(select.value).toBe('rsi_mean_reversion');
    const blockedOption = screen.getByRole<HTMLOptionElement>('option', { name: /RSI Mean Reversion/ });
    expect(blockedOption.disabled).toBe(false);
    expect(blockedOption.textContent).toContain('blocked');
    expect(screen.getAllByText('Blocked').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(BLOCKED_EXPLANATION).length).toBeGreaterThanOrEqual(1);

    const paperRadio = screen.getByRole<HTMLInputElement>('radio', { name: /Paper/ });
    expect(paperRadio.disabled).toBe(true);
    expect(screen.getByRole('button', {
      name: `About Paper: ${BLOCKED_EXPLANATION}`,
    })).toBeTruthy();

    const deployButton = screen.getByRole<HTMLButtonElement>('button', {
      name: 'Deploy paper bot',
      description: BLOCKED_EXPLANATION,
    });
    expect(deployButton.disabled).toBe(true);
    const launchFooter = deployButton.closest<HTMLElement>('.deploy-footer');
    if (launchFooter === null) throw new Error('deploy-footer container not found');
    expect(within(launchFooter).getByText('Blocked')).toBeTruthy();

    fireEvent.click(screen.getByRole('radio', { name: /Dry Run/i }));

    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Deploy dry run bot' }).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Deploy dry run bot' }));

    await vi.waitFor(() => expect(service.deployBot).toHaveBeenCalledOnce());
    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).execution_mode).toBe('dry_run');
  });

  it('shows the blocked reason for a single blocked strategy without a strategy selector', async () => {
    const queryParamMap = convertToParamMap({ strategy_key: 'rsi_mean_reversion' });
    const service = mockService(RECEIPT, { ...DEPLOY_VIEW, strategies: [BLOCKED_STRATEGY] });
    await render(AlpacaDeployWorkflowComponent, {
      providers: [
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: { queryParamMap: of(queryParamMap), snapshot: { queryParamMap } },
        },
        { provide: BrokerV2PanelService, useValue: service },
      ],
      componentInputs: { accountId: 'PA9' },
    });
    await screen.findByText(DEPLOY_VIEW.eligibility.headline);
    await userEvent.type(screen.getByLabelText('Bot name'), 'rsi-blocked-01');

    expect(screen.queryByLabelText('Deployment strategy')).toBeNull();
    expect(screen.getAllByText(BLOCKED_STRATEGY.label).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Blocked').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(BLOCKED_EXPLANATION).length).toBeGreaterThanOrEqual(1);

    const deployButton = screen.getByRole<HTMLButtonElement>('button', {
      name: 'Deploy paper bot',
      description: BLOCKED_EXPLANATION,
    });
    expect(deployButton.disabled).toBe(true);
  });

  it('lets the operator inspect a no-runtime strategy while disabling both execution modes', async () => {
    // #1703: unlike a stale-proof blocked row (BLOCKED_STRATEGY, still
    // Dry-Run-admissible), a no-runtime row admits neither mode. The row
    // remains selectable in the catalog so its backend-authored reason is
    // inspectable; only the execution modes are disabled.
    const service = mockService(RECEIPT, {
      ...DEPLOY_VIEW,
      strategies: [VALIDATION_STRATEGY, EMA_STRATEGY, SMA_OVERRIDE_STRATEGY, NO_RUNTIME_STRATEGY],
    });
    await renderWorkflow(service);

    const select = screen.getByLabelText<HTMLSelectElement>('Deployment strategy');
    const noRuntimeOption = screen.getByRole<HTMLOptionElement>('option', { name: /Strategy B/ });
    expect(select.value).toBe('deployment_validation');
    expect(noRuntimeOption.disabled).toBe(false);

    fireEvent.change(select, {
      target: { value: 'spy_strategy_b' },
    });
    await userEvent.type(screen.getByLabelText('Bot name'), 'strategy-b-01');

    expect(select.value).toBe('spy_strategy_b');
    expect(screen.getAllByText('Blocked').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(NO_RUNTIME_EXPLANATION).length).toBeGreaterThanOrEqual(1);

    const paperRadio = screen.getByRole<HTMLInputElement>('radio', { name: /Paper/ });
    const dryRunRadio = screen.getByRole<HTMLInputElement>('radio', { name: /Dry Run/i });
    expect(paperRadio.disabled).toBe(true);
    expect(dryRunRadio.disabled).toBe(true);

    fireEvent.click(dryRunRadio);

    // Clicking a disabled-for-this-strategy mode is a no-op: the ticket
    // stays on the default 'paper' mode, so the button label is unchanged.
    const deployButton = screen.getByRole<HTMLButtonElement>('button', {
      name: 'Deploy paper bot',
      description: NO_RUNTIME_EXPLANATION,
    });
    expect(deployButton.disabled).toBe(true);
  });

  it('auto-selects the first selectable strategy, skipping a blocked one, when no deep link is given', async () => {
    const service = mockService(RECEIPT, {
      ...DEPLOY_VIEW,
      strategies: [BLOCKED_STRATEGY, EMA_STRATEGY, SMA_OVERRIDE_STRATEGY],
    });
    await renderWorkflow(service);

    expect((screen.getByLabelText('Deployment strategy') as HTMLSelectElement).value)
      .toBe('ema_crossover_signal');
    expect(screen.getByText('✓ Accepted evidence')).toBeTruthy();
  });

  it('preserves a trader-edited symbol when the validated strategy changes', async () => {
    await renderWorkflow();

    fireEvent.input(screen.getByPlaceholderText('SPY'), {
      target: { value: 'QQQ' },
    });
    fireEvent.change(screen.getByLabelText('Deployment strategy'), {
      target: { value: 'ema_crossover_signal' },
    });

    expect((screen.getByPlaceholderText('SPY') as HTMLInputElement).value).toBe('QQQ');
  });

  it('submits only the closed paper canary command and renders its durable receipt', async () => {
    const service = mockService();
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'spy-test-01' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy paper bot' }));
    await screen.findByText(RECEIPT.receipt_id);

    const body = service.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(service.previewStartAdmission).toHaveBeenCalledWith('alpaca', 'PA9', body);
    expect(body).toEqual({
      strategy_instance_id: 'spy-test-01',
      strategy_key: 'deployment_validation',
      symbol: 'SPY',
      sizing: { preset: 'safe_canary', quantity: 1 },
      execution_mode: 'paper',
      carryover_policy: 'FORBID',
      parameters: {},
    });
    expect(body).not.toHaveProperty('mode');
    expect(screen.getByText(RECEIPT.receipt_id)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open bot control' }).getAttribute('href'))
      .toBe(RECEIPT.panel_path);
    expect(screen.queryByRole('tab')).toBeNull();
  });

  it('submits a first-class Dry Run mode that cannot opt into carryover', async () => {
    const service = mockService({
      ...RECEIPT,
      execution_mode: 'dry_run',
      message: 'spy-dry-01 is on duty in Dry Run.',
      bot: { ...RECEIPT.bot, strategy_instance_id: 'spy-dry-01', mode: 'dry_run' },
    });
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'spy-dry-01' },
    });
    fireEvent.click(screen.getByRole('radio', { name: /Dry Run Available/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Deploy dry run bot' }));

    await vi.waitFor(() => expect(service.deployBot).toHaveBeenCalledOnce());
    const body = service.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(body.execution_mode).toBe('dry_run');
    expect(body.carryover_policy).toBe('FORBID');
    expect(screen.getByText(/^Dry Run · /)).toBeTruthy();
  });

  it('renders a backend-authored Start refusal without dispatching deployment', async () => {
    const denied = {
      ...ADMISSION,
      allowed: false,
      reason_code: 'MARKET_DATA_STALE',
      explanation: 'The required market-data feed is not proven ready for this run.',
      next_step: 'Restore fresh market data before Start.',
    } satisfies RunAdmissionDecision;
    const service = mockService();
    service.previewStartAdmission.mockResolvedValue(denied);
    const { fixture } = await renderWorkflow(service);
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;
    component['ticket'].update((ticket) => ({ ...ticket, instanceId: 'spy-stale' }));

    await component['submit']();
    fixture.detectChanges();

    expect(screen.getByText('Start blocked')).toBeTruthy();
    expect(screen.getByText(denied.explanation)).toBeTruthy();
    expect(screen.getByText('Runner safety')).toBeTruthy();
    expect(screen.getByText('Market data')).toBeTruthy();
    expect(service.deployBot).not.toHaveBeenCalled();
  });

  it('removes an obsolete Start decision when the deployment ticket changes', async () => {
    const denied = { ...ADMISSION, allowed: false } satisfies RunAdmissionDecision;
    const service = mockService();
    service.previewStartAdmission.mockResolvedValue(denied);
    const { fixture } = await renderWorkflow(service);
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;
    component['ticket'].update((ticket) => ({ ...ticket, instanceId: 'spy-stale' }));
    await component['submit']();
    fixture.detectChanges();

    fireEvent.input(screen.getByPlaceholderText('SPY'), { target: { value: 'QQQ' } });
    fixture.detectChanges();

    expect(screen.queryByText('Start blocked')).toBeNull();
  });

  it('discards a preview response when the deployment ticket changes in flight', async () => {
    let resolvePreview!: (decision: RunAdmissionDecision) => void;
    const service = mockService();
    service.previewStartAdmission.mockReturnValue(new Promise((resolve) => {
      resolvePreview = resolve;
    }));
    const { fixture } = await renderWorkflow(service);
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;
    component['ticket'].update((ticket) => ({ ...ticket, instanceId: 'spy-race' }));

    const submission = component['submit']();
    await vi.waitFor(() => expect(service.previewStartAdmission).toHaveBeenCalledOnce());
    fireEvent.input(screen.getByPlaceholderText('SPY'), { target: { value: 'QQQ' } });
    resolvePreview(ADMISSION);
    await submission;
    fixture.detectChanges();

    expect(service.deployBot).not.toHaveBeenCalled();
    expect(screen.queryByText('Start allowed')).toBeNull();
  });

  it('submits bounded custom whole-share sizing', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;

    component['ticket'].update((ticket) => ({
      ...ticket,
      instanceId: 'spy-test-02',
      sizingPreset: 'custom',
      quantity: 7,
    }));
    await component['submit']();

    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).sizing)
      .toEqual({ preset: 'custom', quantity: 7 });
  });

  it('exposes available Clerk-proven carryover to the trader and submits the opt-in', async () => {
    const carryoverView: DeployBotView = {
      ...DEPLOY_VIEW,
      carryover_available: true,
      carryover_explanation: 'Keep exactly attributed exposure only after a durable Clerk checkpoint.',
    };
    const service = mockService(RECEIPT, carryoverView);
    await renderWorkflow(service);

    fireEvent.click(screen.getByRole('checkbox', {
      name: /Allow Clerk-proven exposure carryover on STOP/i,
    }));
    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'spy-carryover' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy paper bot' }));
    await vi.waitFor(() => expect(service.deployBot).toHaveBeenCalledOnce());

    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).carryover_policy)
      .toBe('ALLOW');
  });

  it('renders only the backend-authored readiness time', async () => {
    await renderWorkflow();

    expect(screen.queryByText('now')).toBeNull();
  });

  /**
   * `deployView` is parameterized by `accountId` alone. A ticket edit clears the
   * prior admission decision but never re-evaluates these account gates, so the
   * admission timestamp must not be presented as a per-edit recheck.
   */
  it('does not claim admission is re-checked on every edit', async () => {
    const { fixture } = await renderWorkflow();

    fireEvent.input(screen.getByPlaceholderText('SPY'), { target: { value: 'QQQ' } });
    fixture.detectChanges();

    expect(screen.queryByText(/on every edit/)).toBeNull();
    expect(screen.getByText(/Account evaluated/)).toBeTruthy();
  });

  it('normalizes text fields before Signal Form validation and submission gating', async () => {
    const { fixture } = await renderWorkflow();
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;

    fireEvent.input(screen.getByPlaceholderText('alpaca-spy-01'), {
      target: { value: ' spy-validation-01 ' },
    });
    fireEvent.input(screen.getByPlaceholderText('SPY'), {
      target: { value: ' brk.b ' },
    });
    component['ticketForm'].instanceId().markAsTouched();
    component['ticketForm'].symbol().markAsTouched();
    fixture.detectChanges();

    expect(component['ticket']().instanceId).toBe('spy-validation-01');
    expect(component['ticket']().symbol).toBe('BRK.B');
    expect(component['ticketForm']().valid()).toBe(true);
    expect(screen.getByRole('button', { name: 'Deploy paper bot' }).hasAttribute('disabled'))
      .toBe(false);
  });

  it('distinguishes a stale-state conflict from an unknown outcome', async () => {
    const refusedAdmission = {
      ...ADMISSION,
      allowed: false,
      reason_code: 'CUSTODY_HOLD_ACTIVE',
      explanation: 'The Clerk entered a hold after the preview.',
      next_step: 'Resolve the Clerk hold before Start.',
    } satisfies RunAdmissionDecision;
    const error = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          outcome: 'conflict',
          receipt_id: 'deploy-conflict-1',
          recorded_at_ms: 1_700_000_000_002,
          message: 'Deployment readiness changed.',
          why: 'The Clerk entered a hold after the page loaded.',
          next_action: 'Reload readiness and resolve the hold.',
          admission: refusedAdmission,
        },
      },
    });
    const { fixture } = await renderWorkflow(mockService(error));
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;
    component['ticket'].update((ticket) => ({ ...ticket, instanceId: 'spy-conflict' }));

    await component['submit']();
    fixture.detectChanges();

    expect(screen.getByText('State changed before launch')).toBeTruthy();
    expect(screen.getByText('The Clerk entered a hold after the page loaded.')).toBeTruthy();
    expect(screen.getByText('deploy-conflict-1')).toBeTruthy();
    expect(screen.getByText('Start blocked')).toBeTruthy();
    expect(screen.getByText(refusedAdmission.explanation)).toBeTruthy();
  });
});
