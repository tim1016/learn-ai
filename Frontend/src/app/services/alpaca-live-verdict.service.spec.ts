import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import { provideFleetDirectory, testLane } from '../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../fleet/fleet-directory.types';
import type { ResourceTarget } from '../fleet/resource-target';
import { AlpacaLiveVerdictService, UNPOLLED_LANE_STATE } from './alpaca-live-verdict.service';
import { BrokersService } from './brokers.service';
import { POLL_REQUEST_TIMEOUT_MS } from './poll-timeout';

/**
 * Per-lane behaviour without the transport.
 *
 * It deliberately cannot observe whether the lane reads were *dispatched*
 * concurrently — stubbing `BrokersService` stubs out `PolledReadScheduler`,
 * which is the layer where lane isolation is won or lost. The slow-lane test
 * below therefore runs against the real service over `HttpTestingController`;
 * nothing here can stand in for it.
 */
class FakeBrokersService {
  readonly readLane = vi.fn<(clerkId: string) => Promise<AlpacaLiveVerdict>>();

  getLiveVerdicts(targets: readonly ResourceTarget[]): Promise<AlpacaLiveVerdict>[] {
    return targets.map((target) => this.readLane(target.clerkId));
  }
}

function makeVerdict(overrides: Partial<AlpacaLiveVerdict> = {}): AlpacaLiveVerdict {
  return {
    configured_mode: 'paper',
    observed_account_id: 'PA9',
    mode_agreement: 'agreed',
    clerk_authority: 'sqlite',
    clerk_refusal_reason_code: null,
    armed_instance_count: 0,
    envelope_state: 'not_applicable',
    // A paper verdict carries no envelope and no hold, and the server says so
    // with 'not_applicable' on both.
    envelope_agreement: 'not_applicable',
    shadow_state: 'not_applicable',
    loss_hold: 'not_applicable',
    final_verdict: 'paper',
    headline: 'Paper account PA9 — no real money at risk',
    detail: 'ALPACA_MODE=paper.',
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function setup(lanes = [testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })]) {
  const brokers = new FakeBrokersService();
  const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: lanes });
  TestBed.configureTestingModule({
    providers: [
      { provide: BrokersService, useValue: brokers },
      { provide: FleetDirectoryService, useValue: directory.useValue },
    ],
  });
  return { svc: TestBed.inject(AlpacaLiveVerdictService), brokers, directory };
}

/** The same service over the real `BrokersService` and `PolledReadScheduler`,
 * so dispatch order and the poll ceiling are the ones that ship. */
function setupOverHttp(lanes: LaneDescriptor[]) {
  const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: lanes });
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: FleetDirectoryService, useValue: directory.useValue },
    ],
  });
  return {
    svc: TestBed.inject(AlpacaLiveVerdictService),
    http: TestBed.inject(HttpTestingController),
  };
}

