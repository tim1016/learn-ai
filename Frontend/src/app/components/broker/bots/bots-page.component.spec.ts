import { provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountTriageResponse } from '../../../api/account-reconciliation.types';
import type {
  BotDailyLifecycleProjection,
  BotCatalogResponse,
  BotCatalogRow,
  BotLifecycleAction,
  BotLifecycleCondition,
  BotRollCallResponse,
} from '../../../api/live-instances.types';
import type {
  CohortBatchLaunchStatus,
  CohortEvidenceSummary,
} from '../../../api/cohort-batch-launch.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import {
  makeCleanAccountTriage,
  makeFrozenAccountTriage,
} from '../testing/account-triage-fixtures';
import { BotsPageComponent } from './bots-page.component';

const OLD_RUN = 1_700_000_000_000;
const NEW_RUN = 1_800_000_000_000;
const OFFER_EXPIRES_AT = 1_800_000_300_000;
const COHORT_EVIDENCE: CohortEvidenceSummary = {
  sample_count: 0,
  cadence_ms: 5_000,
  healthy_overlap_ms: 0,
  verdict: 'unknown',
  reason: 'COHORT_EVIDENCE_MISSING',
  source: 'account_event.cohort_evidence_sample',
  members: [],
};

function action(
  overrides: Partial<BotLifecycleAction> & Pick<BotLifecycleAction, 'id' | 'label'>,
): BotLifecycleAction {
  return {
    enabled: true,
    reason: null,
    offer_id: null,
    expires_at_ms: null,
    ...overrides,
  };
}

function lifecycle(
  overrides: Partial<BotDailyLifecycleProjection> = {},
): BotDailyLifecycleProjection {
  return {
    phase: 'OFF_DUTY',
    presence_label: 'Off duty',
    display_status: 'Ready',
    attention_badge: 'Ready',
    reason: 'Roll call can offer a fresh start.',
    on_roster: true,
    active_run_id: null,
    latest_run_id: 'run-live-idle-spy',
    drift_detected: false,
    primary_action: action({
      id: 'confirm_start',
      label: 'Start',
      offer_id: 'offer-live-idle-spy',
      expires_at_ms: OFFER_EXPIRES_AT,
    }),
    ambient_actions: [
      action({
        id: 'take_off_roster',
        label: 'Take off roster',
      }),
      action({
        id: 'retire_replace',
        label: 'Retire & Replace',
      }),
    ],
    ...overrides,
  };
}

function accountStaleCondition(): BotLifecycleCondition {
  return {
    scope: 'account',
    severity: 'warning',
    title: 'Account evidence stale',
    detail: 'Receipt acct-recon-DU1234567 expired before this triage snapshot.',
    owner_label: 'Account DU1234567',
    cure_action: 'reconcile_now',
    cure_label: 'Run account reconcile',
  };
}

function bot(overrides: Partial<BotCatalogRow> = {}): BotCatalogRow {
  return {
    strategy_instance_id: 'live-idle-spy',
    name: 'live-idle-spy',
    description: null,
    status_label: 'Ready',
    status_detail: 'Roll call can offer a fresh start.',
    status_tone: 'positive',
    only_fresh_run_available: false,
    needs_attention: false,
    trading_mode: 'live',
    symbols: ['SPY'],
    engine: 'live-engine',
    engine_asset_class: 'equity',
    created_at_ms: OLD_RUN - 1_000,
    updated_at_ms: OLD_RUN - 500,
    last_run_at_ms: OLD_RUN,
    last_run_label: 'Clean',
    last_run_result: 'CLEAN',
    last_run_detail: 'Previous run exited normally.',
    process_state: 'IDLE',
    desired_state: 'PAUSED',
    readiness_verdict: 'READY',
    daily_lifecycle: lifecycle(),
    start_request: {
      readonly: false,
      hydrate_policy: 'require',
      strategy: 'spy_ema',
      max_orders_per_day: 2,
      ibkr_host: '127.0.0.1',
    },
    attendance: [
      {
        session_date: '2026-07-08',
        status: 'rested',
        label: 'Rested',
        receipt_ref: null,
      },
      {
        session_date: '2026-07-07',
        status: 'clean',
        label: 'Clean exit',
        receipt_ref: 'clean_exit:run-live-idle-spy',
      },
    ],
    metrics: {
      pnl: {
        realized: null,
        unrealized: 12.5,
        total: null,
      },
      trade_count: null,
      current_exposure: 'Flat',
      open_positions: 0,
      error_count: 0,
    },
    ...overrides,
  };
}

