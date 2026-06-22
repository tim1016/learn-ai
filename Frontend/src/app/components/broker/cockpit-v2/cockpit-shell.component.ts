// PRD #617 — atomic cockpit replacement.
//
// Owns the operator's primary control surface for live instances:
// page utility row, account/fleet summary, outer instance tabs, identity
// strip (with destructive-action overflow), optional ADR-0008 hazard
// line, and the inner-tab routing for Status & Risk / Activity / Audit /
// Configuration.
//
// Every operational verdict, action eligibility, and remediation hint
// comes from the server-authored `operator_surface` projection.
// Angular formats evidence and maps closed-enum classifications to
// display copy; it never derives operational judgments
// (ADR-0013 §1, the verbatim rule).

import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  inject,
  signal,
  type Signal,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';

import type {
  FleetAccountSummary,
  LiveInstanceStatus,
  LiveInstanceSummary,
  OperatorGate,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';

import { projectAccountAttention } from './lib/account-summary-attention';
import { ClockSync } from './lib/clock-sync';
import {
  DEFAULT_INSTANCE_TAB_STATE,
  type InnerTab,
  type InstanceTabState,
  reduceOnInstanceFocused,
  reduceOnTabSelected,
  reduceOnVerdictObserved,
} from './lib/instance-tab-state';
import { ActivityTabComponent } from './tabs/activity-tab.component';
import { AuditTabComponent } from './tabs/audit-tab.component';
import { ConfigurationTabComponent } from './tabs/configuration-tab.component';
import { StatusRiskTabComponent } from './tabs/status-risk-tab.component';
import { TypedHaltConfirmComponent } from './reused/typed-halt-confirm/typed-halt-confirm.component';

const POLL_INTERVAL_MS = 4_000;
const TICK_INTERVAL_MS = 1_000;

const READINESS_LABEL: Record<string, string> = {
  READY: 'READY',
  BLOCKED: 'BLOCKED',
  DEGRADED: 'DEGRADED',
  UNKNOWN: 'UNKNOWN',
};

@Component({
  selector: 'app-cockpit-shell',
  standalone: true,
  imports: [
    CommonModule,
    StatusRiskTabComponent,
    ActivityTabComponent,
    AuditTabComponent,
    ConfigurationTabComponent,
    TypedHaltConfirmComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './cockpit-shell.component.html',
  styleUrl: './cockpit-shell.component.scss',
})
export class CockpitShellComponent {
  private readonly _live = inject(LiveRunsService);
  private readonly _route = inject(ActivatedRoute);
  private readonly _router = inject(Router);
  private readonly _destroyRef = inject(DestroyRef);

  readonly summaries = signal<LiveInstanceSummary[]>([]);
  readonly status = signal<LiveInstanceStatus | null>(null);
  readonly accountSummary = signal<FleetAccountSummary | null>(null);
  readonly accountSummaryExpanded = signal<boolean>(false);
  // PRD #619-A §A5 — per-resource error signals. A successful refresh
  // of one resource clears that resource's error only; a transport
  // timeout on /status does not erase the user's view that
  // /account-summary failed two seconds ago. The template-facing
  // ``errorMessage`` is the first non-null in a documented priority
  // order (status → summaries → account_summary → mutation) so the
  // banner still surfaces a single message but the underlying state
  // is per-resource.
  readonly statusError = signal<string | null>(null);
  readonly summariesError = signal<string | null>(null);
  readonly accountSummaryError = signal<string | null>(null);
  readonly mutationError = signal<string | null>(null);
  readonly errorMessage = computed<string | null>(
    () =>
      this.mutationError() ??
      this.statusError() ??
      this.summariesError() ??
      this.accountSummaryError(),
  );
  readonly busyAction = signal<string | null>(null);
  readonly typedHaltOpen = signal<boolean>(false);

  // Per-instance tab state keyed by strategy_instance_id.
  private _tabStateMap = new Map<string, InstanceTabState>();
  readonly tabStateVersion = signal<number>(0);
  // Foreground instance (driven by route :id or first summary).
  readonly selectedInstanceId = signal<string | null>(null);
  // Local-clock tick for the identity-strip clock pill.
  readonly tickNow = signal<number>(Date.now());
  // Browser focus / visibility — drives the refresh-on-resume rule.
  readonly browserFocused = signal<boolean>(true);

  // Clock sync utility — captured per status response.
  private readonly _clock = new ClockSync(() => Date.now());

  readonly accountAttention = computed(() => {
    const s = this.accountSummary();
    if (!s) {
      return { isAttention: false, isCollapsible: true };
    }
    return projectAccountAttention(s);
  });

  readonly accountRowExpanded = computed(() => {
    const att = this.accountAttention();
    if (att.isAttention) {
      return true;
    }
    return this.accountSummaryExpanded();
  });

  readonly currentInstanceTabState = computed<InstanceTabState>(() => {
    void this.tabStateVersion();
    const id = this.selectedInstanceId();
    if (!id) return DEFAULT_INSTANCE_TAB_STATE;
    return this._tabStateMap.get(id) ?? DEFAULT_INSTANCE_TAB_STATE;
  });

  readonly selectedTab: Signal<InnerTab> = computed(() => this.currentInstanceTabState().selectedTab);

  readonly clockPill = computed(() => {
    void this.tickNow();
    const snap = this._clock.snapshot();
    return {
      serverNowMs: snap.serverNowMs,
      advisory: snap.advisory,
      offsetSeconds: Math.round(snap.offsetMs / 1000),
    };
  });

  readonly outerTabs = computed(() => {
    void this.tabStateVersion();
    return this.summaries().map((s) => {
      const tab = this._tabStateMap.get(s.strategy_instance_id);
      const attentionUnseen = tab?.attentionUnseen ?? false;
      return {
        id: s.strategy_instance_id,
        process_state: s.process_state,
        readiness_verdict: s.readiness_verdict ?? 'UNKNOWN',
        attentionUnseen,
        readinessLabel: READINESS_LABEL[s.readiness_verdict ?? 'UNKNOWN'] ?? 'UNKNOWN',
      };
    });
  });

  readonly isPoisoned = computed(() => {
    const s = this.status();
    return !!(s && s.last_exit && s.last_exit.halt_trigger);
  });

  /** PRD #619-C4 — host-daemon control-plane banner.
   *
   *  Returns ``null`` when the control plane is healthy (CONNECTED) or
   *  the section is absent (data plane booted without a daemon URL —
   *  the cockpit hides the card). Otherwise returns a backend-authored
   *  payload the template renders verbatim.
   *
   *  - ``RETRYING`` → amber ATTENTION banner with attempt count.
   *  - everything else terminal (UNREACHABLE / AUTH_FAILED /
   *    PROTOCOL_ERROR / INCOMPATIBLE_CONTRACT) → red LAST-KNOWN banner.
   */
  readonly controlPlaneBanner = computed(() => {
    const cp = this.status()?.operator_surface.control_plane ?? null;
    if (cp === null || cp.state === 'CONNECTED') return null;
    const demoted = cp.state !== 'RETRYING';
    const label = demoted ? 'LAST-KNOWN' : 'ATTENTION';
    const attemptText = cp.state === 'RETRYING' && cp.attempt > 0
      ? `retrying · attempt ${cp.attempt}`
      : null;
    return {
      state: cp.state,
      label,
      demoted,
      notice: cp.notice,
      runbookSlug: cp.runbook_slug,
      attemptText,
    };
  });

  /** True when local transport state suggests the operator should not
   *  dispatch new mutations (control plane is not CONNECTED). Server
   *  gates remain authoritative if a request reaches the backend; this
   *  signal is for short-circuiting click handlers locally so the
   *  operator doesn't fire commands into a known-broken channel. */
  readonly localTransportStale = computed(() => {
    const cp = this.status()?.operator_surface.control_plane ?? null;
    return cp !== null && cp.state !== 'CONNECTED';
  });

  constructor() {
    // PRD #619-A §A5 — explicit lifecycle. Every timer, every DOM
    // listener, and the recursive poll scheduler are registered with
    // ``DestroyRef.onDestroy`` so the cockpit leaves no leaked timer
    // or listener behind when navigated away from. The poll loop uses
    // schedule-after-completion (a single in-flight ``_pollTick`` at
    // a time) so an async refresh that takes longer than the cadence
    // cannot pile up overlapping fetches.

    // Initial mount: kick off independent fetches in parallel.
    void this._refreshSummaries();
    void this._refreshAccountSummary();

    this._wireRouteSelection();
    this._wireClockTick();
    this._wirePollLoop();
    this._wireFocusRefresh();
    this._wireSessionBoundaryRefresh();
  }

  /** Drive route-id-aware instance selection on every navigation. */
  private _wireRouteSelection(): void {
    this._route.paramMap.pipe(takeUntilDestroyed()).subscribe((params) => {
      const id = params.get('id');
      if (id) this._selectInstance(id);
    });
  }

  /** Per-second tick for the identity-strip clock pill. */
  private _wireClockTick(): void {
    const handle = setInterval(() => this.tickNow.set(Date.now()), TICK_INTERVAL_MS);
    this._destroyRef.onDestroy(() => clearInterval(handle));
  }

  /** Serialized poll loop (status + summaries + account-summary).
   *
   * Each tick awaits the in-flight ``_pollTick`` before scheduling the
   * next ``setTimeout`` so overlapping async callbacks cannot pile up
   * when the network is slow. The stop flag is a closure local — it
   * never leaks onto the class because the only consumer is the
   * recursive call inside this method. */
  private _wirePollLoop(): void {
    let stopped = false;
    let handle: ReturnType<typeof setTimeout> | null = null;
    const schedule = (): void => {
      if (stopped) return;
      handle = setTimeout(() => {
        void this._pollTick().finally(() => {
          if (!stopped) schedule();
        });
      }, POLL_INTERVAL_MS);
    };
    this._destroyRef.onDestroy(() => {
      stopped = true;
      if (handle !== null) clearTimeout(handle);
    });
    schedule();
  }

  /** Browser focus / tab visibility refresh. PRD #619-A: the account
   * summary is part of the focus refresh too (the original code only
   * re-polled status + summaries, leaving stale account data visible
   * after a long blur). */
  private _wireFocusRefresh(): void {
    const onFocus = (): void => {
      this.browserFocused.set(true);
      const id = this.selectedInstanceId();
      if (id) void this._refreshStatus(id);
      void this._refreshSummaries();
      void this._refreshAccountSummary();
    };
    const onBlur = (): void => this.browserFocused.set(false);
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') onFocus();
    };
    window.addEventListener('focus', onFocus);
    window.addEventListener('blur', onBlur);
    document.addEventListener('visibilitychange', onVisibility);
    this._destroyRef.onDestroy(() => {
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('blur', onBlur);
      document.removeEventListener('visibilitychange', onVisibility);
    });
  }

  /** Boundary-aligned refresh after a session transition. Each
   * ``effect`` run owns its own pair of timeouts via ``onCleanup`` —
   * Angular runs the cleanup before the next reactive pass AND on
   * component destroy, so no manual lifecycle bookkeeping is needed. */
  private _wireSessionBoundaryRefresh(): void {
    effect((onCleanup) => {
      const s = this.status();
      if (!s) return;
      const { earlyMs, boundaryMs } = this._clock.scheduleBoundaryRefresh(
        s.operator_surface.trading_session.next_transition_ms,
      );
      let earlyTimer: ReturnType<typeof setTimeout> | null = null;
      let boundaryTimer: ReturnType<typeof setTimeout> | null = null;
      if (earlyMs !== null) {
        earlyTimer = setTimeout(() => {
          const id = this.selectedInstanceId();
          if (id) void this._refreshStatus(id);
        }, earlyMs);
      }
      if (boundaryMs !== null) {
        boundaryTimer = setTimeout(() => {
          const id = this.selectedInstanceId();
          if (id) void this._refreshStatus(id);
          void this._refreshSummaries();
        }, boundaryMs);
      }
      onCleanup(() => {
        if (earlyTimer !== null) clearTimeout(earlyTimer);
        if (boundaryTimer !== null) clearTimeout(boundaryTimer);
      });
    });
  }

  /** Single serialized poll tick — status (foreground instance) +
   * summaries + account summary. Independent refreshes run in
   * parallel within one tick; ``Promise.allSettled`` ensures one
   * resource's failure does not cancel the others. */
  private async _pollTick(): Promise<void> {
    const id = this.selectedInstanceId();
    const work: Promise<unknown>[] = [
      this._refreshSummaries(),
      this._refreshAccountSummary(),
    ];
    if (id) work.push(this._refreshStatus(id));
    await Promise.allSettled(work);
  }

  selectTab(tab: InnerTab): void {
    const id = this.selectedInstanceId();
    if (!id) return;
    const prior = this._tabStateMap.get(id) ?? DEFAULT_INSTANCE_TAB_STATE;
    this._tabStateMap.set(id, reduceOnTabSelected(prior, tab));
    this.tabStateVersion.update((v) => v + 1);
  }

  selectInstance(id: string): void {
    this._selectInstance(id);
    void this._router.navigate(['/broker/instances', id], { replaceUrl: true });
  }

  toggleAccountSummary(): void {
    if (!this.accountAttention().isCollapsible) return;
    this.accountSummaryExpanded.update((v) => !v);
  }

  // ── operator dispatch (operator_surface.actions) ──────────────────────

  /** PRD #619-C4 — local fail-closed guard. Returns true when the click
   *  must NOT proceed because the data plane reports the host daemon as
   *  not CONNECTED. Both the button disable predicate (visual) and the
   *  dispatch methods (defense) consult this, so a programmatic call
   *  bypassing the button still no-ops. */
  private _refuseOnStaleTransport(operationLabel: string): boolean {
    if (!this.localTransportStale()) return false;
    this.mutationError.set(
      `${operationLabel} is paused: the host daemon transport is not currently connected. ` +
        'Refresh the cockpit once the control plane recovers.',
    );
    return true;
  }

  async dispatchResume(): Promise<void> {
    if (this._refuseOnStaleTransport('Resume')) return;
    await this._setIntent('resume', 'Resume');
  }

  async dispatchPause(): Promise<void> {
    if (this._refuseOnStaleTransport('Pause')) return;
    await this._setIntent('pause', 'Pause');
  }

  async dispatchStop(): Promise<void> {
    const surface = this.status()?.operator_surface;
    // PRD #619-A §A6 — ``stop`` is required on ``OperatorSurfaceActions``;
    // no optional-chaining fallback needed.
    if (!surface?.actions.stop.enabled) return;
    if (this._refuseOnStaleTransport('Stop')) return;
    const confirmed = window.confirm(
      'Stop instance?\n\n' +
        '• Durable intent becomes STOPPED.\n' +
        '• The running subprocess exits.\n' +
        '• STOP does not flatten positions.\n' +
        '• Resumption is intentionally heavyweight (Redeploy).\n' +
        '• Open positions remain unless you first use Flatten and pause.',
    );
    if (!confirmed) return;
    await this._setIntent('stop', 'Stop');
  }

  async dispatchFlattenAndPause(): Promise<void> {
    const surface = this.status()?.operator_surface;
    const cap = surface?.actions.flatten_and_pause;
    if (!cap?.enabled) return;
    if (this._refuseOnStaleTransport('Flatten and pause')) return;
    const id = this.selectedInstanceId();
    if (!id) return;
    this.busyAction.set('flatten_and_pause');
    this.mutationError.set(null);
    try {
      await this._live.flattenAndPause(id, {
        action: 'pause',
        reason: 'Flatten and pause',
        updated_by: 'operator',
      });
      await this._refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this._humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  openTypedHalt(): void {
    if (!this.status()?.operator_surface.actions.mark_poisoned.enabled) return;
    if (this._refuseOnStaleTransport('Mark poisoned')) return;
    this.typedHaltOpen.set(true);
  }

  closeTypedHalt(): void {
    this.typedHaltOpen.set(false);
  }

  async confirmTypedHalt(): Promise<void> {
    const id = this.selectedInstanceId();
    if (!id) return;
    this.busyAction.set('mark_poisoned');
    this.mutationError.set(null);
    this.typedHaltOpen.set(false);
    try {
      await this._live.issueInstanceCommand(id, { verb: 'MARK_POISONED' });
      await this._refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this._humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  // ── gate-action dispatch (suggested-action renderer) ────────────────

  onGateInvokeCapability(capability: 'resume' | 'pause'): void {
    if (capability === 'resume') void this.dispatchResume();
    else void this.dispatchPause();
  }

  onGateFocusAction(focus: { tab: InnerTab; action: string }): void {
    this.selectTab(focus.tab);
  }

  onGateRedeploy(): void {
    const s = this.status();
    if (!s) return;
    void this._router.navigate(['/broker/deploy'], { queryParams: this._redeployQueryParams(s) });
  }

  onGateOpenRunbook(slug: string): void {
    window.open(`/runbooks/${encodeURIComponent(slug)}`, '_blank', 'noopener');
  }

  redeployQueryParams(): Record<string, string> {
    const s = this.status();
    if (!s) return {};
    return this._redeployQueryParams(s);
  }

  // ── presentation helpers ────────────────────────────────────────────

  formatClock(ms: number): string {
    const d = new Date(ms);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
  }

  intentLabel(value: string | null | undefined): string {
    return value ?? 'UNKNOWN';
  }

  failingReadinessGates(): OperatorGate[] {
    const gates = this.status()?.operator_surface.readiness_gates ?? [];
    return gates.filter((g) => g.status !== 'pass');
  }

  // ── private — server I/O ────────────────────────────────────────────

  private _selectInstance(id: string): void {
    this.selectedInstanceId.set(id);
    void this._refreshStatus(id);
    // On focus-switch, apply the focus-instance reducer to clear
    // attentionUnseen and (when applicable) force the Status tab.
    const summary = this.summaries().find((s) => s.strategy_instance_id === id);
    if (summary) {
      const prior = this._tabStateMap.get(id) ?? DEFAULT_INSTANCE_TAB_STATE;
      const next = reduceOnInstanceFocused(prior, summary.readiness_verdict ?? 'UNKNOWN');
      this._tabStateMap.set(id, next);
      this.tabStateVersion.update((v) => v + 1);
    }
  }

  private async _refreshSummaries(): Promise<void> {
    try {
      const rows = await this._live.getInstances();
      this.summaries.set(rows);
      // PRD #619-A §A5 — clear *this* resource's error on success.
      // Other resources' errors stay visible until they themselves
      // recover (or fail again).
      this.summariesError.set(null);
      // Apply readiness-transition reducer to every row.
      const foregroundId = this.selectedInstanceId();
      for (const row of rows) {
        const prior =
          this._tabStateMap.get(row.strategy_instance_id) ?? DEFAULT_INSTANCE_TAB_STATE;
        const { state } = reduceOnVerdictObserved(
          prior,
          row.readiness_verdict ?? 'UNKNOWN',
          row.strategy_instance_id === foregroundId,
        );
        this._tabStateMap.set(row.strategy_instance_id, state);
      }
      this.tabStateVersion.update((v) => v + 1);
      // First-mount: auto-select first instance if no route id.
      if (!this.selectedInstanceId() && rows.length) {
        const fromRoute = this._route.snapshot.paramMap.get('id');
        const chosen =
          (fromRoute && rows.find((r) => r.strategy_instance_id === fromRoute)?.strategy_instance_id) ||
          rows[0].strategy_instance_id;
        this._selectInstance(chosen);
      }
    } catch (err) {
      this.summariesError.set(this._humanError(err));
    }
  }

  private async _refreshStatus(id: string): Promise<void> {
    try {
      const s = await this._live.getInstanceStatus(id);
      this.status.set(s);
      this._clock.observe(s.fetched_at_ms);
      this.statusError.set(null);
    } catch (err) {
      this.statusError.set(this._humanError(err));
    }
  }

  private async _refreshAccountSummary(): Promise<void> {
    try {
      this.accountSummary.set(await this._live.getAccountSummary());
      this.accountSummaryError.set(null);
    } catch (err) {
      this.accountSummaryError.set(this._humanError(err));
    }
  }

  private async _setIntent(
    action: 'resume' | 'pause' | 'stop',
    label: string,
  ): Promise<void> {
    const id = this.selectedInstanceId();
    if (!id) return;
    this.busyAction.set(action);
    this.mutationError.set(null);
    try {
      await this._live.setInstanceDesiredState(id, { action, reason: label, updated_by: 'operator' });
      await this._refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this._humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private _redeployQueryParams(s: LiveInstanceStatus): Record<string, string> {
    const params: Record<string, string> = {};
    if (s.provenance) {
      if (s.provenance.strategy_spec_path) params['spec'] = s.provenance.strategy_spec_path;
      if (s.provenance.qc_audit_copy_path) params['audit'] = s.provenance.qc_audit_copy_path;
      if (s.provenance.qc_cloud_backtest_id)
        params['backtest_id'] = s.provenance.qc_cloud_backtest_id;
      if (s.provenance.account_id) params['account'] = s.provenance.account_id;
      params['parent_run_id'] = s.provenance.run_id;
      params['strategy_instance_id'] = s.strategy_instance_id;
    }
    if (s.start_defaults?.strategy) params['strategy'] = s.start_defaults.strategy;
    return params;
  }

  private _humanError(err: unknown): string {
    if (err && typeof err === 'object' && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return String(err);
  }
}
