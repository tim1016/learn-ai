// PRD #617 — cockpit shell component spec.  Uses TestBed (Vitest +
// Angular's bundled testing) to drive the component without the
// @testing-library/angular runtime dependency, mirroring the existing
// spec style under cockpit-v2/reused/.

import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CockpitShellComponent } from './cockpit-shell.component';
import { LiveRunsService } from '../../../services/live-runs.service';
import type {
  OperatorSurfaceControlPlane,
  OperatorSurfaceRuntimeFreshness,
} from '../../../api/live-instances.types';

function makeStatus() {
  return {
    strategy_instance_id: 'sid-x',
    process: { state: 'running', pid: 1, bound_run_id: 'r1', started_at_ms: 0 },
    live_binding: { run_id: 'r1', run_dir: null, source: 'registry' },
    evidence_binding: null,
    desired_state: {
      state: 'RUNNING',
      path_status: 'ok',
      updated_at_ms: 0,
      updated_by: 'op',
      reason: null,
      version: 1,
    },
    readiness: {
      kind: 'live_readiness',
      as_of_ms: 0,
      source: 'engine',
      verdict: 'BLOCKED',
      summary: '',
      gates: [],
      orders_used: null,
      orders_cap: null,
    },
    latest_decision: null,
    decision_columns: [],
    broker: null,
    start_defaults: null,
    provenance: null,
    sizing: null,
    last_exit: null,
    symbol: 'SPY',
    action_plan: null,
    instrument_surface: null,
    lineage: null,
    operator_surface: {
      schema_version: 1,
      host_process: { state: 'RUNNING', notice: null, copyable_command: null },
      prior_run: { classification: 'UNKNOWN' },
      broker: { safety_verdict: 'UNSAFE', connection: 'DISCONNECTED' },
      configuration: { verdict: 'READY', reason_codes: [] },
      current_risk: {
        posture: 'FLAT',
        pending_order_count: 0,
        verdict: 'READY',
        unrealized_pnl: null,
      },
      daily_order_cap: { used: null, limit: null },
      action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
      actions: {
        resume: {
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: 'BROKER_SAFETY_UNSAFE',
          disabled_reasons: ['BROKER_SAFETY_UNSAFE'],
        },
        pause: { enabled: true, effect: 'LIVE_ACTUATION', disabled_reason_code: null, disabled_reasons: [] },
        stop: { enabled: true, effect: 'LIVE_ACTUATION', disabled_reason_code: null, disabled_reasons: [] },
        flatten_and_pause: {
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: 'NO_OWNED_POSITIONS',
          disabled_reasons: ['NO_OWNED_POSITIONS'],
        },
        mark_poisoned: {
          enabled: true,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: null,
          disabled_reasons: [],
        },
      },
      trading_session: {
        phase: 'RTH',
        permits_strategy_activity: true,
        next_transition_ms: null,
        timezone: 'America/New_York',
        as_of_ms: 0,
      },
      readiness_gates: [],
      runtime_freshness: null,
      control_plane: null,
    },
    fetched_at_ms: 0,
  };
}

function makeStub(): Partial<LiveRunsService> {
  return {
    getInstances: vi.fn().mockResolvedValue([
      {
        strategy_instance_id: 'sid-x',
        process_state: 'running',
        readiness_verdict: 'BLOCKED',
        readiness_as_of_ms: 0,
      },
    ]),
    getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
    getAccountSummary: vi.fn().mockResolvedValue({
      account_id: 'DU1',
      account_identity: 'CONSISTENT',
      account_identity_reason_codes: [],
      contamination: {
        net_positions: {},
        explained_total: {},
        explained_by_instance: [],
        residual: {},
        verdict: 'clean',
        policy_blocks_starts: false,
        summary: 'clean',
      },
    }),
    setInstanceDesiredState: vi.fn(),
    flattenAndPause: vi.fn(),
    issueInstanceCommand: vi.fn(),
  };
}

async function renderShell(stub: Partial<LiveRunsService>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      provideHttpClient(),
      provideRouter([]),
      { provide: LiveRunsService, useValue: stub },
    ],
  });
  const fixture = TestBed.createComponent(CockpitShellComponent);
  fixture.detectChanges();
  // Flush the constructor-time refresh promises.
  await Promise.resolve();
  await Promise.resolve();
  fixture.detectChanges();
  return fixture;
}

