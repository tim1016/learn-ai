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
      host_process: {
        state: 'RUNNING',
        notice: null,
        copyable_command: null,
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'ALREADY_RUNNING',
        },
      },
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
      broker_observation_consistency: null,
      reconciliation: {
        state: 'NOT_AVAILABLE',
        failure_reason: null,
        adopted_intent_ids: [],
        last_reconcile_ms: null,
      },
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

  // ── ADR-0011 / ADR-0013 §1 — env chip honesty (P1-001) ──────────────

  async function renderShellWithSafetyVerdict(
    verdict: 'PAPER_ONLY' | 'UNSAFE' | 'UNKNOWN',
  ) {
    const stub = makeStub();
    const status = makeStatus();
    stub.getInstanceStatus = vi.fn().mockResolvedValue({
      ...status,
      operator_surface: {
        ...status.operator_surface,
        broker: { ...status.operator_surface.broker, safety_verdict: verdict },
      },
    });
    return renderShell(stub);
  }

  it.each([
    ['PAPER_ONLY', 'PAPER'] as const,
    ['UNSAFE', 'UNSAFE'] as const,
    ['UNKNOWN', 'UNKNOWN'] as const,
  ])(
    'env chip renders %s verdict as label "%s" (not synthesized from status truthiness)',
    async (verdict, expectedLabel) => {
      const fixture = await renderShellWithSafetyVerdict(verdict);
      await fixture.whenStable();
      fixture.detectChanges();

      const chip = (fixture.nativeElement as HTMLElement).querySelector(
        '[data-testid="env-chip"]',
      );
      expect(chip).toBeTruthy();
      expect(chip?.textContent?.trim()).toBe(expectedLabel);
      expect(chip?.getAttribute('data-value')).toBe(verdict);
    },
  );

  it('env chip is absent before the first status response (no claim is honest)', async () => {
    const stub = makeStub();
    // Make the first /status fetch hang so status() stays null.
    stub.getInstanceStatus = vi.fn().mockReturnValue(new Promise(() => undefined));
    const fixture = await renderShell(stub);
    await fixture.whenStable();
    fixture.detectChanges();

    const chip = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="env-chip"]',
    );
    expect(chip).toBeNull();
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

  it('exposes a Deploy new strategy link in the page utility row pointing at /broker/deploy', async () => {
    const fixture = await renderShell(makeStub());
    await fixture.whenStable();
    fixture.detectChanges();
    const link = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="deploy-new-strategy"]',
    ) as HTMLAnchorElement | null;
    expect(link).toBeTruthy();
    expect(link?.getAttribute('href')).toBe('/broker/deploy');
  });

  it('disables Resume and renders operator-language copy (not the raw reason code) on the title attribute', async () => {
    // P1/P2 audit 2026-06-22 — was: title === 'BROKER_SAFETY_UNSAFE'
    // (the operator saw the raw enum). Now: title === operator copy
    // resolved through the shared disabled-reason-copy map.
    const fixture = await renderShell(makeStub());
    await fixture.whenStable();
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="action-resume"]',
    ) as HTMLButtonElement | null;
    expect(btn).toBeTruthy();
    expect(btn?.disabled).toBe(true);
    const title = btn?.getAttribute('title') ?? '';
    // The raw code must NOT be the title (it was, pre-fix).
    expect(title).not.toBe('BROKER_SAFETY_UNSAFE');
    // The title must be operator-language and mention the UNSAFE
    // verdict + the paper-only restoration path.
    expect(title).toContain('UNSAFE');
    expect(title).toContain('paper-only');
  });

  it('renders runtime demotion reason codes prominently', async () => {
    const stub = makeStub();
    const status = makeStatus();
    const runtimeFreshness: OperatorSurfaceRuntimeFreshness = {
      posture_demoted: true,
      stale_reason_codes: ['COMMAND_LOOP_STALE', 'CONTROL_PLANE_LEASE_STALE'],
      headline: null,
      stale_reasons: [],
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

  // PRD #619-C4 follow-up (Codex P2) — wire the stale-transport signal
  // into the button disable predicates AND the dispatch methods so the
  // operator never fires a mutation into a known-broken channel.

  // 2026-06-22 audit F-R2: ADR-0004 amendment D — only Resume and
  // Flatten-and-pause are gated by control-plane/transport demotion.
  // Durable Pause and Stop must remain available so the operator's
  // fail-safe intent controls are not removed at the moment they are
  // most needed.

  it('disables Resume + Flatten when control_plane is RETRYING (ADR-0004 D asymmetry)', async () => {
    const fixture = await renderWithControlPlane(
      makeControlPlane({
        state: 'RETRYING',
        attempt: 1,
        notice: 'Daemon retrying.',
        runbook_slug: 'daemon-retrying',
      }),
    );
    await fixture.whenStable();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    for (const id of ['action-resume', 'action-flatten-and-pause']) {
      const btn = el.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement | null;
      expect(btn, `button ${id} should exist`).toBeTruthy();
      expect(btn?.disabled, `button ${id} should be disabled by transport gate`).toBe(true);
      const title = btn?.getAttribute('title') ?? '';
      expect(title).not.toBe('TRANSPORT_STALE');
      expect(title.toLowerCase()).toContain('transport');
      expect(title.toLowerCase()).toContain('connected');
    }
  });

  it('leaves Pause + Stop enabled when control_plane is RETRYING (ADR-0004 D fail-safe)', async () => {
    const fixture = await renderWithControlPlane(
      makeControlPlane({
        state: 'RETRYING',
        attempt: 1,
        notice: 'Daemon retrying.',
        runbook_slug: 'daemon-retrying',
      }),
    );
    await fixture.whenStable();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    for (const id of ['action-pause', 'action-stop']) {
      const btn = el.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement | null;
      expect(btn, `button ${id} should exist`).toBeTruthy();
      expect(
        btn?.disabled,
        `button ${id} must NOT be disabled by transport gate (ADR-0004 D fail-safe)`,
      ).toBe(false);
    }
  });

  it('dispatchPause fires through transport-stale (durable Pause is fail-safe)', async () => {
    const stub = makeStub();
    const status = makeStatus();
    status.operator_surface.actions.pause.enabled = true;
    stub.getInstanceStatus = vi.fn().mockResolvedValue({
      ...status,
      operator_surface: {
        ...status.operator_surface,
        control_plane: makeControlPlane({ state: 'UNREACHABLE' }),
      },
    });

    const fixture = await renderShell(stub);
    await fixture.whenStable();
    fixture.detectChanges();

    await fixture.componentInstance.dispatchPause();

    // ADR-0004 D — durable Pause MUST fire through. Removing the
    // operator's fail-safe intent controls during a control-plane
    // incident would be less safe.
    expect(stub.setInstanceDesiredState).toHaveBeenCalledOnce();
  });

  // ── ADR-0008 §5 cold-start reconciliation banner ────────────────────

  async function renderShellWithReconciliation(
    recon: {
      state: 'NOT_AVAILABLE' | 'IN_PROGRESS' | 'CLEAN' | 'ADOPTED' | 'STALE' | 'FAILED';
      failure_reason?: string | null;
      adopted_intent_ids?: string[];
      last_reconcile_ms?: number | null;
    },
  ) {
    const stub = makeStub();
    const status = makeStatus();
    stub.getInstanceStatus = vi.fn().mockResolvedValue({
      ...status,
      operator_surface: {
        ...status.operator_surface,
        reconciliation: {
          failure_reason: null,
          adopted_intent_ids: [],
          last_reconcile_ms: null,
          ...recon,
        },
      },
    });
    return renderShell(stub);
  }

  it('renders NO banner when reconciliation state is CLEAN', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'CLEAN' });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('[data-testid^="reconciliation-banner-"]')).toBeNull();
  });

  it('renders the ADOPTED banner with the recovered-order count', async () => {
    const fixture = await renderShellWithReconciliation({
      state: 'ADOPTED',
      adopted_intent_ids: ['iid-1', 'iid-2'],
    });
    const el = fixture.nativeElement as HTMLElement;
    const banner = el.querySelector('[data-testid="reconciliation-banner-adopted"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('RECONCILE · ADOPTED');
    expect(banner?.textContent).toContain('recovered 2 adopted broker orders');
  });

  it('renders the STALE banner when evidence is out of date', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'STALE' });
    const el = fixture.nativeElement as HTMLElement;
    const banner = el.querySelector('[data-testid="reconciliation-banner-stale"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('RECONCILE · STALE');
  });

  it('renders the NOT_AVAILABLE banner when no receipt has landed', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'NOT_AVAILABLE' });
    const el = fixture.nativeElement as HTMLElement;
    const banner = el.querySelector(
      '[data-testid="reconciliation-banner-not-available"]',
    );
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('RECONCILE · NOT AVAILABLE');
  });

  it('renders the FAILED banner with the failure reason', async () => {
    const fixture = await renderShellWithReconciliation({
      state: 'FAILED',
      failure_reason: 'broker_probe_failed',
    });
    const el = fixture.nativeElement as HTMLElement;
    const banner = el.querySelector('[data-testid="reconciliation-banner-failed"]');
    expect(banner).not.toBeNull();
    expect(banner?.getAttribute('role')).toBe('alert');
    expect(banner?.textContent).toContain('broker_probe_failed');
  });

  it('renders the IN_PROGRESS banner while reconciliation is running', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'IN_PROGRESS' });
    const el = fixture.nativeElement as HTMLElement;
    const banner = el.querySelector(
      '[data-testid="reconciliation-banner-in-progress"]',
    );
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('RECONCILE · IN PROGRESS');
  });

  // ── reconciliation PR 2 — "Reconcile now" button ──────────────────────

  it('renders Reconcile-now button when reconciliation state is STALE', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'STALE' });
    const el = fixture.nativeElement as HTMLElement;
    const btn = el.querySelector(
      '[data-testid="cockpit-reconcile-now-button"]',
    ) as HTMLButtonElement | null;
    expect(btn).not.toBeNull();
    expect(btn?.disabled).toBe(false);
    expect(btn?.textContent?.trim()).toBe('Reconcile now');
  });

  it('renders Reconcile-now button when reconciliation state is NOT_AVAILABLE', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'NOT_AVAILABLE' });
    const el = fixture.nativeElement as HTMLElement;
    const btn = el.querySelector(
      '[data-testid="cockpit-reconcile-now-button"]',
    ) as HTMLButtonElement | null;
    expect(btn).not.toBeNull();
    expect(btn?.disabled).toBe(false);
  });

  it('hides Reconcile-now button when reconciliation state is CLEAN', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'CLEAN' });
    const el = fixture.nativeElement as HTMLElement;
    expect(
      el.querySelector('[data-testid="cockpit-reconcile-now-button"]'),
    ).toBeNull();
  });

  it('hides Reconcile-now button when reconciliation state is FAILED', async () => {
    const fixture = await renderShellWithReconciliation({
      state: 'FAILED',
      failure_reason: 'broker_probe_failed',
    });
    const el = fixture.nativeElement as HTMLElement;
    expect(
      el.querySelector('[data-testid="cockpit-reconcile-now-button"]'),
    ).toBeNull();
  });

  it('hides Reconcile-now button when reconciliation state is IN_PROGRESS', async () => {
    const fixture = await renderShellWithReconciliation({ state: 'IN_PROGRESS' });
    const el = fixture.nativeElement as HTMLElement;
    // The IN_PROGRESS banner renders without the button — the verb is
    // already in flight, so a second click would race.
    expect(
      el.querySelector('[data-testid="cockpit-reconcile-now-button"]'),
    ).toBeNull();
  });

  it('click invokes LiveRunsService.reconcileInstance with the sid', async () => {
    const stub = makeStub();
    const status = makeStatus();
    stub.getInstanceStatus = vi.fn().mockResolvedValue({
      ...status,
      operator_surface: {
        ...status.operator_surface,
        reconciliation: {
          state: 'STALE',
          failure_reason: null,
          adopted_intent_ids: [],
          last_reconcile_ms: null,
        },
      },
    });
    const reconcileSpy = vi.fn().mockResolvedValue({
      request_id: 'aaaaaaaaaaaaaaaaaaaaaa',
      accepted_at_ms: 1,
    });
    (stub as unknown as { reconcileInstance: typeof reconcileSpy }).reconcileInstance =
      reconcileSpy;
    const fixture = await renderShell(stub);
    await fixture.whenStable();
    fixture.detectChanges();

    const btn = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="cockpit-reconcile-now-button"]',
    ) as HTMLButtonElement;
    btn.click();
    await fixture.whenStable();

    expect(reconcileSpy).toHaveBeenCalledWith('sid-x');
  });
});
