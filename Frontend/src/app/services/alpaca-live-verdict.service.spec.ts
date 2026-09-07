import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { AlpacaLiveVerdictService } from './alpaca-live-verdict.service';
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
    shadow_state: 'not_applicable',
    final_verdict: 'paper',
    headline: 'Paper account PA9 — no real money at risk',
    detail: 'ALPACA_MODE=paper.',
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function setup() {
  const brokers = new FakeBrokersService();
  TestBed.configureTestingModule({ providers: [{ provide: BrokersService, useValue: brokers }] });
  return { svc: TestBed.inject(AlpacaLiveVerdictService), brokers };
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('AlpacaLiveVerdictService', () => {
  it('holds null before the first response', () => {
    const { svc } = setup();
    expect(svc.verdict()).toBeNull();
  });

  it('refresh() stores the server verdict verbatim', async () => {
    const { svc, brokers } = setup();
    const v = makeVerdict({ final_verdict: 'live-unarmed', configured_mode: 'live' });
    brokers.getLiveVerdict.mockResolvedValue(v);

    await svc.refresh();

    expect(svc.verdict()).toEqual(v);
    expect(svc.lastError()).toBeNull();
  });

  it('refresh() clears the verdict and records the error when the read fails', async () => {
    const { svc, brokers } = setup();
    brokers.getLiveVerdict.mockResolvedValue(makeVerdict());
    await svc.refresh();
    brokers.getLiveVerdict.mockRejectedValue(new Error('down'));

    await svc.refresh();

    expect(svc.verdict()).toBeNull();
    expect(svc.lastError()).toBeInstanceOf(Error);
  });

  it('start() is idempotent and refreshes immediately', async () => {
    const { svc, brokers } = setup();
    brokers.getLiveVerdict.mockResolvedValue(makeVerdict());

    svc.start();
    svc.start();
    await Promise.resolve();

    expect(brokers.getLiveVerdict).toHaveBeenCalledTimes(1);
  });
});
