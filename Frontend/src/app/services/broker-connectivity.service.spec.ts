import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrokerConnectivityService } from './broker-connectivity.service';
import { BrokerHealthService } from './broker-health.service';
import { LiveRunsService } from './live-runs.service';

interface SetupOpts {
  instances: unknown[];
  fleetVerdict?: 'clean' | 'contaminated';
  policyBlocks?: boolean;
  daemonHealth?: Record<string, unknown>;
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
  const health = { health: () => ({ connected: true }), refresh: vi.fn() };
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

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerConnectivityService fleet state', () => {
  it('reports "Nothing deployed" (neutral) when no instances exist', async () => {
    const service = setup({ instances: [] });
    await flush();

    expect(service.nothingDeployed()).toBe(true);
    expect(service.fleetState()).toBe('unknown'); // neutral, not a healthy green
    expect(fleetLink(service)?.detail).toBe('Nothing deployed');
  });

  it('reports "Clear" (ok) for a clean account with instances deployed', async () => {
    const service = setup({ instances: [{ strategy_instance_id: 'spy_ema_paper' }] });
    await flush();

    expect(service.nothingDeployed()).toBe(false);
    expect(service.fleetState()).toBe('ok');
    expect(fleetLink(service)?.detail).toBe('Clear');
  });

  it('reports the policy block (warn) when contaminated and starts are blocked', async () => {
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