function catalog(bots: BotCatalogRow[]): BotCatalogResponse {
  return {
    bots,
    roll_call: {
      ready: bots.filter((row) => row.daily_lifecycle.display_status === 'Ready').length,
      off_roster: bots.filter((row) => row.daily_lifecycle.display_status === 'Off roster').length,
      sick_bay: bots.filter((row) => row.daily_lifecycle.display_status === 'Sick bay').length,
      on_duty: bots.filter((row) => row.daily_lifecycle.display_status === 'On duty').length,
      off_duty: bots.filter((row) => row.daily_lifecycle.display_status === 'Off duty').length,
      retired: bots.filter((row) => row.daily_lifecycle.display_status === 'Retired').length,
      generated_at_ms: NEW_RUN,
      session_date: '2026-07-08',
      effective_stop_ms: OFFER_EXPIRES_AT,
    },
    evening_report: {
      session_date: '2026-07-08',
      generated_at_ms: NEW_RUN,
      clean_exits: 1,
      rested: 1,
      sick: 1,
      retired: 0,
      summary: '1 clean exit · 1 rested · 1 in sick bay · 0 retired',
      rows: bots.map((row) => ({
        strategy_instance_id: row.strategy_instance_id,
        label: row.attendance[0]?.label ?? 'Rested',
        status: row.attendance[0]?.status ?? 'rested',
        receipt_ref: row.attendance[0]?.receipt_ref ?? null,
      })),
    },
  };
}

class FakeLiveRunsService {
  getBotCatalog = vi.fn<() => Promise<BotCatalogResponse>>();
  deleteBot = vi.fn<(instanceId: string, request?: unknown) => Promise<unknown>>();
  runRollCall = vi.fn<() => Promise<BotRollCallResponse>>();
  launchCohort = vi.fn();
  startHostRunner = vi.fn<(runId: string, request: unknown) => Promise<unknown>>();
  deployPreflight = vi.fn();
  getLatestCohortBatchLaunch = vi.fn();
  getInstanceStatus = vi.fn();
  getStatus = vi.fn();
}

class FakeBrokerService {
  accountTriage = vi.fn<(accountId: string) => Promise<AccountTriageResponse>>();
  reconcileAccount = vi.fn<(accountId: string) => Promise<unknown>>();
}

class FakeBrokerHealthService {
  readonly health = signal({
    connected: true,
    is_paper: true,
    account_id: 'DU1234567',
    mode: 'paper' as const,
    host: '127.0.0.1',
    port: 4002,
    client_id: 1,
    connection_state: 'connected' as const,
  });
  refresh = vi.fn(async () => undefined);
}

