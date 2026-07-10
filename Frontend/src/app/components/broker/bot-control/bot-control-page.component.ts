import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';

import type {
  BotLifecycleAction,
  BotLifecycleActionId,
  LifecycleProjectionEventRow,
  LiveInstanceStatus,
  TraderPrimaryRemediation,
} from '../../../api/live-instances.types';
import type { OperatorMove } from '../../../api/operator-blocker.types';
import type { HostRunnerStartRequest } from '../../../api/live-runs.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { ActiveBotSidebarNoticeService } from '../../../shell/active-bot-sidebar-notice.service';
import type { ActiveBotSidebarNotice } from '../../../shell/active-bot-sidebar-notice.service';
import { VerdictCardComponent } from './verdict-card/verdict-card.component';
import { TraderGuidancePaneComponent } from './overview-tab/trader-guidance-pane.component';
import { TypedHaltConfirmComponent } from './reused/typed-halt-confirm/typed-halt-confirm.component';
import { redeployQueryParamsForStatus } from './lib/redeploy-query-params';
import { resolveOperatorRunbookRoute } from './lib/operator-runbook-routes';
import { canStartHostProcess } from './lib/start-host-process';
import {
  renderTraderRemediation,
  type RenderedAction,
  type RendererDispatch,
} from './lib/suggested-action-renderer';
import { toOperationError, type OperationKind } from '../operation-error';
import {
  botSurfaceStreamEnabled,
  openBotSurfaceStream,
  shouldAcceptSurfaceSnapshot,
  type BotSurfaceStream,
} from './lib/bot-surface-stream';

const POLL_INTERVAL_MS = 4_000;
const POISONED_CONFIRM_MESSAGE =
  'Flagging this instance as POISONED is IRREVERSIBLE: the current run can never resume on its run_id. Recovery requires a fresh deployment (new run_id) after you reconcile the account.';
const CRASH_RECOVERY_CONFIRM_MESSAGE =
  'Recording recovery evidence clears the crash-retired start gate and lets this bot run again. Only confirm if you have verified in IBKR that the broker account is FLAT with no open orders. This writes audited safety evidence.';
const RETIRE_REPLACE_CONFIRM_MESSAGE =
  'Retire & Replace permanently retires this bot instance, then opens replacement deploy with the current lineage. Confirm only after you have verified the broker account is flat with no open orders.';
const REMOVE_BOT_CONFIRM_MESSAGE =
  'Remove hides this bot from the catalog with a soft-delete marker. The underlying audit files stay on disk, but this bot will no longer appear in the active bot list.';