/** Let one tick resolve its roster and dispatch its lane reads. */
async function flush(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

/** Drive `n` additional polling ticks, awaiting each in turn. `roster()`
 * forces a real `directory.refresh()` only on every 6th tick
 * (`DIRECTORY_REFRESH_EVERY_N_TICKS`), so a test proving something about the
 * post-refresh roster must cross that boundary rather than asserting after
 * one call. */
async function driveTicks(svc: AlpacaLiveVerdictService, n: number): Promise<void> {
  for (let i = 0; i < n; i += 1) await svc.refresh();
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('AlpacaLiveVerdictService', () => {
  it('holds the unpolled default for a lane before its first response', () => {
    const { svc } = setup();
    expect(svc.stateFor('clrk_a')).toEqual(UNPOLLED_LANE_STATE);
  });

  it('refresh() stores the server verdict verbatim under the lane clerk_id', async () => {
    const { svc, brokers } = setup([testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })]);
    const v = makeVerdict({
      final_verdict: 'live-unarmed',
      configured_mode: 'live',
      envelope_agreement: 'unsealed',
      loss_hold: 'clear',
    });
    brokers.readLane.mockResolvedValue(v);

    await svc.refresh();

    expect(svc.stateFor('clrk_a')).toEqual({ verdict: v, lastError: null });
  });

  it('refresh() records the error and clears this lane\'s verdict when its read fails', async () => {
    const { svc, brokers } = setup([testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })]);
    brokers.readLane.mockResolvedValue(makeVerdict());
    await svc.refresh();
    brokers.readLane.mockRejectedValue(new Error('down'));

    await svc.refresh();

    const state = svc.stateFor('clrk_a');
    expect(state.verdict).toBeNull();
    expect(state.lastError).toBeInstanceOf(Error);
  });

  it("one lane's failed read never blanks, delays, or overwrites another lane's state (FR-093)", async () => {
    const { svc, brokers } = setup([
      testLane({ clerk_id: 'clrk_paper', broker: 'alpaca', display_label: 'Paper' }),
      testLane({ clerk_id: 'clrk_live', broker: 'alpaca', display_label: 'Live' }),
    ]);
    const liveVerdict = makeVerdict({ final_verdict: 'live-armed', configured_mode: 'live' });
    brokers.readLane.mockImplementation(async (clerkId: string) => {
      if (clerkId === 'clrk_paper') throw new Error('paper lane down');
      return liveVerdict;
    });

    await svc.refresh();

    expect(svc.stateFor('clrk_paper').verdict).toBeNull();
    expect(svc.stateFor('clrk_paper').lastError).toBeInstanceOf(Error);
    expect(svc.stateFor('clrk_live')).toEqual({ verdict: liveVerdict, lastError: null });
  });

  it("one lane's SLOW read never costs another lane its badge, through the real scheduler (FR-093)", async () => {
    vi.useFakeTimers();
    const { svc, http } = setupOverHttp([
      testLane({ clerk_id: 'clrk_slow', broker: 'alpaca', display_label: 'Live' }),
      testLane({ clerk_id: 'clrk_fast', broker: 'alpaca', display_label: 'Paper' }),
    ]);

    const tick = svc.refresh();
    await flush();

    // Both lanes are in flight at once. Serialized — which is what the shared
    // scheduler does by default — only the first URL would appear here, and
    // the second lane's read would later be rejected with "the poll ceiling
    // elapsed while it waited for a turn" for a fault that was never its own.
    const open = http.match((request) => request.url.endsWith('/live-verdict'));
    expect(open.map((request) => request.request.url)).toEqual([
      '/api/brokers/alpaca/clerks/clrk_slow/live-verdict',
      '/api/brokers/alpaca/clerks/clrk_fast/live-verdict',
    ]);

    const fast = makeVerdict({
      configured_mode: 'live',
      final_verdict: 'live-armed',
      observed_account_id: '9LIVE0001',
      armed_instance_count: 2,
    });
    open[1].flush(fast);

    // The slow lane answers nothing and burns the entire ceiling.
    await vi.advanceTimersByTimeAsync(POLL_REQUEST_TIMEOUT_MS + 1);
    await tick;

    expect(svc.stateFor('clrk_fast')).toEqual({ verdict: fast, lastError: null });
    expect(svc.stateFor('clrk_slow').verdict).toBeNull();
    expect(svc.stateFor('clrk_slow').lastError).toBeDefined();
  });

  it('drives the fleet directory itself and retries a failed load on the next tick', async () => {
    const brokers = new FakeBrokersService();
    const lanes = [testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })];
    let attempts = 0;
    const ensureLoaded = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) throw new Error('directory down');
      return { observed_at_ms: 1, clerks: lanes };
    });
    TestBed.configureTestingModule({
      providers: [
        { provide: BrokersService, useValue: brokers },
        {
          provide: FleetDirectoryService,
          useValue: { ensureLoaded, lanesOf: () => (attempts > 1 ? lanes : []) },
        },
      ],
    });
    const svc = TestBed.inject(AlpacaLiveVerdictService);
    brokers.readLane.mockResolvedValue(makeVerdict());

    // A failed directory load is state, not a terminal condition: the tick
    // completes with no lanes rather than throwing, and asks again next time.
    await svc.refresh();
    expect(ensureLoaded).toHaveBeenCalledTimes(1);
    expect(svc.stateFor('clrk_a')).toEqual(UNPOLLED_LANE_STATE);

    await svc.refresh();

    expect(ensureLoaded).toHaveBeenCalledTimes(2);
    expect(svc.stateFor('clrk_a').verdict).not.toBeNull();
  });

  it('forgets a lane the directory stops reporting, so its return renders a fresh read', async () => {
    const { svc, brokers, directory } = setup([
      testLane({ clerk_id: 'clrk_a', broker: 'alpaca' }),
      testLane({ clerk_id: 'clrk_b', broker: 'alpaca' }),
    ]);
    brokers.readLane.mockResolvedValue(makeVerdict());
    await svc.refresh(); // tick 1
    expect(svc.stateFor('clrk_b').verdict).not.toBeNull();

    // The double only promotes a rebind to what `ensureLoaded()`/`lanesOf()`
    // see once `directory.refresh()` runs — exactly like the real service.
    // `roster()` only forces that every 6th tick, so cross that boundary
    // rather than asserting after the very next call.
    directory.rebind({
      observed_at_ms: 2,
      clerks: [testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })],
    });
    await driveTicks(svc, 5); // ticks 2-6; tick 6 is the forced refresh

    expect(svc.stateFor('clrk_b')).toEqual(UNPOLLED_LANE_STATE);
    expect(svc.stateFor('clrk_a').verdict).not.toBeNull();
  });

  it('picks up a lane the directory starts reporting after the first load, but only on a refresh tick', async () => {
    const { svc, brokers, directory } = setup([testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })]);
    brokers.readLane.mockResolvedValue(makeVerdict());
    await svc.refresh(); // tick 1: the roster is cached via ensureLoaded()
    expect(svc.stateFor('clrk_a').verdict).not.toBeNull();

    // A lane provisioned after the tab's first successful directory load —
    // exactly the case `ensureLoaded()`'s permanent post-load cache can
    // never see on its own.
    directory.rebind({
      observed_at_ms: 2,
      clerks: [
        testLane({ clerk_id: 'clrk_a', broker: 'alpaca' }),
        testLane({ clerk_id: 'clrk_b', broker: 'alpaca' }),
      ],
    });

    // Ticks 2-5 still call ensureLoaded(), a no-op once loaded: the new lane
    // must stay invisible and un-badged through every one of them.
    for (let i = 0; i < 4; i += 1) {
      await svc.refresh();
      expect(svc.stateFor('clrk_b')).toEqual(UNPOLLED_LANE_STATE);
    }

    // Tick 6 forces directory.refresh() — only now does the new lane get a
    // badge.
    await svc.refresh();

    expect(svc.stateFor('clrk_b').verdict).not.toBeNull();
  });

  it('a failed forced directory refresh leaves the existing roster in place, and the next due tick asks again', async () => {
    const { svc, brokers, directory } = setup([testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })]);
    brokers.readLane.mockResolvedValue(makeVerdict());
    await svc.refresh(); // tick 1: the roster is cached via ensureLoaded()
    expect(svc.stateFor('clrk_a').verdict).not.toBeNull();

    let refreshAttempts = 0;
    directory.useValue.refresh = vi.fn(async () => {
      refreshAttempts += 1;
      throw new Error('directory refresh failed');
    });

    await driveTicks(svc, 5); // ticks 2-6; tick 6 is the forced refresh, and it rejects

    expect(refreshAttempts).toBe(1);
    // Loud, not blank: the last known badge survives a failed refresh.
    expect(svc.stateFor('clrk_a').verdict).not.toBeNull();

    await driveTicks(svc, 6); // ticks 7-12; tick 12 asks again rather than giving up forever

    expect(refreshAttempts).toBe(2);
  });

  it('start() is idempotent and refreshes every known lane immediately', async () => {
    const { svc, brokers } = setup([
      testLane({ clerk_id: 'clrk_a', broker: 'alpaca' }),
      testLane({ clerk_id: 'clrk_b', broker: 'alpaca' }),
    ]);
    brokers.readLane.mockResolvedValue(makeVerdict());

    svc.start();
    svc.start();
    await flush();

    expect(brokers.readLane).toHaveBeenCalledTimes(2);
  });
});
