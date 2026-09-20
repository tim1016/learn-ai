import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import {
  LaneAttentionService,
  QUIET_LANE_ATTENTION_STATE,
  UNPOLLED_LANE_ATTENTION_STATE,
  type AggregateAttentionLane,
  type AggregateAttentionResponse,
  type LaneAttentionItem,
} from './lane-attention.service';

const AGGREGATE_URL = '/api/broker-clerks/aggregate/attention';

function okLane(
  clerkId: string,
  items: LaneAttentionItem[] = [],
): AggregateAttentionLane {
  return { broker: 'alpaca', clerk_id: clerkId, ok: true, value: { account_id: 'PA1', items } };
}

function failedLane(clerkId: string, reason = 'clerk_unreachable'): AggregateAttentionLane {
  return {
    broker: 'alpaca',
    clerk_id: clerkId,
    ok: false,
    error_reason: reason,
    error_message: 'agent timed out',
  };
}

function response(lanes: readonly AggregateAttentionLane[]): AggregateAttentionResponse {
  return { observed_at_ms: 1_700_000_000_000, lanes };
}

function item(overrides: Partial<{ condition_id: string; severity: string }> = {}) {
  return {
    condition_id: 'unc-1',
    reason_code: 'EXIT_NOT_FLAT',
    kind: 'uncertainty',
    severity: 'blocking',
    strategy_instance_id: 'ema-1',
    symbol: 'SPY',
    headline: 'This bot’s exit has not flattened its position',
    ...overrides,
  };
}

function setup() {
  TestBed.configureTestingModule({
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  return {
    svc: TestBed.inject(LaneAttentionService),
    http: TestBed.inject(HttpTestingController),
  };
}

describe('LaneAttentionService', () => {
  afterEach(() => TestBed.inject(HttpTestingController).verify());

  it('stores each lane’s own slice of one aggregate poll, never a merge', async () => {
    const { svc, http } = setup();
    const pending = svc.refresh();

    http
      .expectOne(AGGREGATE_URL)
      .flush(response([okLane('clrk_a', [item()]), okLane('clrk_b', [item({ condition_id: 'unc-2' })])]));
    await pending;

    expect(svc.stateFor('clrk_a').items.map((i) => i.condition_id)).toEqual(['unc-1']);
    expect(svc.stateFor('clrk_b').items.map((i) => i.condition_id)).toEqual(['unc-2']);
  });

  it('renders one lane’s failed read as its own unknown, with the fold’s reason — never as quiet', async () => {
    const { svc, http } = setup();
    const pending = svc.refresh();

    http
      .expectOne(AGGREGATE_URL)
      .flush(response([okLane('clrk_a'), failedLane('clrk_b')]));
    await pending;

    expect(svc.stateFor('clrk_a')).toEqual(QUIET_LANE_ATTENTION_STATE);
    expect(svc.stateFor('clrk_b').unknown).toBe(true);
    expect(svc.stateFor('clrk_b').errorReason).toBe('clerk_unreachable');
    expect(svc.stateFor('clrk_b').items).toEqual([]);
  });

  it('clears a lane’s items exactly when its next read reports none', async () => {
    const { svc, http } = setup();
    const first = svc.refresh();
    http.expectOne(AGGREGATE_URL).flush(response([okLane('clrk_a', [item()])]));
    await first;
    expect(svc.stateFor('clrk_a').items.length).toBe(1);

    const second = svc.refresh();
    http.expectOne(AGGREGATE_URL).flush(response([okLane('clrk_a')]));
    await second;
    expect(svc.stateFor('clrk_a')).toEqual(QUIET_LANE_ATTENTION_STATE);
  });

  it('dedupes by condition_id so one condition is one item', async () => {
    const { svc, http } = setup();
    const pending = svc.refresh();

    http
      .expectOne(AGGREGATE_URL)
      .flush(response([okLane('clrk_a', [item(), item(), item({ condition_id: 'unc-2' })])]));
    await pending;

    expect(svc.stateFor('clrk_a').items.map((i) => i.condition_id)).toEqual(['unc-1', 'unc-2']);
  });

  it('forgets lanes the aggregate no longer reports', async () => {
    const { svc, http } = setup();
    const first = svc.refresh();
    http.expectOne(AGGREGATE_URL).flush(response([okLane('clrk_a')]));
    await first;

    const second = svc.refresh();
    http.expectOne(AGGREGATE_URL).flush(response([okLane('clrk_b')]));
    await second;

    expect(svc.stateFor('clrk_a')).toEqual(UNPOLLED_LANE_ATTENTION_STATE);
    expect(svc.stateFor('clrk_b')).toEqual(QUIET_LANE_ATTENTION_STATE);
  });

  it('keeps every lane’s last known state when the aggregate read itself fails', async () => {
    const { svc, http } = setup();
    const first = svc.refresh();
    http.expectOne(AGGREGATE_URL).flush(response([okLane('clrk_a', [item()])]));
    await first;

    const second = svc.refresh();
    http.expectOne(AGGREGATE_URL).flush('gateway down', { status: 502, statusText: 'Bad Gateway' });
    await second;

    // Safe direction: the open condition keeps ringing until a successful
    // read says otherwise — a transport fault never blanks it.
    expect(svc.stateFor('clrk_a').items.length).toBe(1);
    expect(svc.stateFor('clrk_a').unknown).toBe(false);
  });
});
