import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { LiveRunsService } from './live-runs.service';
import type { DaemonDiagnosticReport } from '../api/daemon-diagnostics.types';
import type { HostRunnerActionResponse, HostRunnerHealth } from '../api/live-runs.types';
import type {
  CrashRecoveryOverrideResponse,
  LifecycleSafetyTriageResponse,
  LifecycleTimelineResponse,
} from '../api/live-instances.types';

/**
 * Start/Stop/health all go through the data plane (`/api/live-instances/...`),
 * NOT directly to the daemon. The daemon enforces a mandatory
 * `X-Live-Runner-Token` on every route — including `/health` since PRD #619-C
 * P2 — and the browser must never hold that shared secret. The data plane
 * forwards the token server-side. A regression here (calling
 * `environment.liveRunnerDaemonUrl` directly) reintroduces the 401 that made
 * the cockpit's "Live engine unavailable" banner fire while the daemon was
 * actually up and the deploy form unusable.
 */
describe('LiveRunsService start/stop proxy', () => {
  let service: LiveRunsService;
  let httpMock: HttpTestingController;

  const accepted: HostRunnerActionResponse = {
    accepted: true,
    process: {
      state: 'running',
      run_id: 'run-abc',
      pid: 1,
      started_at_ms: 1,
      ended_at_ms: null,
      exit_code: null,
      command: [],
      log_path: '/x',
      message: 'active',
    },
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        LiveRunsService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    service = TestBed.inject(LiveRunsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('startHostRunner posts to the data-plane proxy, not the daemon', async () => {
    const promise = service.startHostRunner('run-abc', {
      readonly: false,
      hydrate_policy: 'optional',
      strategy: 'spy_ema_crossover',
      max_orders_per_day: 4,
      ibkr_host: '127.0.0.1',
    });

    const req = httpMock.expectOne('/api/live-instances/runs/run-abc/start');
    expect(req.request.method).toBe('POST');
    expect(req.request.url.startsWith('http')).toBe(false); // relative — proxied, not daemon-direct
    expect(req.request.body.readonly).toBe(false);
    req.flush(accepted);

    await expect(promise).resolves.toEqual(accepted);
  });

  it('stopHostRunner posts to the data-plane proxy', async () => {
    const promise = service.stopHostRunner('run-abc', { force: false });

    const req = httpMock.expectOne('/api/live-instances/runs/run-abc/stop');
    expect(req.request.method).toBe('POST');
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(accepted);

    await expect(promise).resolves.toEqual(accepted);
  });

  it('encodes the run id in the proxy path', async () => {
    const promise = service.stopHostRunner('run/with space', { force: true });

    const req = httpMock.expectOne('/api/live-instances/runs/run%2Fwith%20space/stop');
    expect(req.request.method).toBe('POST');
    req.flush(accepted);

    await promise;
  });

  it('getHostRunnerHealth probes the data-plane proxy, not the daemon directly', async () => {
    const health: HostRunnerHealth = {
      ok: true,
      repo_root: '/r',
      live_runs_root: '/r/artifacts',
      fetched_at_ms: 1,
      process: {
        state: 'idle',
        run_id: null,
        pid: null,
        started_at_ms: null,
        ended_at_ms: null,
        exit_code: null,
        command: [],
        log_path: null,
        message: null,
      },
    };
    const promise = service.getHostRunnerHealth();

    const req = httpMock.expectOne('/api/live-instances/daemon-health');
    expect(req.request.method).toBe('GET');
    // Relative URL — proxied through the data plane, never daemon-direct.
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(health);

    await expect(promise).resolves.toEqual(health);
  });

  it('reads deploy preflight through the data-plane proxy', async () => {
    const response = {
      ready: false,
      blockers: [
        {
          id: 'broker_disconnected',
          severity: 'blocking' as const,
          disposition: 'fix_elsewhere' as const,
          headline: 'Broker disconnected',
          detail: 'Connect the IBKR session before deploying.',
          primary_move: {
            label: 'Connect the broker',
            action: { kind: 'navigate' as const, route: '/broker', fragment: null },
            target: null,
          },
          secondary_moves: [],
          applies_to: 'both' as const,
        },
      ],
    };
    const promise = service.deployPreflight({
      strategyKey: 'deployment_validation',
      accountId: 'DU123',
      instanceId: 'bot-1',
    });

    const req = httpMock.expectOne(
      (candidate) =>
        candidate.url === '/api/live-instances/deploy-preflight' &&
        candidate.params.get('strategy_key') === 'deployment_validation' &&
        candidate.params.get('account_id') === 'DU123' &&
        candidate.params.get('instance_id') === 'bot-1',
    );
    expect(req.request.method).toBe('GET');
    req.flush(response);

    await expect(promise).resolves.toEqual(response);
  });

  it('reads the bounded lifecycle projection timeline through the data plane', async () => {
    const response: LifecycleTimelineResponse = {
      projection_available: true,
      canonical_fallback_required: false,
      rows: [],
    };
    const promise = service.getLifecycleTimeline({
      account_id: 'DU123',
      strategy_instance_id: 'sid-x',
      run_id: 'run-x',
      limit: 5,
    });

    const req = httpMock.expectOne((request) =>
      request.url === '/api/lifecycle-projection/timeline'
      && request.params.get('account_id') === 'DU123'
      && request.params.get('strategy_instance_id') === 'sid-x'
      && request.params.get('run_id') === 'run-x'
      && request.params.get('limit') === '5',
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(response);

    await expect(promise).resolves.toEqual(response);
  });

  it('records crash recovery overrides through the data-plane instance endpoint', async () => {
    const response: CrashRecoveryOverrideResponse = {
      accepted: true,
      account_id: 'DU123',
      strategy_instance_id: 'sid-x',
      run_id: 'run-x',
      bot_order_namespace: 'learn-ai/sid-x/v1',
      override_id: 'crash-recovery-1',
      recorded_at_ms: 1_700_000_000_001,
      blocking_recorded_at_ms: 1_700_000_000_000,
      event_type: 'account_audited_override_recorded',
      rung_receipt_warnings: [],
    };
    const promise = service.recordCrashRecoveryOverride('sid/x', {
      confirm_account_flat: true,
      approved_by: 'operator',
    });

    const req = httpMock.expectOne('/api/live-instances/sid%2Fx/crash-recovery-override');
    expect(req.request.method).toBe('POST');
    expect(req.request.url.startsWith('http')).toBe(false);
    expect(req.request.body).toEqual({ confirm_account_flat: true, approved_by: 'operator' });
    req.flush(response);

    await expect(promise).resolves.toEqual(response);
  });

  it('reads filtered lifecycle safety triage through the data plane', async () => {
    const response: LifecycleSafetyTriageResponse = {
      projection_available: true,
      canonical_fallback_required: false,
      rows: [],
    };
    const promise = service.getLifecycleSafetyTriage({
      account_id: 'DU123',
      strategy_instance_id: 'sid-x',
      run_id: 'run-x',
      status: 'blocked',
      event_type: 'BrokerOrderUncertain',
      node_id: 'ack_or_reconcile',
      severity: 'warning',
      limit: 25,
    });

    const req = httpMock.expectOne((request) =>
      request.url === '/api/lifecycle-projection/safety-triage'
      && request.params.get('account_id') === 'DU123'
      && request.params.get('strategy_instance_id') === 'sid-x'
      && request.params.get('run_id') === 'run-x'
      && request.params.get('status') === 'blocked'
      && request.params.get('event_type') === 'BrokerOrderUncertain'
      && request.params.get('node_id') === 'ack_or_reconcile'
      && request.params.get('severity') === 'warning'
      && request.params.get('limit') === '25',
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(response);

    await expect(promise).resolves.toEqual(response);
  });

  it('renewControlPlaneLease posts to the data-plane proxy, not the daemon directly', async () => {
    const health: HostRunnerHealth = {
      ok: true,
      repo_root: '/r',
      live_runs_root: '/r/artifacts',
      fetched_at_ms: 1,
      process: {
        state: 'idle',
        run_id: null,
        pid: null,
        started_at_ms: null,
        ended_at_ms: null,
        exit_code: null,
        command: [],
        log_path: null,
        message: null,
      },
      daemon_boot_id: 'boot-abc',
      lease_status: 'CONNECTED',
      last_lease_written_at_ms: 2,
    };
    const promise = service.renewControlPlaneLease();

    const req = httpMock.expectOne('/api/live-instances/daemon-health/renew-lease');
    expect(req.request.method).toBe('POST');
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(health);

    await expect(promise).resolves.toEqual(health);
  });

  it('getDaemonDiagnostics reads the always-200 data-plane report', async () => {
    const report: DaemonDiagnosticReport = daemonReport();
    const promise = service.getDaemonDiagnostics();

    const req = httpMock.expectOne('/api/live-instances/daemon-diagnose');
    expect(req.request.method).toBe('GET');
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(report);

    await expect(promise).resolves.toEqual(report);
  });

  it('reads bot event backfill through the live-runs data-plane route', async () => {
    const promise = service.getBotEvents('run/with space', { after_seq: 12, limit: 25 });

    const req = httpMock.expectOne((request) =>
      request.url === '/api/live-runs/run%2Fwith%20space/bot-events'
      && request.params.get('after_seq') === '12'
      && request.params.get('limit') === '25',
    );
    expect(req.request.method).toBe('GET');
    req.flush({ rows: [], next_seq: null });

    await expect(promise).resolves.toEqual({ rows: [], next_seq: null });
  });

  it('getInstanceDaemonDiagnostics encodes the strategy instance id', async () => {
    const report: DaemonDiagnosticReport = daemonReport();
    const promise = service.getInstanceDaemonDiagnostics('bot/with space');

    const req = httpMock.expectOne('/api/live-instances/bot%2Fwith%20space/daemon-diagnose');
    expect(req.request.method).toBe('GET');
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(report);

    await expect(promise).resolves.toEqual(report);
  });
});

function daemonReport(): DaemonDiagnosticReport {
  return {
    overall_status: 'fail',
    transport: 'UNREACHABLE',
    dominant_condition: 'unreachable',
    headline: {
      title: 'Live engine is not answering',
      summary: 'The data plane could not reach the host live engine.',
      remediation: 'Start the host live engine on this machine, then refresh diagnostics.',
    },
    checks: [],
    per_instance: [],
    daemon_boot_id: null,
    fetched_at_ms: 1_700_000_000_000,
  };
}
