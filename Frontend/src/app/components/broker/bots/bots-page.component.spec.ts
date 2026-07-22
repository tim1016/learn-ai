import { provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountTriageResponse } from '../../../api/account-reconciliation.types';
import type { BotCatalogResponse, BotCatalogRow, BotRollCallResponse } from '../../../api/live-instances.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BotsPageComponent } from './bots-page.component';

const READY_BOT = {
  strategy_instance_id: 'paper-spy',
  name: 'paper-spy',
  description: null,
  status_label: 'Ready',
  status_detail: 'Ready after roll call.',
  status_tone: 'positive',
  only_fresh_run_available: false,
  needs_attention: false,
  trading_mode: 'paper',
  symbols: ['SPY'],
  engine: 'live-engine',
  engine_asset_class: 'equity',
  created_at_ms: 1,
  updated_at_ms: 1,
  last_run_at_ms: 1,
  last_run_label: 'Clean',
  last_run_result: 'CLEAN',
  last_run_detail: 'Previous run exited normally.',
  process_state: 'IDLE',
  desired_state: 'PAUSED',
  readiness_verdict: 'READY',
  daily_lifecycle: {
    phase: 'OFF_DUTY',
    presence_label: 'Off duty',
    display_status: 'Ready',
    attention_badge: 'Ready',
    reason: 'Ready after roll call.',
    on_roster: true,
    active_run_id: null,
    latest_run_id: 'run-paper-spy',
    drift_detected: false,
    primary_action: {
      id: 'confirm_start',
      label: 'Start',
      enabled: true,
      reason: null,
      offer_id: 'offer-paper-spy',
      expires_at_ms: 2,
    },
    ambient_actions: [],
  },
  start_request: {
    readonly: false,
    hydrate_policy: 'require',
    strategy: 'spy_ema',
    max_orders_per_day: 2,
    ibkr_host: '127.0.0.1',
  },
  attendance: [],
  metrics: {
    pnl: { realized: null, unrealized: null, total: null },
    trade_count: null,
    current_exposure: 'Flat',
    open_positions: 0,
    error_count: 0,
  },
} as BotCatalogRow;

class FakeLiveRunsService {
  getBotCatalog = vi.fn<() => Promise<BotCatalogResponse>>();
  runRollCall = vi.fn<() => Promise<BotRollCallResponse>>();
  startHostRunner = vi.fn();
  deleteBot = vi.fn();
}

class FakeBrokerService {
  accountTriage = vi.fn<() => Promise<AccountTriageResponse>>();
  reconcileAccount = vi.fn();
}

class FakeBrokerHealthService {
  readonly health = signal({ account_id: 'DU1234567' });
}

describe('BotsPageComponent', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('starts one ready bot with its fresh offer and exposes no cohort launcher', async () => {
    const liveRuns = new FakeLiveRunsService();
    const broker = new FakeBrokerService();
    const health = new FakeBrokerHealthService();
    const catalog = {
      bots: [READY_BOT],
      roll_call: {
        ready: 1,
        off_roster: 0,
        sick_bay: 0,
        on_duty: 0,
        off_duty: 1,
        retired: 0,
        generated_at_ms: 1,
        session_date: '2026-07-21',
        effective_stop_ms: null,
      },
      evening_report: null,
    } as BotCatalogResponse;
    liveRuns.getBotCatalog.mockResolvedValue(catalog);
    liveRuns.runRollCall.mockResolvedValue({ summary: catalog.roll_call, offers: [] } as BotRollCallResponse);
    liveRuns.startHostRunner.mockResolvedValue({ accepted: true, process: { state: 'running' } });
    broker.accountTriage.mockResolvedValue({ freeze_banner: null } as AccountTriageResponse);

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [BotsPageComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        { provide: LiveRunsService, useValue: liveRuns },
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useValue: health },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(BotsPageComponent);
    await settle(fixture);

    await fixture.componentInstance.startReadyBots();
    await settle(fixture);

    expect(liveRuns.startHostRunner).toHaveBeenCalledWith('run-paper-spy', {
      ...READY_BOT.start_request,
      roll_call_offer_id: 'offer-paper-spy',
    });
    expect(fixture.componentInstance.launchProgress().title).toBe('Canary start accepted');
    expect((fixture.nativeElement as HTMLElement).textContent?.toLowerCase()).not.toContain('cohort');
  });
});

async function settle(fixture: ComponentFixture<BotsPageComponent>): Promise<void> {
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
}