@Component({
  selector: 'app-bot-control-page',
  imports: [VerdictCardComponent, TraderGuidancePaneComponent, TypedHaltConfirmComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-control-page.component.html',
  styleUrl: './bot-control-page.component.scss',
})
export class BotControlPageComponent {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly activeBotSidebarNotice = inject(ActiveBotSidebarNoticeService);
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private pollToken = 0;
  private statusRequestSeq = 0;
  private destroyed = false;
  private surfaceStream: BotSurfaceStream | null = null;

  readonly instanceId = signal<string | null>(null);
  readonly status = signal<LiveInstanceStatus | null>(null);
  readonly statusError = signal<string | null>(null);
  readonly mutationError = signal<string | null>(null);
  readonly timelineRows = signal<LifecycleProjectionEventRow[]>([]);
  readonly timelineProjectionAvailable = signal<boolean>(false);
  readonly timelineCanonicalFallbackRequired = signal<boolean>(true);
  readonly timelineNotice = signal<string | null>(null);
  readonly busyAction = signal<string | null>(null);
  readonly typedHaltOpen = signal<boolean>(false);
  private readonly typedHaltInstanceId = signal<string | null>(null);
  readonly crashRecoveryConfirmOpen = signal<boolean>(false);
  readonly retireReplaceConfirmOpen = signal<boolean>(false);
  readonly removeBotConfirmOpen = signal<boolean>(false);
  readonly poisonedConfirmMessage = POISONED_CONFIRM_MESSAGE;
  readonly crashRecoveryConfirmMessage = CRASH_RECOVERY_CONFIRM_MESSAGE;
  readonly retireReplaceConfirmMessage = RETIRE_REPLACE_CONFIRM_MESSAGE;
  readonly removeBotConfirmMessage = REMOVE_BOT_CONFIRM_MESSAGE;

  readonly errorMessage = computed<string | null>(
    () => this.mutationError() ?? this.statusError(),
  );

  readonly hostRunnerNotice = computed<ActiveBotSidebarNotice | null>(() => {
    const hostProcess = this.status()?.operator_surface.host_process ?? null;
    if (hostProcess?.state === 'WAITING_FOR_HOST') {
      const cap = hostProcess.start_capability;
      return {
        instanceId: this.instanceId() ?? '',
        kind: 'live-binding-invalid',
        summary: 'Live binding invalid.',
        message:
          hostProcess.notice ?? 'This bot has no active live binding. Bind again before trading.',
        command: null,
        action: canStartHostProcess(cap)
          ? {
              label: 'Bind again',
              busyLabel: 'Binding...',
              runId: cap.run_id,
              request: cap.request,
            }
          : null,
      };
    }
    if (hostProcess?.state !== 'UNREACHABLE') return null;
    return {
      instanceId: this.instanceId() ?? '',
      kind: 'host-runner-unreachable',
      summary: 'Warning, host runner unreachable.',
      message: hostProcess.notice ?? 'The host runner cannot be reached for this bot.',
      command: hostProcess.copyable_command,
      action: null,
    };
  });

  readonly traderGuidance = computed(
    () => this.status()?.operator_surface.trader_guidance ?? null,
  );
  readonly renderedPrimaryRemediation = computed<RenderedAction | null>(() =>
    renderTraderRemediation(
      this.traderGuidance()?.primary_remediation ?? null,
      this.primaryRemediationDispatch,
    ),
  );

  private readonly primaryRemediationDispatch: RendererDispatch = {
    invokeCapability: (capability) => {
      if (capability === 'resume') void this.dispatchResumeIntent();
      else void this.dispatchEndDayNow();
    },
    // Destructive focus actions have no lifecycle-chart node in the redesign; the
    // confirm dialog is now their canonical render site. Flatten-and-pause is an
    // account-scoped recovery, so it routes to the Account Monitor.
    focus: (_tab, action) => {
      if (action === 'mark_poisoned') this.openTypedHalt();
      else if (action === 'stop') void this.dispatchStop();
      else this.openAccountMonitor();
    },
    redeploy: () => this.onGateRedeploy(),
    openRunbook: (slug) => this.onGateOpenRunbook(slug),
    invokeEndpoint: (endpoint) => {
      if (endpoint === 'reconcile_instance') void this.dispatchReconcileNow();
    },
  };

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed()).subscribe((params) => {
      const id = params.get('id');
      const token = ++this.pollToken;
      this.clearPollTimer();
      this.closeSurfaceStream();
      this.instanceId.set(id);
      this.status.set(null);
      this.statusError.set(null);
      this.mutationError.set(null);
      this.timelineRows.set([]);
      this.timelineProjectionAvailable.set(false);
      this.timelineCanonicalFallbackRequired.set(true);
      this.timelineNotice.set(null);
      this.busyAction.set(null);
      this.typedHaltOpen.set(false);
      this.typedHaltInstanceId.set(null);
      this.crashRecoveryConfirmOpen.set(false);
      this.retireReplaceConfirmOpen.set(false);
      this.removeBotConfirmOpen.set(false);
      if (id) {
        void this.refreshStatus(id).finally(() => {
          if (botSurfaceStreamEnabled()) this.openSurfaceStream(id, token);
          else this.scheduleNextPoll(id, token);
        });
      }
    });
    effect(() => {
      const id = this.instanceId();
      const notice = this.hostRunnerNotice();
      this.activeBotSidebarNotice.setNotice(
        id && notice
          ? {
              instanceId: id,
              kind: notice.kind,
              summary: notice.summary,
              message: notice.message,
              command: notice.command,
              action: notice.action,
            }
          : null,
      );
    });
    this.destroyRef.onDestroy(() => {
      this.destroyed = true;
      this.pollToken += 1;
      this.clearPollTimer();
      this.closeSurfaceStream();
      this.activeBotSidebarNotice.clearForInstance(this.instanceId());
    });
  }

  dispatchOverviewAction(action: BotLifecycleActionId): void {
    switch (action) {
      case 'confirm_start':
        void this.dispatchStartProcess();
        break;
      case 'end_day_now':
        void this.dispatchEndDayNow();
        break;
      case 'add_to_roster':
        void this.dispatchRosterChange(true);
        break;
      case 'take_off_roster':
        void this.dispatchRosterChange(false);
        break;
      case 'retire_replace':
        this.openRetireReplaceConfirm();
        break;
      default: {
        const unreachable: never = action;
        this.mutationError.set(`Unsupported lifecycle action: ${String(unreachable)}`);
      }
    }
  }

  invokePrimaryRemediation(): void {
    this.renderedPrimaryRemediation()?.invoke();
  }

  invokeTraderRemediation(remediation: TraderPrimaryRemediation): void {
    renderTraderRemediation(remediation, this.primaryRemediationDispatch)?.invoke();
  }

  onSettingsRequested(): void {
    this.onGateRedeploy();
  }

  async dispatchStartProcess(): Promise<void> {
    const id = this.instanceId();
    const cap = this.status()?.operator_surface.host_process.start_capability;
    if (!id || this.busyAction() || !cap || !canStartHostProcess(cap)) return;
    const request = this.startRequestWithRollCallOffer(cap.request);
    if (!request) {
      this.mutationError.set('Run roll call before starting this bot.');
      return;
    }
    this.busyAction.set('start_process');
    this.mutationError.set(null);
    try {
      await this.liveRuns.startHostRunner(cap.run_id, request);
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('start', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  // Opening the attestation dialog is the only entry point. Recording recovery
  // evidence clears a safety gate, so the operator must explicitly confirm the
  // account is flat before we post confirm_account_flat — never on a bare click.
  dispatchCrashRecoveryOverride(): void {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.crashRecoveryConfirmOpen.set(true);
  }

  cancelCrashRecoveryConfirm(): void {
    this.crashRecoveryConfirmOpen.set(false);
  }

  async confirmCrashRecoveryOverride(): Promise<void> {
    if (!this.crashRecoveryConfirmOpen()) return;
    this.crashRecoveryConfirmOpen.set(false);
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('crash_recovery_override');
    this.mutationError.set(null);
    try {
      await this.liveRuns.recordCrashRecoveryOverride(id, {
        confirm_account_flat: true,
        approved_by: 'operator',
      });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('recovery-override', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchStop(): Promise<void> {
    await this.setIntent('stop', 'Stop');
  }

  async dispatchResumeIntent(): Promise<void> {
    await this.setIntent('resume', 'Resume');
  }

  async dispatchEndDayNow(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('end_day_now');
    this.mutationError.set(null);
    try {
      await this.liveRuns.endDayNow(id, { force: false });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('stop', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchRosterChange(onRoster: boolean): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    const action: BotLifecycleActionId = onRoster ? 'add_to_roster' : 'take_off_roster';
    this.busyAction.set(action);
    this.mutationError.set(null);
    try {
      await this.liveRuns.setBotLifecycleRoster(id, {
        on_roster: onRoster,
        updated_by: 'operator',
        reason: onRoster ? 'Add to roster' : 'Take off roster',
      });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchReconcileNow(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('reconcile_now');
    this.mutationError.set(null);
    try {
      await this.liveRuns.reconcileInstance(id);
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('reconcile', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  onGateRedeploy(): void {
    void this.router.navigate(['/broker/deploy'], { queryParams: this.redeployQueryParams() });
  }

  onGateOpenRunbook(slug: string): void {
    const route = resolveOperatorRunbookRoute(slug, this.instanceId());
    if (route === null) {
      this.mutationError.set(`No operator route is registered for runbook: ${slug}`);
      return;
    }
    void this.router.navigate(route.commands);
  }

  invokeBlockerMove(move: OperatorMove): void {
    switch (move.action.kind) {
      case 'navigate':
        void this.router.navigate([move.action.route], move.action.fragment ? { fragment: move.action.fragment } : {});
        break;
      case 'open_runbook':
        this.onGateOpenRunbook(move.action.slug);
        break;
      case 'retire_replace':
        this.openTerminalRetireReplaceConfirm();
        break;
      case 'remove':
        this.openRemoveBotConfirm();
        break;
      case 'confirm_in_form':
        this.openWhyForUnavailableMove();
        break;
      default: {
        const unreachable: never = move.action;
        this.mutationError.set(`Unsupported blocker move: ${String(unreachable)}`);
      }
    }
  }

  openAccountMonitor(): void {
    void this.router.navigate(['/broker/account-monitor'], {
      fragment: 'account-reconciliation-action',
    });
  }

  private openWhyForUnavailableMove(): void {
    this.mutationError.set('This blocker action is only available on the deploy form.');
  }

  openTypedHalt(): void {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.typedHaltInstanceId.set(id);
    this.typedHaltOpen.set(true);
  }

  closeTypedHalt(): void {
    this.typedHaltOpen.set(false);
    this.typedHaltInstanceId.set(null);
  }

  async confirmTypedHalt(): Promise<void> {
    const id = this.instanceId();
    if (!id || id !== this.typedHaltInstanceId() || this.busyAction()) return;
    this.busyAction.set('mark_poisoned');
    this.mutationError.set(null);
    this.typedHaltOpen.set(false);
    this.typedHaltInstanceId.set(null);
    try {
      await this.liveRuns.issueInstanceCommand(id, { verb: 'MARK_POISONED' });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('mark-poisoned', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  openRetireReplaceConfirm(): void {
    if (this.isActionDisabled('retire_replace')) return;
    this.retireReplaceConfirmOpen.set(true);
  }

  openTerminalRetireReplaceConfirm(): void {
    if (!this.instanceId() || this.busyAction()) return;
    this.retireReplaceConfirmOpen.set(true);
  }

  cancelRetireReplaceConfirm(): void {
    this.retireReplaceConfirmOpen.set(false);
  }

  async confirmRetireReplace(): Promise<void> {
    if (!this.retireReplaceConfirmOpen()) return;
    this.retireReplaceConfirmOpen.set(false);
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('retire_replace');
    this.mutationError.set(null);
    try {
      await this.liveRuns.retireAndReplace(id, {
        confirm_account_flat: true,
        replacement_requested: true,
        updated_by: 'operator',
        reason: 'Retire & Replace',
      });
      await this.refreshStatus(id);
      this.onGateRedeploy();
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  openRemoveBotConfirm(): void {
    if (!this.instanceId() || this.busyAction()) return;
    this.removeBotConfirmOpen.set(true);
  }

  cancelRemoveBotConfirm(): void {
    this.removeBotConfirmOpen.set(false);
  }

  async confirmRemoveBot(): Promise<void> {
    if (!this.removeBotConfirmOpen()) return;
    this.removeBotConfirmOpen.set(false);
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('remove');
    this.mutationError.set(null);
    try {
      await this.liveRuns.deleteBot(id, { mode: 'soft', deleted_by: 'operator' });
      void this.router.navigate(['/broker/bots']);
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private redeployQueryParams(): Record<string, string> {
    const s = this.status();
    if (!s) return {};
    return redeployQueryParamsForStatus(s);
  }

  private isActionDisabled(action: BotLifecycleActionId): boolean {
    if (this.status() === null || this.busyAction() !== null) return true;
    return !this.lifecycleAction(action)?.enabled;
  }

  private lifecycleAction(action: BotLifecycleActionId): BotLifecycleAction | null {
    const lifecycle = this.status()?.daily_lifecycle;
    if (!lifecycle) return null;
    if (lifecycle.primary_action?.id === action) return lifecycle.primary_action;
    return lifecycle.ambient_actions.find((candidate) => candidate.id === action) ?? null;
  }

  private startRequestWithRollCallOffer(
    request: HostRunnerStartRequest,
  ): HostRunnerStartRequest | null {
    const offerId = this.lifecycleAction('confirm_start')?.offer_id ?? null;
    if (!offerId) return null;
    return { ...request, roll_call_offer_id: offerId };
  }

  private async refreshStatus(id: string): Promise<void> {
    const seq = ++this.statusRequestSeq;
    try {
      const status = await this.liveRuns.getInstanceStatus(id);
      if (this.instanceId() !== id || seq !== this.statusRequestSeq) return;
      this.status.set(status);
      this.statusError.set(null);
      void this.refreshLifecycleTimeline(id, status, seq);
    } catch (err) {
      if (this.instanceId() !== id || seq !== this.statusRequestSeq) return;
      this.statusError.set(this.humanError(err));
      this.timelineRows.set([]);
      this.timelineProjectionAvailable.set(false);
      this.timelineCanonicalFallbackRequired.set(true);
    }
  }

  private async refreshLifecycleTimeline(
    id: string,
    status: LiveInstanceStatus,
    seq: number,
  ): Promise<void> {
    try {
      const timeline = await this.liveRuns.getLifecycleTimeline({
        account_id: this.timelineAccountId(status),
        strategy_instance_id: status.strategy_instance_id,
        run_id: this.timelineRunId(status),
        limit: 8,
      });
      if (this.instanceId() !== id || seq !== this.statusRequestSeq) return;
      this.timelineRows.set(timeline.rows);
      this.timelineProjectionAvailable.set(timeline.projection_available);
      this.timelineCanonicalFallbackRequired.set(timeline.canonical_fallback_required);
      this.timelineNotice.set(null);
    } catch (err) {
      if (this.instanceId() !== id || seq !== this.statusRequestSeq) return;
      this.timelineRows.set([]);
      this.timelineProjectionAvailable.set(false);
      this.timelineCanonicalFallbackRequired.set(true);
      this.timelineNotice.set(`Lifecycle projection unavailable: ${this.humanError(err)}`);
    }
  }

  private timelineAccountId(status: LiveInstanceStatus): string | null {
    return status.operator_surface.account_owner?.account_id ?? status.start_defaults?.account_id ?? null;
  }

  private timelineRunId(status: LiveInstanceStatus): string | null {
    return status.live_binding?.run_id
      ?? status.evidence_binding?.run_id
      ?? status.daily_lifecycle.active_run_id
      ?? status.daily_lifecycle.latest_run_id;
  }

  private async setIntent(action: 'resume' | 'stop', label: string): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set(action);
    this.mutationError.set(null);
    try {
      await this.liveRuns.setInstanceDesiredState(id, {
        action,
        reason: label,
        updated_by: 'operator',
      });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage(action, err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private operationErrorMessage(operation: OperationKind, err: unknown): string {
    const error = toOperationError(operation, err);
    return `${error.detail} ${error.remediation}`;
  }

  private humanError(err: unknown): string {
    if (err && typeof err === 'object' && 'error' in err && 'message' in err) {
      const httpErr = err as { error?: unknown; message?: unknown };
      const detail =
        httpErr.error && typeof httpErr.error === 'object'
          ? (httpErr.error as { detail?: unknown }).detail
          : null;
      if (typeof detail === 'string') return detail;
      if (detail && typeof detail === 'object') {
        const body = detail as {
          message?: unknown;
          reason_code?: unknown;
          disabled_reason_code?: unknown;
        };
        const message = typeof body.message === 'string' ? body.message : null;
        const reason =
          typeof body.reason_code === 'string'
            ? body.reason_code
            : typeof body.disabled_reason_code === 'string'
              ? body.disabled_reason_code
              : null;
        if (message && reason) return `${message} (${formatReceiptLabel(reason)})`;
        if (message) return message;
        if (reason) return formatReceiptLabel(reason);
      }
      if (typeof httpErr.message === 'string') return httpErr.message;
    }
    if (err instanceof Error && err.message) return err.message;
    return 'Could not load bot control data.';
  }

  private scheduleNextPoll(id: string, token: number): void {
    if (this.destroyed || this.instanceId() !== id || token !== this.pollToken) return;
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      void this.refreshStatus(id).finally(() => this.scheduleNextPoll(id, token));
    }, POLL_INTERVAL_MS);
  }

  private openSurfaceStream(id: string, token: number): void {
    if (this.destroyed || this.instanceId() !== id || token !== this.pollToken) return;
    this.closeSurfaceStream();
    this.surfaceStream = openBotSurfaceStream(id, {
      onSnapshot: (snapshot) => {
        if (this.destroyed || this.instanceId() !== id || token !== this.pollToken) return;
        if (!shouldAcceptSurfaceSnapshot(this.status(), snapshot)) return;
        const seq = ++this.statusRequestSeq;
        this.status.set(snapshot);
        this.statusError.set(null);
        void this.refreshLifecycleTimeline(id, snapshot, seq);
      },
      onMalformedSnapshot: (message) => {
        if (this.destroyed || this.instanceId() !== id || token !== this.pollToken) return;
        this.statusError.set(message);
      },
    });
  }

  private closeSurfaceStream(): void {
    this.surfaceStream?.close();
    this.surfaceStream = null;
  }

  private clearPollTimer(): void {
    if (this.pollTimer === null) return;
    clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }
}
