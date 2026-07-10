import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrokerConnectivityService } from './broker-connectivity.service';
import { BrokerHealthService } from './broker-health.service';
import { LiveRunsService } from './live-runs.service';

class StubEventSource {
  static instances: StubEventSource[] = [];
  readonly listeners = new Map<string, (event: Event) => void>();
  closed = false;

  constructor(readonly url: string) {
    StubEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === 'function') this.listeners.set(name, listener);
  }

  emit(name: string, data: string): void {
    this.listeners.get(name)?.(new MessageEvent(name, { data }));
  }

  close(): void {
    this.closed = true;
  }
}

interface SetupOpts {
  instances: unknown[];
  fleetVerdict?: 'clean' | 'contaminated';
  policyBlocks?: boolean;
  daemonHealth?: Record<string, unknown>;
  /** Override the broker-health payload the connectivity service reads. The
   * default is a happy ``connected`` snapshot so unrelated tests don't have
   * to spell it out. */
  brokerHealth?: Record<string, unknown>;
}

function setup(opts: SetupOpts) {
  const svc = {
    getHostRunnerHealth: vi.fn().mockResolvedValue(
      opts.daemonHealth ?? {
        ok: true,
        repo_root: '/repo',
        live_runs_root: '/runs',
        fetched_at_ms: 1,
        process: { state: 'idle' },
      },
    ),
    getAccountFleet: vi.fn().mockResolvedValue({
      net_positions: null,
      explained_total: {},
      explained_by_instance: [],
      residual: {},
      verdict: opts.fleetVerdict ?? 'clean',
      policy_blocks_starts: opts.policyBlocks ?? false,
      summary: '',
    }),
    getInstances: vi.fn().mockResolvedValue(opts.instances),
  };
  const defaultBrokerHealth = {
    connected: true,
    connection_state: 'connected',
  };
  const health = {
    health: () => opts.brokerHealth ?? defaultBrokerHealth,
    refresh: vi.fn(),
  };
  TestBed.configureTestingModule({
    providers: [
      { provide: LiveRunsService, useValue: svc },
      { provide: BrokerHealthService, useValue: health },
    ],
  });
  return TestBed.inject(BrokerConnectivityService);
}

/** Let the resource() loaders resolve and effects flush. */
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  TestBed.flushEffects();
  await Promise.resolve();
}

function fleetLink(service: BrokerConnectivityService) {
  return service.links().find((l) => l.key === 'fleet');
}