describe('CockpitShellComponent', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders five independent indicators on the identity strip', async () => {
    const fixture = await renderShell(makeStub());
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    for (const id of [
      'indicator-process',
      'indicator-intent',
      'indicator-readiness',
      'indicator-broker',
      'indicator-safety',
    ]) {
      expect(el.querySelector(`[data-testid="${id}"]`)).toBeTruthy();
    }
  });

  it('disables Resume with the guarded reason on the title attribute', async () => {
    const fixture = await renderShell(makeStub());
    await fixture.whenStable();
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="action-resume"]',
    ) as HTMLButtonElement | null;
    expect(btn).toBeTruthy();
    expect(btn?.disabled).toBe(true);
    expect(btn?.getAttribute('title')).toBe('BROKER_SAFETY_UNSAFE');
  });

  it('renders runtime demotion reason codes prominently', async () => {
    const stub = makeStub();
    const status = makeStatus();
    const runtimeFreshness: OperatorSurfaceRuntimeFreshness = {
      posture_demoted: true,
      stale_reason_codes: ['COMMAND_LOOP_STALE', 'CONTROL_PLANE_LEASE_STALE'],
      command_loop: {
        state: 'STALE',
        age_ms: 4_000,
        stale_reason_codes: ['COMMAND_LOOP_STALE'],
      },
      broker: { state: 'FRESH', age_ms: 500, stale_reason_codes: [] },
      bar_loop: { state: 'FRESH', age_ms: 500, stale_reason_codes: [] },
      control_plane: {
        state: 'STALE',
        age_ms: 6_000,
        stale_reason_codes: ['CONTROL_PLANE_LEASE_STALE'],
      },
    };
    const demotedStatus = {
      ...status,
      operator_surface: {
        ...status.operator_surface,
        runtime_freshness: runtimeFreshness,
      },
    };
    stub.getInstanceStatus = vi.fn().mockResolvedValue(demotedStatus);

    const fixture = await renderShell(stub);
    await fixture.whenStable();
    fixture.detectChanges();

    const banner = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="runtime-freshness-banner"]',
    );
    expect(banner?.textContent).toContain('LAST-KNOWN');
    expect(banner?.textContent).toContain('COMMAND_LOOP_STALE');
    expect(banner?.textContent).toContain('CONTROL_PLANE_LEASE_STALE');
  });

  it('renders the Stop button only inside the overflow menu (canonical render-site rule)', async () => {
    const fixture = await renderShell(makeStub());
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const stops = el.querySelectorAll('[data-testid="action-stop"]');
    expect(stops.length).toBe(1);
    const overflow = el.querySelector('[data-testid="overflow-menu"]');
    expect(overflow?.contains(stops[0])).toBe(true);
  });

  it('does not render any Mark Poisoned trigger on the Status tab', async () => {
    const fixture = await renderShell(makeStub());
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('[data-testid="audit-mark-poisoned-trigger"]')).toBeNull();
  });

  // ── PRD #619-C4 — control-plane banner & local-transport-stale signal ──

  function makeControlPlane(
    overrides: Partial<OperatorSurfaceControlPlane> = {},
  ): OperatorSurfaceControlPlane {
    return {
      state: 'CONNECTED',
      last_transition_ms: 0,
      last_success_ms: 0,
      attempt: 0,
      daemon_boot_id: 'boot-A',
      notice: null,
      runbook_slug: null,
      ...overrides,
    };
  }

  async function renderWithControlPlane(
    cp: OperatorSurfaceControlPlane | null,
  ) {
    const stub = makeStub();
    const status = makeStatus();
    stub.getInstanceStatus = vi.fn().mockResolvedValue({
      ...status,
      operator_surface: { ...status.operator_surface, control_plane: cp },
    });
    return renderShell(stub);
  }

  it('hides the control-plane banner when the section is absent', async () => {
    const fixture = await renderWithControlPlane(null);
    await fixture.whenStable();
    fixture.detectChanges();

    const banner = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="control-plane-banner"]',
    );
    expect(banner).toBeNull();
  });

  it('hides the control-plane banner when the state is CONNECTED', async () => {
    const fixture = await renderWithControlPlane(makeControlPlane());
    await fixture.whenStable();
    fixture.detectChanges();

    const banner = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="control-plane-banner"]',
    );
    expect(banner).toBeNull();
  });

  it('renders a RETRYING banner with attempt count and server-authored notice', async () => {
    const fixture = await renderWithControlPlane(
      makeControlPlane({
        state: 'RETRYING',
        attempt: 3,
        notice: 'Host daemon connectivity is degraded; retrying.',
        runbook_slug: 'daemon-retrying',
      }),
    );
    await fixture.whenStable();
    fixture.detectChanges();

    const banner = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="control-plane-banner"]',
    );
    expect(banner).toBeTruthy();
    expect(banner?.textContent).toContain('ATTENTION');
    expect(banner?.textContent).toContain('Host daemon connectivity is degraded');
    const attempt = banner?.querySelector('[data-testid="control-plane-attempt"]');
    expect(attempt?.textContent).toContain('attempt 3');
    expect(banner?.classList.contains('demoted')).toBe(false);
  });

  it.each([
    ['UNREACHABLE'] as const,
    ['AUTH_FAILED'] as const,
    ['PROTOCOL_ERROR'] as const,
    ['INCOMPATIBLE_CONTRACT'] as const,
  ])(
    'renders a LAST-KNOWN demoted banner for %s',
    async (state) => {
      const fixture = await renderWithControlPlane(
        makeControlPlane({
          state,
          notice: 'server-authored notice',
          runbook_slug: 'daemon-' + state.toLowerCase(),
        }),
      );
      await fixture.whenStable();
      fixture.detectChanges();

      const banner = (fixture.nativeElement as HTMLElement).querySelector(
        '[data-testid="control-plane-banner"]',
      );
      expect(banner).toBeTruthy();
      expect(banner?.textContent).toContain('LAST-KNOWN');
      expect(banner?.textContent).toContain('server-authored notice');
      expect(banner?.classList.contains('demoted')).toBe(true);
      // Terminal kinds do NOT show an attempt count.
      expect(
        banner?.querySelector('[data-testid="control-plane-attempt"]'),
      ).toBeNull();
    },
  );

  it('localTransportStale is false on CONNECTED', async () => {
    const fixture = await renderWithControlPlane(makeControlPlane());
    await fixture.whenStable();
    fixture.detectChanges();

    expect(fixture.componentInstance.localTransportStale()).toBe(false);
  });

  it.each([
    ['RETRYING'] as const,
    ['UNREACHABLE'] as const,
    ['AUTH_FAILED'] as const,
  ])('localTransportStale is true on %s', async (state) => {
    const fixture = await renderWithControlPlane(makeControlPlane({ state }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(fixture.componentInstance.localTransportStale()).toBe(true);
  });

  it('localTransportStale is false when the control_plane section is absent', async () => {
    const fixture = await renderWithControlPlane(null);
    await fixture.whenStable();
    fixture.detectChanges();

    expect(fixture.componentInstance.localTransportStale()).toBe(false);
  });
});
