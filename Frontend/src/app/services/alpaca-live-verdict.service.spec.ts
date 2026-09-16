import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import { provideFleetDirectory, testLane } from '../fleet/fleet-directory-testing';
import { AlpacaLiveVerdictService, UNPOLLED_LANE_STATE } from './alpaca-live-verdict.service';
import { BrokersService } from './brokers.service';

class FakeBrokersService {
  getLiveVerdict = vi.fn();
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

afterEach(() => {
  TestBed.resetTestingModule();
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
    brokers.getLiveVerdict.mockResolvedValue(v);

    await svc.refresh();

    expect(svc.stateFor('clrk_a')).toEqual({ verdict: v, lastError: null });
  });

  it('refresh() records the error and clears this lane\'s verdict when its read fails', async () => {
    const { svc, brokers } = setup([testLane({ clerk_id: 'clrk_a', broker: 'alpaca' })]);
    brokers.getLiveVerdict.mockResolvedValue(makeVerdict());
    await svc.refresh();
    brokers.getLiveVerdict.mockRejectedValue(new Error('down'));

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
    brokers.getLiveVerdict.mockImplementation(async (target: { clerkId: string }) => {
      if (target.clerkId === 'clrk_paper') throw new Error('paper lane down');
      return liveVerdict;
    });

    await svc.refresh();

    expect(svc.stateFor('clrk_paper').verdict).toBeNull();
    expect(svc.stateFor('clrk_paper').lastError).toBeInstanceOf(Error);
    expect(svc.stateFor('clrk_live')).toEqual({ verdict: liveVerdict, lastError: null });
  });

  it('start() is idempotent and refreshes every known lane immediately', async () => {
    const { svc, brokers } = setup([
      testLane({ clerk_id: 'clrk_a', broker: 'alpaca' }),
      testLane({ clerk_id: 'clrk_b', broker: 'alpaca' }),
    ]);
    brokers.getLiveVerdict.mockResolvedValue(makeVerdict());

    svc.start();
    svc.start();
    await Promise.resolve();

    expect(brokers.getLiveVerdict).toHaveBeenCalledTimes(2);
  });
});
