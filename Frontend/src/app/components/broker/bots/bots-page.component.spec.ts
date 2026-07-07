import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  BotCatalogResponse,
  BotCatalogRow,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BotsPageComponent } from './bots-page.component';

const OLD_RUN = 1_700_000_000_000;
const NEW_RUN = 1_800_000_000_000;

function bot(overrides: Partial<BotCatalogRow> = {}): BotCatalogRow {
  return {
    strategy_instance_id: 'live-idle-spy',
    name: 'live-idle-spy',
    description: null,
    status_label: 'Ready for paper trading',
    status_detail: 'All readiness checks are passing.',
    status_tone: 'positive',
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

class FakeLiveRunsService {
  getBotCatalog = vi.fn<() => Promise<BotCatalogResponse>>();
  deleteBot = vi.fn<(instanceId: string, request?: unknown) => Promise<unknown>>();
}

async function setup() {
  const service = new FakeLiveRunsService();
  service.getBotCatalog.mockResolvedValue({
    bots: [
      bot(),
      bot({
        strategy_instance_id: 'live-running-aapl',
        name: 'live-running-aapl',
        symbols: ['AAPL'],
        last_run_at_ms: NEW_RUN,
        needs_attention: true,
        status_label: 'Needs operator review',
        status_detail: 'Desired state has no durable intent.',
        status_tone: 'danger',
        last_run_label: 'Exited with error',
        last_run_result: 'EXITED_WITH_ERROR',
        last_run_detail: 'Previous run exited with an error: runtime exception. Exit code 1.',
        process_state: 'RUNNING',
        readiness_verdict: 'DEGRADED',
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
        status_label: 'Monitoring only',
        status_tone: 'neutral',
      }),
    ],
  });
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
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(BotsPageComponent);
  await settle(fixture);
  return { fixture, service, router: TestBed.inject(Router) };
}

async function settle(fixture: ComponentFixture<BotsPageComponent>): Promise<void> {
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
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
    fixture.componentInstance.setReadinessFilter('DEGRADED');
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
    expect(text).toContain('DEGRADED');
    expect(text).toContain('Exited with error');
    expect(text).not.toContain('RUNNING');
    expect(text).not.toContain('Needs operator review');
    expect(text).not.toContain('Desired state has no durable intent.');
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
    service.getBotCatalog.mockResolvedValueOnce({
      bots: [
        bot({
          strategy_instance_id: 'live-running-aapl',
          name: 'live-running-aapl',
          symbols: ['AAPL'],
          last_run_at_ms: NEW_RUN,
        }),
      ],
    });

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
