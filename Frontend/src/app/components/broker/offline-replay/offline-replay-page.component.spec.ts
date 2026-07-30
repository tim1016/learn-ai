import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type {
  OfflineReplayCatalogResponse,
  OfflineReplaySession,
} from '../../../api/offline-replay.types';
import { OfflineReplayService } from '../../../services/offline-replay.service';
import { OfflineReplayPageComponent } from './offline-replay-page.component';

const SESSION_OPEN_MS = 1_775_000_000_000;

function replaySession(status: OfflineReplaySession['status']): OfflineReplaySession {
  return {
    session_id: 'replay-1',
    status,
    strategy_key: 'ema_crossover_signal',
    session_date_ms: SESSION_OPEN_MS,
    session_open_ms: SESSION_OPEN_MS,
    session_close_ms: SESSION_OPEN_MS + 390 * 60_000,
    warmup_end_ms: SESSION_OPEN_MS + 225 * 60_000,
    playback_end_ms: SESSION_OPEN_MS + 285 * 60_000,
    playhead_ms: SESSION_OPEN_MS + 230 * 60_000,
    playback_minutes: 60,
    speed: 60,
    created_at_ms: SESSION_OPEN_MS,
    started_at_ms: SESSION_OPEN_MS,
    completed_at_ms: null,
    symbols: ['SPY', 'TSLA'],
    bots: (['SPY', 'TSLA'] as const).map((symbol) => ({
      symbol,
      status: status === 'failed' ? 'failed' : 'running',
      run_id: `replay-1-${symbol.toLowerCase()}`,
      bars_processed: 230,
      decisions_emitted: 1,
      orders_submitted: 0,
      fills_observed: 0,
      open_position_quantity: 0,
      final_equity_usd: '100000.00',
      data_sha256: `${symbol.toLowerCase()}-digest`,
      failure_code: null,
      failure_message: null,
    })),
    failure_code: null,
    failure_message: null,
  };
}

const CATALOG: OfflineReplayCatalogResponse = {
  sessions: [
    {
      session_date_ms: SESSION_OPEN_MS,
      session_open_ms: SESSION_OPEN_MS,
      session_close_ms: SESSION_OPEN_MS + 390 * 60_000,
      eligible: true,
      cached_symbols: ['SPY', 'TSLA'],
    },
  ],
  recommended_session_date_ms: SESSION_OPEN_MS,
};

class FakeOfflineReplayService {
  getCatalog = vi.fn().mockResolvedValue(CATALOG);
  listSessions = vi.fn().mockResolvedValue([] as OfflineReplaySession[]);
  createSession = vi.fn().mockResolvedValue(replaySession('running'));
  getSession = vi.fn().mockResolvedValue(replaySession('running'));
  command = vi.fn().mockResolvedValue(replaySession('paused'));
}

async function setup(sessions: OfflineReplaySession[] = []) {
  TestBed.resetTestingModule();
  const service = new FakeOfflineReplayService();
  service.listSessions.mockResolvedValue(sessions);
  await TestBed.configureTestingModule({
    imports: [OfflineReplayPageComponent],
    providers: [{ provide: OfflineReplayService, useValue: service }],
  }).compileComponents();
  const fixture = TestBed.createComponent(OfflineReplayPageComponent);
  fixture.detectChanges();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await fixture.whenStable();
  fixture.detectChanges();
  return { fixture, service };
}

function clickButton(
  fixture: ComponentFixture<OfflineReplayPageComponent>,
  label: string,
): void {
  const root = fixture.nativeElement as HTMLElement;
  const button = Array.from(root.querySelectorAll('button')).find((element) =>
    element.textContent?.includes(label),
  );
  if (!button) throw new Error(`no button matching "${label}"`);
  button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

describe('OfflineReplayPageComponent', () => {
  it('launches one SPY bot and one TSLA bot from the launch control', async () => {
    const { fixture, service } = await setup();

    clickButton(fixture, 'Launch SPY + TSLA');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(service.createSession).toHaveBeenCalledWith(
      expect.objectContaining({
        symbols: ['SPY', 'TSLA'],
        playback_minutes: 60,
        speed: 60,
      }),
    );
    const symbols = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.symbol'),
    ).map((element) => element.textContent?.trim());
    expect(symbols).toEqual(['SPY', 'TSLA']);
    fixture.destroy();
  });

  it('sends a single-minute step from the transport while paused', async () => {
    const { fixture, service } = await setup([replaySession('paused')]);

    clickButton(fixture, 'Step one minute');
    await fixture.whenStable();

    expect(service.command).toHaveBeenCalledWith('replay-1', { action: 'step' });
    fixture.destroy();
  });

  it('renders a bot failure instead of hiding the stopped engine', async () => {
    const failed = replaySession('failed');
    failed.bots[1] = {
      ...failed.bots[1],
      failure_code: 'BOT_RUNTIME_FAILED',
      failure_message: 'strategy callback raised',
    };

    const { fixture } = await setup([failed]);

    const alert = (fixture.nativeElement as HTMLElement).querySelector(
      '.bot-failure[role="alert"]',
    );
    expect(alert?.textContent).toContain('Bot Runtime Failed');
    expect(alert?.textContent).toContain('strategy callback raised');
    fixture.destroy();
  });
});