async function setup(options: { triage?: AccountTriageResponse } = {}) {
  const service = new FakeLiveRunsService();
  const broker = new FakeBrokerService();
  const health = new FakeBrokerHealthService();
  broker.accountTriage.mockResolvedValue(options.triage ?? cleanTriage());
  broker.reconcileAccount.mockResolvedValue({});
  service.getBotCatalog.mockResolvedValue(
    catalog([
      bot(),
      bot({
        strategy_instance_id: 'live-running-aapl',
        name: 'live-running-aapl',
        symbols: ['AAPL'],
        last_run_at_ms: NEW_RUN,
        needs_attention: true,
        status_label: 'Sick bay',
        status_detail: '1 condition needs a cure before start.',
        status_tone: 'danger',
        last_run_label: 'Exited with error',
        last_run_result: 'EXITED_WITH_ERROR',
        last_run_detail: 'Previous run exited with an error: runtime exception. Exit code 1.',
        process_state: 'RUNNING',
        readiness_verdict: 'DEGRADED',
        daily_lifecycle: lifecycle({
          display_status: 'Sick bay',
          attention_badge: 'Sick bay',
          reason: '1 condition needs a cure before start.',
          conditions: [accountStaleCondition()],
          primary_action: null,
        }),
        start_request: null,
        attendance: [
          {
            session_date: '2026-07-08',
            status: 'sick',
            label: 'Sick bay',
            receipt_ref: 'condition:broker_disconnected',
          },
        ],
        metrics: {
          pnl: { realized: null, unrealized: -4, total: null },
          trade_count: null,
          current_exposure: 'AAPL 10',
          open_positions: 1,
          error_count: 1,
        },
      }),
      bot({
        strategy_instance_id: 'paper-msft',
        name: 'paper-msft',
        trading_mode: 'paper',
        symbols: ['MSFT'],
        last_run_at_ms: NEW_RUN,
        process_state: 'RUNNING',
        status_label: 'On duty',
        status_tone: 'positive',
        daily_lifecycle: lifecycle({
          phase: 'ON_DUTY',
          presence_label: 'On duty',
          display_status: 'On duty',
          attention_badge: null,
          reason: null,
          active_run_id: 'run-paper-msft',
          primary_action: action({
            id: 'end_day_now',
            label: 'End day now',
          }),
          ambient_actions: [
            action({
              id: 'take_off_roster',
              label: 'Take off roster',
            }),
          ],
        }),
        start_request: null,
        attendance: [
          {
            session_date: '2026-07-08',
            status: 'clean',
            label: 'Clean exit',
            receipt_ref: 'clean_exit:run-paper-msft',
          },
        ],
      }),
    ]),
  );
  service.runRollCall.mockResolvedValue({
    summary: {
      ready: 1,
      off_roster: 0,
      sick_bay: 1,
      on_duty: 1,
      off_duty: 0,
      retired: 0,
      generated_at_ms: NEW_RUN,
      session_date: '2026-07-08',
      effective_stop_ms: OFFER_EXPIRES_AT,
    },
    offers: [
      {
        offer_id: 'offer-live-idle-spy',
        strategy_instance_id: 'live-idle-spy',
        run_id: 'run-live-idle-spy',
        session_date: '2026-07-08',
        issued_at_ms: NEW_RUN,
        expires_at_ms: OFFER_EXPIRES_AT,
      },
    ],
  });
  service.startHostRunner.mockResolvedValue({
    accepted: true,
    process: {
      state: 'running',
      pid: 123,
      bound_run_id: 'run-live-idle-spy',
      started_at_ms: NEW_RUN,
    },
  });
  const cohortLaunchResponse: CohortBatchLaunchStatus = {
    schema_version: 1,
    account_id: 'DU1234567',
    cohort_id: 'paper-validation-test',
    member_strategy_instance_ids: ['live-idle-spy', 'live-running-aapl'],
    window_start_ms: NEW_RUN,
    window_end_ms: NEW_RUN + 300_000,
    authorized_by: 'local-operator',
    authorized_recorded_at_ms: NEW_RUN,
    outcomes_state: 'recorded',
    outcomes: [
      {
        strategy_instance_id: 'live-idle-spy',
        state: 'accepted',
        reason: 'START_ACCEPTED',
        next_safe_action: 'Monitor the bot receipt state and account exposure.',
      },
      {
        strategy_instance_id: 'live-idle-qqq',
        state: 'accepted',
        reason: 'START_ACCEPTED',
        next_safe_action: 'Monitor the bot receipt state and account exposure.',
      },
    ],
    outcomes_recorded_at_ms: NEW_RUN,
    outcomes_error: null,
    evidence: COHORT_EVIDENCE,
  };
  service.launchCohort.mockResolvedValue(cohortLaunchResponse);
  service.deployPreflight.mockResolvedValue({ ready: true, blockers: [] });
  service.getLatestCohortBatchLaunch.mockResolvedValue(null);
  service.deleteBot.mockResolvedValue({
    strategy_instance_id: 'paper-msft',
    mode: 'soft',
    deleted_at_ms: NEW_RUN,
    deleted_by: 'operator',
    reason: 'Deleted from Bots page',
    deleted_run_ids: ['run-paper-msft'],
    marker_path: '/tmp/live_state/paper-msft/bot_deletion.json',
    hidden_from_catalog: true,
  });

  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [BotsPageComponent],
    providers: [
      provideZonelessChangeDetection(),
      provideRouter([]),
      { provide: LiveRunsService, useValue: service },
      { provide: BrokerService, useValue: broker },
      { provide: BrokerHealthService, useValue: health },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(BotsPageComponent);
  await settle(fixture);
  return { fixture, service, broker, health, router: TestBed.inject(Router) };
}

async function settle(fixture: ComponentFixture<BotsPageComponent>): Promise<void> {
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
}

function cleanTriage(): AccountTriageResponse {
  return makeCleanAccountTriage({ generatedAtMs: NEW_RUN });
}

function frozenTriage(): AccountTriageResponse {
  return makeFrozenAccountTriage({
    generatedAtMs: NEW_RUN,
    conditionOptions: {
      generatedAtMs: NEW_RUN,
      affectedStrategyInstanceIds: ['live-running-aapl'],
    },
    freezeBanner: {
      headline: 'Account sick bay is gating new starts.',
      detail: 'Run account reconciliation and clear the active account freeze before deploying.',
    },
    clearFreezeActionable: false,
  });
}

describe('BotsPageComponent', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('sorts live bots for operational triage', async () => {
    const { fixture } = await setup();

    expect(fixture.componentInstance.liveBots().map((row) => row.name)).toEqual([
      'live-running-aapl',
      'live-idle-spy',
    ]);
  });

  it('filters by global search and server-authored triage fields', async () => {
    const { fixture } = await setup();
    fixture.componentInstance.searchQuery.set('aapl');
    fixture.componentInstance.setAttentionFilter('needs-attention');
    fixture.componentInstance.setLifecycleFilter('Sick bay');
    fixture.detectChanges();

    expect(fixture.componentInstance.liveBots().map((row) => row.name)).toEqual([
      'live-running-aapl',
    ]);
    expect(fixture.componentInstance.paperBots()).toEqual([]);
  });

  it('separates live and paper bots into mode tabs', async () => {
    const { fixture } = await setup();

    expect(fixture.componentInstance.activeModeTab()).toBe('paper');
    expect(fixture.componentInstance.activeTabCount()).toBe(1);
    expect(fixture.componentInstance.paperBots().map((row) => row.name)).toEqual([
      'paper-msft',
    ]);

    fixture.componentInstance.setActiveModeTab('live');
    fixture.detectChanges();

    expect(fixture.componentInstance.activeTabCount()).toBe(2);
  });

  it('renders table and mobile card views from the same bot rows', async () => {
    const { fixture } = await setup();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';

    expect(text).toContain('live-running-aapl');
    expect(text).toContain('AAPL 10');
    expect(text).toContain('Sick bay');
    expect(text).toContain('Exited with error');
    expect(text).not.toContain('DEGRADED');
    expect(text).not.toContain('BLOCKED');
    expect(text).not.toContain('Fresh run only');
    expect(text).not.toContain('RUNNING');
    expect(text).not.toContain('Needs operator review');
    expect(text).not.toContain('Desired state has no durable intent.');
    expect(text).toContain('1 ready · 1 on duty · 1 in sick bay · 0 off roster · 0 retired');
    expect(fixture.nativeElement.querySelectorAll('[data-testid="bot-attendance-strip"]').length)
      .toBeGreaterThan(0);
    const sickStatusCell = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.status-cell'),
    ).find((cell) => cell.textContent?.includes('1 condition needs a cure before start.'));
    expect(sickStatusCell?.textContent).toContain('Off duty');
    expect(sickStatusCell?.textContent).toContain('Sick bay');
  });

  it('runs the sick-bay reconcile cure from the bot row', async () => {
    const { fixture, broker, router } = await setup();
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    const root = fixture.nativeElement as HTMLElement;

    expect(root.textContent).toContain('Account evidence stale');
    expect(root.textContent).toContain(
      'Receipt acct-recon-DU1234567 expired before this triage snapshot.',
    );
    const button = root.querySelector<HTMLButtonElement>(
      '[data-testid="bot-condition-action-live-running-aapl"]',
    );
    expect(button?.textContent).toContain('Run account reconcile');

    button?.click();
    await settle(fixture);

    expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    expect(broker.accountTriage).toHaveBeenCalledWith('DU1234567');
    expect(navigate).not.toHaveBeenCalledWith(['/broker/accounts']);
    expect(navigate).not.toHaveBeenCalledWith(['/broker/bots', 'live-running-aapl']);
  });

  it('sends the displayed cohort once and renders server-derived outcomes', async () => {
    const { fixture, service } = await setup();
    const secondReady = bot({
      strategy_instance_id: 'live-idle-qqq',
      name: 'live-idle-qqq',
      symbols: ['QQQ'],
      daily_lifecycle: lifecycle({
        latest_run_id: 'run-live-idle-qqq',
        primary_action: action({
          id: 'confirm_start',
          label: 'Start',
          offer_id: 'offer-live-idle-qqq',
          expires_at_ms: OFFER_EXPIRES_AT,
        }),
      }),
    });
    service.getBotCatalog.mockResolvedValue(catalog([bot(), secondReady]));
    await fixture.componentInstance.refresh();
    await settle(fixture);
    vi.mocked(service.runRollCall).mockClear();

    await fixture.componentInstance.startReadyBots();
    await settle(fixture);

    expect(service.runRollCall).toHaveBeenCalledTimes(1);
    expect(service.launchCohort).toHaveBeenCalledWith(
      'DU1234567',
      { member_strategy_instance_ids: ['live-idle-qqq', 'live-idle-spy'] },
    );
    expect(service.startHostRunner).not.toHaveBeenCalled();
    expect(fixture.componentInstance.launchProgress().title).toBe('Cohort start accepted');
    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      'Start accepted; live status is Ready.',
    );
  });

  it('blocks an incomplete cohort receipt and labels its server reason code', async () => {
    const { fixture, service } = await setup();
    const incompleteCohort: CohortBatchLaunchStatus = {
      schema_version: 1,
      account_id: 'DU1234567',
      cohort_id: 'paper-validation-incomplete',
      member_strategy_instance_ids: ['live-idle-spy'],
      window_start_ms: NEW_RUN,
      window_end_ms: NEW_RUN + 300_000,
      authorized_by: 'local-operator',
      authorized_recorded_at_ms: NEW_RUN,
      outcomes_state: 'recorded',
      outcomes: [
        {
          strategy_instance_id: 'live-idle-spy',
          state: 'blocked',
          reason: 'ACCOUNT_FROZEN',
          next_safe_action: 'Clear the account freeze.',
        },
      ],
      outcomes_recorded_at_ms: NEW_RUN,
      outcomes_error: null,
      evidence: COHORT_EVIDENCE,
    };
    service.launchCohort.mockResolvedValue(incompleteCohort);

    await fixture.componentInstance.startReadyBots();
    await settle(fixture);

    const rendered = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(fixture.componentInstance.launchProgress().title).toBe('Cohort start needs attention');
    expect(rendered).toContain('The server did not accept this cohort member.');
    expect(rendered).toContain('Account Frozen');
    expect(rendered).not.toContain('ACCOUNT_FROZEN');
  });

  it('keeps the launch at batch-level pending until durable member outcomes arrive', async () => {
    const { fixture, service } = await setup();
    const pendingCohort: CohortBatchLaunchStatus = {
      schema_version: 1,
      account_id: 'DU1234567',
      cohort_id: 'paper-validation-pending',
      member_strategy_instance_ids: ['live-idle-spy'],
      window_start_ms: NEW_RUN,
      window_end_ms: NEW_RUN + 300_000,
      authorized_by: 'local-operator',
      authorized_recorded_at_ms: NEW_RUN,
      outcomes_state: 'pending',
      outcomes: [],
      outcomes_recorded_at_ms: null,
      outcomes_error: null,
      evidence: COHORT_EVIDENCE,
    };
    service.launchCohort.mockResolvedValue(pendingCohort);

    await fixture.componentInstance.startReadyBots();
    await settle(fixture);

    const rendered = fixture.nativeElement.textContent ?? '';
    expect(fixture.componentInstance.launchProgress().phase).toBe('running');
    expect(fixture.componentInstance.launchProgress().title).toBe('Cohort start pending');
    expect(rendered).toContain('server has not yet recorded this member outcome');
    expect(rendered).not.toContain('server did not record a cohort outcome');
  });

  it('blocks the batch when its durable outcome receipt is unreadable', async () => {
    const { fixture, service } = await setup();
    const unreadableCohort: CohortBatchLaunchStatus = {
      schema_version: 1,
      account_id: 'DU1234567',
      cohort_id: 'paper-validation-unreadable',
      member_strategy_instance_ids: ['live-idle-spy'],
      window_start_ms: NEW_RUN,
      window_end_ms: NEW_RUN + 300_000,
      authorized_by: 'local-operator',
      authorized_recorded_at_ms: NEW_RUN,
      outcomes_state: 'unreadable',
      outcomes: [],
      outcomes_recorded_at_ms: null,
      outcomes_error: 'Outcome receipt has an invalid durable schema.',
      evidence: COHORT_EVIDENCE,
    };
    service.launchCohort.mockResolvedValue(unreadableCohort);

    await fixture.componentInstance.startReadyBots();
    await settle(fixture);

    const rendered = fixture.nativeElement.textContent ?? '';
    expect(fixture.componentInstance.launchProgress().phase).toBe('blocked');
    expect(fixture.componentInstance.launchProgress().title).toBe('Cohort outcome receipt unreadable');
    expect(rendered).toContain('Outcome receipt has an invalid durable schema.');
    expect(rendered).toContain('Refresh before retrying.');
  });

  it('requires cohort authorization confirmation before starting ready bots', async () => {
    const { fixture, service } = await setup();
    const root = fixture.nativeElement as HTMLElement;
    await fixture.componentInstance.requestCohortStart();
    await settle(fixture);

    expect(root.querySelector('[role="alertdialog"]')?.textContent).toContain('Authorize ready bots');
    expect(root.querySelector('[role="alertdialog"]')?.textContent).toContain('Target account: DU1234567');
    expect(root.querySelector('[role="alertdialog"]')?.textContent).toContain('Paper Execution');
    expect(service.launchCohort).not.toHaveBeenCalled();
  });

  it('uses the 3-bot stagger preset only for three selected, preflight-ready bots', async () => {
    const { fixture, service } = await setup();
    const additionalReadyBots = ['qqq', 'iwm'].map((symbol) => bot({
      strategy_instance_id: `live-idle-${symbol}`,
      name: `live-idle-${symbol}`,
      symbols: [symbol.toUpperCase()],
      daily_lifecycle: lifecycle({
        latest_run_id: `run-live-idle-${symbol}`,
        primary_action: action({
          id: 'confirm_start',
          label: 'Start',
          offer_id: `offer-live-idle-${symbol}`,
          expires_at_ms: OFFER_EXPIRES_AT,
        }),
      }),
    }));
    service.getBotCatalog.mockResolvedValue(catalog([bot(), ...additionalReadyBots]));
    await fixture.componentInstance.refresh();
    await settle(fixture);

    await fixture.componentInstance.requestCohortStart();
    await settle(fixture);
    fixture.componentInstance.selectStaggerCohortPreset(3);

    const selected = [...fixture.componentInstance.cohortSelectedIds()];
    expect(selected).toEqual(['live-idle-iwm', 'live-idle-qqq', 'live-idle-spy']);

    await fixture.componentInstance.confirmCohortStart(selected);
    await settle(fixture);

    expect(service.launchCohort).toHaveBeenCalledWith('DU1234567', {
      member_strategy_instance_ids: ['live-idle-iwm', 'live-idle-qqq', 'live-idle-spy'],
      launch_profile: 'paper_three_bot_stagger_v2',
    });
  });

  it('uses the 5-bot stagger preset only for five selected, preflight-ready bots', async () => {
    const { fixture, service } = await setup();
    const additionalReadyBots = ['qqq', 'iwm', 'aapl', 'msft'].map((symbol) => bot({
      strategy_instance_id: `live-idle-${symbol}`,
      name: `live-idle-${symbol}`,
      symbols: [symbol.toUpperCase()],
      daily_lifecycle: lifecycle({
        latest_run_id: `run-live-idle-${symbol}`,
        primary_action: action({
          id: 'confirm_start',
          label: 'Start',
          offer_id: `offer-live-idle-${symbol}`,
          expires_at_ms: OFFER_EXPIRES_AT,
        }),
      }),
    }));
    service.getBotCatalog.mockResolvedValue(catalog([bot(), ...additionalReadyBots]));
    await fixture.componentInstance.refresh();
    await settle(fixture);

    await fixture.componentInstance.requestCohortStart();
    await settle(fixture);
    fixture.componentInstance.selectStaggerCohortPreset(5);

    const selected = [...fixture.componentInstance.cohortSelectedIds()];
    expect(selected).toEqual([
      'live-idle-aapl',
      'live-idle-iwm',
      'live-idle-msft',
      'live-idle-qqq',
      'live-idle-spy',
    ]);

    await fixture.componentInstance.confirmCohortStart(selected);
    await settle(fixture);

    expect(service.launchCohort).toHaveBeenCalledWith('DU1234567', {
      member_strategy_instance_ids: [
        'live-idle-aapl',
        'live-idle-iwm',
        'live-idle-msft',
        'live-idle-qqq',
        'live-idle-spy',
      ],
      launch_profile: 'paper_five_bot_stagger_v2',
    });
  });

  it('lists hard cohort preflight blockers and disables authorization', async () => {
    const { fixture, service } = await setup();
    service.deployPreflight.mockResolvedValue({
      ready: false,
      blockers: [{
        condition: { id: 'fleet_contaminated', severity: 'blocking', scope: 'fleet', evidence: {} },
        host: 'deploy_preflight',
        anchor: { kind: 'surface', subject_key: null },
        audience: 'operator',
        disposition: 'fix_elsewhere',
        headline: 'Fleet contamination blocks starts',
        detail: 'Clear the account fleet state before starting bots.',
        primary_move: null,
        secondary_moves: [],
        applies_to: 'both',
      }],
    });
    const root = fixture.nativeElement as HTMLElement;
    await fixture.componentInstance.requestCohortStart();
    await settle(fixture);

    expect(root.textContent).toContain('Fleet contamination blocks starts');
    const authorize = Array.from(root.querySelectorAll<HTMLButtonElement>('button'))
      .find((button) => button.textContent?.includes('Authorize 0 selected bots'));
    expect(authorize?.disabled).toBe(true);
  });

  it('blocks Start one ready before roll call when account sick bay is active', async () => {
    const { fixture, service } = await setup({ triage: frozenTriage() });

    await fixture.componentInstance.startReadyBots();
    await settle(fixture);

    expect(service.runRollCall).not.toHaveBeenCalled();
    expect(service.startHostRunner).not.toHaveBeenCalled();
    expect(fixture.componentInstance.launchProgress().phase).toBe('blocked');
    expect(fixture.componentInstance.launchProgress().detail).toContain('Account sick bay');
  });

  it('renders the account freeze banner from account triage', async () => {
    const { fixture, broker, router } = await setup({ triage: frozenTriage() });
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    await fixture.componentInstance.refreshAccountTriage();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;

    expect(root.textContent).toContain('Account sick bay is gating new starts.');
    expect(root.textContent).toContain(
      'Run account reconciliation and clear the active account freeze before deploying.',
    );
    expect(broker.accountTriage).toHaveBeenCalledWith('DU1234567');

    const button = Array.from(root.querySelectorAll('button')).find((candidate) =>
      candidate.textContent?.includes('Open Accounts'),
    );
    expect(button).toBeDefined();
    button?.click();

    expect(navigate).toHaveBeenCalledWith(['/broker/accounts']);
  });

  it('filters by lifecycle status without rendering banned readiness vocabulary', async () => {
    const { fixture } = await setup();
    fixture.componentInstance.bots.update((rows) =>
      rows.map((row) =>
        row.strategy_instance_id === 'paper-msft'
          ? {
              ...row,
              only_fresh_run_available: true,
              readiness_verdict: 'BLOCKED',
              status_label: 'Off roster',
              status_detail: 'This bot is intentionally left off tomorrow\'s duty roster.',
              daily_lifecycle: lifecycle({
                display_status: 'Off roster',
                attention_badge: 'Off roster',
                reason: 'This bot is intentionally left off tomorrow\'s duty roster.',
                on_roster: false,
                primary_action: null,
                ambient_actions: [
                  action({
                    id: 'add_to_roster',
                    label: 'Add to roster',
                  }),
                  action({
                    id: 'retire_replace',
                    label: 'Retire & Replace',
                  }),
                ],
              }),
            }
          : row,
      ),
    );
    fixture.componentInstance.setLifecycleFilter('Off roster');
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(fixture.componentInstance.paperBots().map((row) => row.name)).toEqual([
      'paper-msft',
    ]);
    expect(text).toContain('Off roster');
    expect(text).not.toContain('Fresh run only');
    expect(text).not.toContain('BLOCKED');
    expect(fixture.componentInstance.paperBots()[0].searchText).not.toContain(
      'only fresh run available',
    );
  });

  it('navigates to the bot control page when a row is clicked', async () => {
    const { fixture, router } = await setup();
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    const targetRow = fixture.nativeElement.querySelector(
      '[aria-label="Open bot live-running-aapl"]',
    ) as HTMLElement | null;
    expect(targetRow).toBeDefined();
    targetRow?.click();
    await settle(fixture);

    expect(navigate).toHaveBeenCalledWith(['/broker/bots', 'live-running-aapl']);
  });

  it('soft deletes selected bots after confirmation', async () => {
    const { fixture, service } = await setup();

    fixture.componentInstance.toggleBotSelection('paper-msft', true);
    fixture.detectChanges();
    fixture.componentInstance.requestDeleteSelected();
    fixture.detectChanges();

    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      'Soft delete selected bots?',
    );

    await fixture.componentInstance.confirmDeleteSelected();
    await settle(fixture);

    expect(service.deleteBot).toHaveBeenCalledWith('paper-msft', {
      mode: 'soft',
      deleted_by: 'operator',
      reason: 'Deleted from Bots page',
    });
    expect(fixture.componentInstance.selectedCount()).toBe(0);
    expect(service.getBotCatalog).toHaveBeenCalledTimes(2);
  });

  it('refreshes the catalog when a bulk soft delete partially succeeds', async () => {
    const { fixture, service } = await setup();
    service.deleteBot.mockImplementation((instanceId: string) => {
      if (instanceId === 'paper-msft') {
        return Promise.resolve({});
      }
      return Promise.reject(new Error('Bot is still running'));
    });
    service.getBotCatalog.mockResolvedValueOnce(
      catalog([
        bot({
          strategy_instance_id: 'live-running-aapl',
          name: 'live-running-aapl',
          symbols: ['AAPL'],
          last_run_at_ms: NEW_RUN,
        }),
      ]),
    );

    fixture.componentInstance.toggleBotSelection('paper-msft', true);
    fixture.componentInstance.toggleBotSelection('live-running-aapl', true);
    fixture.componentInstance.requestDeleteSelected();
    await fixture.componentInstance.confirmDeleteSelected();
    await settle(fixture);

    expect(service.deleteBot).toHaveBeenCalledTimes(2);
    expect(service.getBotCatalog).toHaveBeenCalledTimes(2);
    expect(fixture.componentInstance.selectedCount()).toBe(1);
    expect(fixture.componentInstance.isSelected('paper-msft')).toBe(false);
    expect(fixture.componentInstance.isSelected('live-running-aapl')).toBe(true);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Bot is still running');
  });
});