function brokerLink(service: BrokerConnectivityService) {
  return service.links().find((l) => l.key === 'broker');
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('BrokerConnectivityService fleet state', () => {
  it('reports "Nothing deployed" (neutral) when no instances exist', async () => {
    vi.stubGlobal('EventSource', undefined);
    const service = setup({ instances: [] });
    await flush();

    expect(service.nothingDeployed()).toBe(true);
    expect(service.fleetState()).toBe('unknown'); // neutral, not a healthy green
    expect(fleetLink(service)?.detail).toBe('Nothing deployed');
  });

  it('reports "Clear" (ok) for a clean account with instances deployed', async () => {
    vi.stubGlobal('EventSource', undefined);
    const service = setup({ instances: [{ strategy_instance_id: 'spy_ema_paper' }] });
    await flush();

    expect(service.nothingDeployed()).toBe(false);
    expect(service.fleetState()).toBe('ok');
    expect(fleetLink(service)?.detail).toBe('Clear');
  });

  it('reports the policy block (warn) when contaminated and starts are blocked', async () => {
    vi.stubGlobal('EventSource', undefined);
    const service = setup({
      instances: [{ strategy_instance_id: 'spy_ema_paper' }],
      fleetVerdict: 'contaminated',
      policyBlocks: true,
    });
    await flush();

    expect(service.fleetState()).toBe('warn');
    expect(service.fleetBlocksStarts()).toBe(true);
    expect(fleetLink(service)?.detail).toBe('Contaminated — new starts blocked');
  });

  it('uses the fleet roster SSE snapshot as the browser roster source', async () => {
    StubEventSource.instances = [];
    vi.stubGlobal('EventSource', StubEventSource);
    const service = setup({ instances: [] });
    await flush();

    expect(service.nothingDeployed()).toBe(false);

    StubEventSource.instances[0].emit(
      'snapshot',
      JSON.stringify({
        stream_epoch: 'fleet-epoch',
        surface_version: 1,
        fetched_at_ms: 1_700_000_000_000,
        daemon_fetched_at_ms: 1_700_000_000_000,
        instances: [
          {
            strategy_instance_id: 'blocked-bot',
            process_state: 'idle',
            readiness_verdict: 'BLOCKED',
            readiness_as_of_ms: 1_700_000_000_000,
          },
          {
            strategy_instance_id: 'ready-bot',
            process_state: 'running',
            readiness_verdict: 'READY',
            readiness_as_of_ms: 1_700_000_000_000,
          },
        ],
      }),
    );

    expect(StubEventSource.instances[0].url).toBe(
      '/api/live-instances/fleet/stream?control_intent=learn-ai-browser-control',
    );
    expect(service.nothingDeployed()).toBe(false);
    expect(service.rosterChips()).toEqual([
      {
        id: 'blocked-bot',
        label: 'blocked-bot',
        processState: 'idle',
        readinessVerdict: 'BLOCKED',
        state: 'warn',
      },
    ]);
  });
});

describe('BrokerConnectivityService broker state (auto-reconnect)', () => {
  it('renders CONNECTED with a green dot when the backend publishes connection_state=connected', async () => {
    const service = setup({
      instances: [],
      brokerHealth: { connected: true, connection_state: 'connected' },
    });
    await flush();

    expect(service.brokerState()).toBe('ok');
    expect(service.brokerConnectionState()).toBe('connected');
    expect(brokerLink(service)?.detail).toBe('Connected');
    expect(service.blockers()).not.toContain(
      expect.stringContaining('Broker reconnecting'),
    );
  });

  it('renders RECONNECTING with the attempt counter when the monitor is mid-attempt', async () => {
    const service = setup({
      instances: [],
      brokerHealth: {
        connected: false,
        connection_state: 'reconnecting',
        reconnect_attempt: 3,
      },
    });
    await flush();

    expect(service.brokerState()).toBe('warn');
    expect(service.brokerConnectionState()).toBe('reconnecting');
    expect(brokerLink(service)?.detail).toBe('Reconnecting (attempt 3)');
    // The blocker reason is operator-actionable — order entry stays
    // paused until the link comes back.
    expect(
      service.blockers().some((b) => b.startsWith('Broker reconnecting')),
    ).toBe(true);
  });

  it('renders SOFT_LOST (warn) when Error 1100 fired but the socket is open', async () => {
    const service = setup({
      instances: [],
      brokerHealth: {
        connected: true,
        connection_state: 'soft_lost',
        connection_lost: true,
      },
    });
    await flush();

    expect(service.brokerState()).toBe('warn');
    expect(service.brokerConnectionState()).toBe('soft_lost');
    expect(brokerLink(service)?.detail).toContain('feed lost');
  });

  it('renders RECOVERING while post-reconnect recovery callbacks are running', async () => {
    const service = setup({
      instances: [],
      brokerHealth: {
        connected: true,
        connection_state: 'recovering',
      },
    });
    await flush();

    expect(service.brokerState()).toBe('warn');
    expect(service.brokerConnectionState()).toBe('recovering');
    expect(brokerLink(service)?.detail).toContain('Recovering streams');
    expect(service.blockers()).toContain(
      'Broker recovering streams — order entry paused until subscriptions and probes pass.',
    );
  });

  it('renders SUBSCRIPTIONS_STALE with the IBKR code when data was lost', async () => {
    const service = setup({
      instances: [],
      brokerHealth: {
        connected: true,
        connection_state: 'subscriptions_stale',
        last_ibkr_code: 1101,
      },
    });
    await flush();

    expect(service.brokerState()).toBe('warn');
    expect(service.brokerConnectionState()).toBe('subscriptions_stale');
    expect(brokerLink(service)?.detail).toBe('Subscriptions stale — resubscribe required (1101)');
  });

  it('renders DISCONNECTED (down) when the socket is hard-closed', async () => {
    const service = setup({
      instances: [],
      brokerHealth: {
        connected: false,
        connection_state: 'disconnected',
        condition: {
          code: 'DATA_PLANE_BROKER_DISCONNECTED',
          severity: 'warning',
          title: 'Data-plane broker session disconnected',
          summary:
            'The FastAPI data-plane IBKR client is disconnected. IB Gateway/TWS may still be logged in.',
          remediation: 'Use the IBKR Connect control to establish the data-plane session.',
        },
      },
    });
    await flush();

    expect(service.brokerState()).toBe('down');
    expect(service.brokerConnectionState()).toBe('disconnected');
    expect(brokerLink(service)?.detail).toBe('Data-plane broker session disconnected');
    expect(service.blockers()).toContain(
      'The FastAPI data-plane IBKR client is disconnected. IB Gateway/TWS may still be logged in. Use the IBKR Connect control to establish the data-plane session.',
    );
  });

  it('renders HARD_DOWN when auto-recovery exhausts reconnect attempts', async () => {
    const service = setup({
      instances: [],
      brokerHealth: {
        connected: false,
        connection_state: 'hard_down',
        condition: {
          code: 'DATA_PLANE_BROKER_HARD_DOWN',
          severity: 'critical',
          title: 'Data-plane broker session down',
          summary:
            'IB Gateway/TWS may be logged in, but the FastAPI data-plane IBKR client is not connected.',
          remediation: 'Use the IBKR Connect/Reconnect control after confirming Gateway API access is enabled.',
        },
      },
    });
    await flush();

    expect(service.brokerState()).toBe('down');
    expect(service.brokerConnectionState()).toBe('hard_down');
    expect(brokerLink(service)?.detail).toBe('Data-plane broker session down');
    expect(service.blockers()).toContain(
      'IB Gateway/TWS may be logged in, but the FastAPI data-plane IBKR client is not connected. Use the IBKR Connect/Reconnect control after confirming Gateway API access is enabled.',
    );
  });

  it('renders DISABLED (unknown / grey) using the backend reason when the broker is intentionally off', async () => {
    const service = setup({
      instances: [],
      brokerHealth: {
        connected: false,
        disabled: true,
        connection_state: 'disabled',
        reason: 'IBKR_BROKER_ENABLED=false — host runner owns the IBKR session',
      },
    });
    await flush();

    expect(service.brokerState()).toBe('unknown');
    expect(service.brokerConnectionState()).toBe('disabled');
    expect(brokerLink(service)?.detail).toContain('host runner');
  });
});

describe('BrokerConnectivityService daemonFreshness', () => {
  const base = { ok: true, repo_root: '/repo', live_runs_root: '/runs', fetched_at_ms: 1, process: { state: 'idle' } };

  it('reads up-to-date when the running SHA matches the working tree', async () => {
    const service = setup({
      instances: [],
      daemonHealth: { ...base, git_sha: 'abc1234def', repo_head_sha: 'abc1234def', code_stale: false },
    });
    await flush();

    const f = service.daemonFreshness();
    expect(f.state).toBe('fresh');
    expect(f.sha).toBe('abc1234');
  });

  it('reads stale with the behind-count when the daemon predates the working tree', async () => {
    const service = setup({
      instances: [],
      daemonHealth: { ...base, git_sha: 'old1234aaa', repo_head_sha: 'new5678bbb', code_stale: true, commits_behind: 3 },
    });
    await flush();

    const f = service.daemonFreshness();
    expect(f.state).toBe('stale');
    expect(f.commitsBehind).toBe(3);
  });

  it('does NOT read fresh for a legacy daemon that omits the freshness contract', async () => {
    // Pre-change daemon: only git_sha (its on-disk HEAD), no repo_head_sha /
    // code_stale. Treating missing code_stale as falsy would falsely show
    // "up to date" in exactly the stale scenario this detects (Codex P1).
    const service = setup({
      instances: [],
      daemonHealth: { ...base, git_sha: 'legacy123ff' },
    });
    await flush();

    const f = service.daemonFreshness();
    expect(f.state).toBe('unknown');
    expect(f.sha).toBe('legacy1'); // still shown so the strip can prompt a restart
  });
});
