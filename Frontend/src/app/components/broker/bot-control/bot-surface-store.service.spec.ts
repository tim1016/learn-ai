import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LiveRunsService } from '../../../services/live-runs.service';
import { makeStatus } from './bot-control-page.fixtures';
import { BotSurfaceStore } from './bot-surface-store.service';

class StubEventSource {
  static instances: StubEventSource[] = [];
  readonly listeners = new Map<string, ((event: Event) => void)[]>();
  readonly url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    StubEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: (event: Event) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }

  dispatch(name: string, data = ''): void {
    const event = new MessageEvent(name, { data });
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }

  close(): void {
    this.closed = true;
  }
}

describe('BotSurfaceStore', () => {
  const liveRuns = { getInstanceStatus: vi.fn() };

  beforeEach(() => {
    StubEventSource.instances = [];
    vi.stubGlobal('EventSource', StubEventSource);
    liveRuns.getInstanceStatus.mockReset();
    TestBed.configureTestingModule({
      providers: [
        BotSurfaceStore,
        { provide: LiveRunsService, useValue: liveRuns },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.unstubAllGlobals();
  });

  it('retains the same-session snapshot read-only when the stream drops', async () => {
    const snapshot = makeStatus();
    liveRuns.getInstanceStatus.mockResolvedValue(snapshot);
    const store = TestBed.inject(BotSurfaceStore);

    await store.bootstrapInstance('sid-x');
    store.connect('sid-x');
    const source = StubEventSource.instances[0];
    source.dispatch('open');
    source.dispatch('snapshot', JSON.stringify(snapshot));
    expect(store.readOnly()).toBe(false);

    source.dispatch('error');

    expect(store.status()).toBe(snapshot);
    expect(store.readOnly()).toBe(true);
    expect(store.errorMessage()).toContain('same-session snapshot');
  });

  it('coalesces guard and resolver bootstrap reads for the same route', async () => {
    let resolve!: (status: ReturnType<typeof makeStatus>) => void;
    const pending = new Promise<ReturnType<typeof makeStatus>>((done) => {
      resolve = done;
    });
    liveRuns.getInstanceStatus.mockReturnValue(pending);
    const store = TestBed.inject(BotSurfaceStore);

    const guardRead = store.bootstrapInstance('sid-x');
    const resolverRead = store.bootstrapInstance('sid-x');
    resolve(makeStatus());

    expect(guardRead).toBe(resolverRead);
    await expect(guardRead).resolves.toMatchObject({ kind: 'ready' });
    expect(liveRuns.getInstanceStatus).toHaveBeenCalledOnce();
  });

  it('classifies proven absence separately from an unreachable control plane', async () => {
    const store = TestBed.inject(BotSurfaceStore);
    liveRuns.getInstanceStatus.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 410 }),
    );
    expect(await store.bootstrapInstance('deleted-bot')).toEqual({
      kind: 'missing',
      status: 410,
    });

    liveRuns.getInstanceStatus.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 0 }),
    );
    const unreachable = await store.bootstrapInstance('unknown-bot');
    expect(unreachable.kind).toBe('unreachable');
    expect(store.status()).toBeNull();
  });

  it('rejects a bootstrap snapshot for a different bot identity', async () => {
    liveRuns.getInstanceStatus.mockResolvedValue(makeStatus({ id: 'sid-b' }));
    const store = TestBed.inject(BotSurfaceStore);

    const result = await store.bootstrapInstance('sid-a');

    expect(result.kind).toBe('unreachable');
    expect(store.status()).toBeNull();
  });

  it('matches a pending mutation to the receipt delivered by SSE without refetch', async () => {
    const initial = makeStatus();
    liveRuns.getInstanceStatus.mockResolvedValue(initial);
    const store = TestBed.inject(BotSurfaceStore);
    await store.bootstrapInstance('sid-x');
    store.connect('sid-x');
    store.establishPending({ mutation_attempt_id: 'mutation-1' });
    const updated = makeStatus();
    updated.surface_version = initial.surface_version + 1;
    updated.latest_mutation = {
      schema_version: 1,
      mutation_attempt_id: 'mutation-1',
      instance_id: 'sid-x',
      run_id: 'run-x',
      action: 'pause',
      requested_at_ms: 1_700_000_000_000,
      last_transition_at_ms: 1_700_000_000_010,
      dispatch_state: 'RESPONSE_CONFIRMED',
      outcome: { actuated: true },
      evidence: null,
    };

    StubEventSource.instances[0].dispatch('snapshot', JSON.stringify(updated));

    expect(store.pendingAttemptId()).toBeNull();
    expect(store.latestMutationReceipt()?.mutation_attempt_id).toBe('mutation-1');
    expect(liveRuns.getInstanceStatus).toHaveBeenCalledTimes(1);
  });

  it('closes the response/SSE race when the receipt arrives before the HTTP response', async () => {
    const initial = makeStatus();
    liveRuns.getInstanceStatus.mockResolvedValue(initial);
    const store = TestBed.inject(BotSurfaceStore);
    await store.bootstrapInstance('sid-x');
    store.connect('sid-x');
    const updated = makeStatus();
    updated.surface_version = initial.surface_version + 1;
    updated.latest_mutation = {
      schema_version: 1,
      mutation_attempt_id: 'mutation-race',
      instance_id: 'sid-x',
      run_id: 'run-x',
      action: 'resume',
      requested_at_ms: 1_700_000_000_000,
      last_transition_at_ms: 1_700_000_000_010,
      dispatch_state: 'RESPONSE_CONFIRMED',
      outcome: { actuated: true },
      evidence: null,
    };
    StubEventSource.instances[0].dispatch('snapshot', JSON.stringify(updated));

    store.establishPending({ mutation_attempt_id: 'mutation-race' });

    expect(store.pendingAttemptId()).toBeNull();
    expect(store.latestMutationReceipt()?.mutation_attempt_id).toBe('mutation-race');
  });

  it('keeps a pending attempt across a stream drop and clears it from the reconnect snapshot', async () => {
    const initial = makeStatus();
    liveRuns.getInstanceStatus.mockResolvedValue(initial);
    const store = TestBed.inject(BotSurfaceStore);
    await store.bootstrapInstance('sid-x');
    store.connect('sid-x');
    const source = StubEventSource.instances[0];
    source.dispatch('open');
    source.dispatch('snapshot', JSON.stringify(initial));
    store.establishPending({ mutation_attempt_id: 'mutation-outage' });

    source.dispatch('error');

    // The outage must not silently discard the pending attempt, and the
    // banner + read-only state must make the wait honest.
    expect(store.pendingAttemptId()).toBe('mutation-outage');
    expect(store.readOnly()).toBe(true);
    expect(store.errorMessage()).toContain('same-session snapshot');

    const reconnected = makeStatus();
    reconnected.surface_version = initial.surface_version + 1;
    reconnected.latest_mutation = {
      schema_version: 1,
      mutation_attempt_id: 'mutation-outage',
      instance_id: 'sid-x',
      run_id: 'run-x',
      action: 'pause',
      requested_at_ms: 1_700_000_000_000,
      last_transition_at_ms: 1_700_000_000_010,
      dispatch_state: 'RESPONSE_CONFIRMED',
      outcome: { actuated: true },
      evidence: null,
    };
    source.dispatch('snapshot', JSON.stringify(reconnected));

    expect(store.pendingAttemptId()).toBeNull();
    expect(store.latestMutationReceipt()?.mutation_attempt_id).toBe('mutation-outage');
    expect(store.readOnly()).toBe(false);
    expect(store.errorMessage()).toBeNull();
  });

  it('recovers read/write state after a malformed event is followed by a valid snapshot', async () => {
    const initial = makeStatus();
    liveRuns.getInstanceStatus.mockResolvedValue(initial);
    const store = TestBed.inject(BotSurfaceStore);
    await store.bootstrapInstance('sid-x');
    store.connect('sid-x');
    const source = StubEventSource.instances[0];
    source.dispatch('open');
    source.dispatch('snapshot', '{not-json');
    expect(store.readOnly()).toBe(true);

    source.dispatch('snapshot', JSON.stringify(initial));

    expect(store.streamStatus()).toBe('open');
    expect(store.readOnly()).toBe(false);
    expect(store.errorMessage()).toBeNull();
  });

  it('closes the EventSource with its route-scoped injector', async () => {
    liveRuns.getInstanceStatus.mockResolvedValue(makeStatus());
    const store = TestBed.inject(BotSurfaceStore);
    await store.bootstrapInstance('sid-x');
    store.connect('sid-x');
    const source = StubEventSource.instances[0];

    TestBed.resetTestingModule();

    expect(source.closed).toBe(true);
  });
});
