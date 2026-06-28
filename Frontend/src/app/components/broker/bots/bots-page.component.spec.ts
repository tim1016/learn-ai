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

const OLD_CREATED = 1_700_000_000_000;
const NEW_CREATED = 1_800_000_000_000;

function bot(overrides: Partial<BotCatalogRow> = {}): BotCatalogRow {
  return {
    strategy_instance_id: 'old-spy',
    name: 'old-spy',
    description: null,
    status_label: 'Ready for paper trading',
    status_tone: 'positive',
    needs_attention: false,
    trading_mode: 'paper',
    symbols: ['SPY'],
    engine: 'live-engine',
    engine_asset_class: 'equity',
    created_at_ms: OLD_CREATED,
    updated_at_ms: 1_700_000_000_100,
    last_run_at_ms: 1_700_000_000_200,
    last_run_result: 'CLEAN',
    process_state: 'RUNNING',
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
}

async function setup() {
  const service = new FakeLiveRunsService();
  service.getBotCatalog.mockResolvedValue({
    bots: [
      bot(),
      bot({
        strategy_instance_id: 'new-aapl',
        name: 'new-aapl',
        symbols: ['AAPL'],
        created_at_ms: NEW_CREATED,
        needs_attention: true,
        status_label: 'Blocked by readiness gate',
        status_tone: 'danger',
        metrics: {
          pnl: { realized: null, unrealized: -4, total: null },
          trade_count: null,
          current_exposure: 'AAPL 10',
          open_positions: 1,
          error_count: 1,
        },
      }),
    ],
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

  it('renders the backend catalog order without local resorting', async () => {
    const { fixture } = await setup();
    const cards = [...fixture.nativeElement.querySelectorAll('.bot-card h2')] as HTMLElement[];
    expect(cards.map((el) => el.textContent?.trim())).toEqual(['old-spy', 'new-aapl']);
  });

  it('filters by name and symbol from the catalog projection', async () => {
    const { fixture } = await setup();
    fixture.componentInstance.nameQuery.set('old');
    fixture.componentInstance.symbolQuery.set('SPY');
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('old-spy');
    expect(text).not.toContain('new-aapl');
  });

  it('filters by server-authored attention and trading mode fields', async () => {
    const { fixture } = await setup();
    fixture.componentInstance.setErrorFilter('has-errors');
    fixture.componentInstance.setTradingModeFilter('paper');
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('new-aapl');
    expect(text).not.toContain('old-spy');
  });

  it('expands card metadata inline', async () => {
    const { fixture } = await setup();
    expect(fixture.nativeElement.querySelector('.expanded')).toBeNull();

    fixture.componentInstance.toggleExpanded('new-aapl');
    fixture.detectChanges();

    const expanded = fixture.nativeElement.querySelector('.expanded') as HTMLElement | null;
    expect(expanded?.textContent).toContain('Created');
    expect(expanded?.textContent).toContain('Trading mode');
  });

  it('navigates to the existing instance cockpit', async () => {
    const { fixture, router } = await setup();
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    await fixture.componentInstance.openCockpit('new-aapl');

    expect(navigate).toHaveBeenCalledWith(['/broker/instances', 'new-aapl']);
  });
});
