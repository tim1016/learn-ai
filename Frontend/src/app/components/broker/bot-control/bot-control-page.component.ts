import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Router } from '@angular/router';
import { timer } from 'rxjs';

import type {
  BotLifecycleAction,
  BotLifecycleActionId,
  BotRollCallOffer,
  GateResult,
  LiveInstanceStatus,
  OperatorSurfaceConfirmations,
  TraderPrimaryRemediation,
} from '../../../api/live-instances.types';
import type { OperatorConfirmationCopy, OperatorMove } from '../../../api/operator-blocker.types';
import type { HostRunnerStartRequest } from '../../../api/live-runs.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerService } from '../../../services/broker.service';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { ActiveBotSidebarNoticeService } from '../../../shell/active-bot-sidebar-notice.service';
import type { ActiveBotSidebarNotice } from '../../../shell/active-bot-sidebar-notice.service';
import { VerdictCardComponent } from './verdict-card/verdict-card.component';
import { BotControlSidePanelComponent } from './bot-control-side-panel.component';
import { OverviewTabComponent } from './overview-tab/overview-tab.component';
import { TraderGuidancePaneComponent } from './overview-tab/trader-guidance-pane.component';
import { TraderViewComponent } from './trader-view/trader-view.component';
import { TypedHaltConfirmComponent } from './reused/typed-halt-confirm/typed-halt-confirm.component';
import type { BotEventStreamCommand } from './reused/bot-event-stream/bot-event-stream-action';
import { redeployQueryParamsForStatus } from './lib/redeploy-query-params';
import { resolveOperatorRunbookRoute } from './lib/operator-runbook-routes';
import { canStartHostProcess } from './lib/start-host-process';
import {
  presentTraderRemediation,
  type PresentedAction,
} from './lib/suggested-action-renderer';
import { toOperationError, type OperationKind } from '../operation-error';
import { BotSurfaceStore } from './bot-surface-store.service';

const MISSING_CONFIRMATION_COPY_ERROR =
  'Backend confirmation copy is unavailable; refusing unsafe action.';

type StartupAutomationState =
  | { readonly phase: 'idle' }
  | { readonly phase: 'running'; readonly label: string; readonly detail: string }
  | { readonly phase: 'ready'; readonly label: string; readonly detail: string }
  | { readonly phase: 'blocked'; readonly label: string; readonly detail: string };

interface StartReadyCapability {
  readonly run_id: string;
  readonly request: HostRunnerStartRequest;
}

@Component({
  selector: 'app-bot-control-page',
  imports: [
    VerdictCardComponent,
    OverviewTabComponent,
    BotControlSidePanelComponent,
    TraderGuidancePaneComponent,
    TraderViewComponent,
    TypedHaltConfirmComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-control-page.component.html',
  styleUrl: './bot-control-page.component.scss',
})
export class BotControlPageComponent {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly broker = inject(BrokerService);
  private readonly surface = inject(BotSurfaceStore);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly activeBotSidebarNotice = inject(ActiveBotSidebarNoticeService);

  readonly instanceId = this.surface.instanceId;
  readonly status = this.surface.status;
  readonly statusError = this.surface.errorMessage;
  readonly readOnly = this.surface.readOnly;
  readonly pendingAttemptId = this.surface.pendingAttemptId;
  readonly mutationError = signal<string | null>(null);
  readonly busyAction = signal<string | null>(null);
  readonly activeLens = signal<'trader' | 'operations'>('trader');
  readonly startupAutomation = signal<StartupAutomationState>({ phase: 'idle' });
  readonly accountStartGate = computed<GateResult | null>(() =>
    this.status()?.operator_surface.host_process.start_capability.gate_results.find(
      (gate) => gate.gate_id === 'account.observation_lease',
    ) ?? null,
  );
  readonly startupAutomationDisplay = computed<StartupAutomationState>(() => {
    const gate = this.accountStartGate();
    if (gate?.status === 'block') {
      return {
        phase: 'blocked',
        label: 'Account verification blocked',
        detail: gate.operator_reason,
      };
    }
    return this.startupAutomation();
  });
  readonly accountReconcileAvailable = computed(
    () =>
      this.accountStartGate()?.status === 'block' &&
      this.boundAccountId() !== null,
  );
  readonly typedHaltOpen = signal<boolean>(false);
  private readonly typedHaltInstanceId = signal<string | null>(null);
  readonly crashRecoveryConfirmOpen = signal<boolean>(false);
  readonly retireReplaceConfirmOpen = signal<boolean>(false);
  readonly removeBotConfirmOpen = signal<boolean>(false);
  private readonly retireReplaceMoveConfirmation = signal<OperatorConfirmationCopy | null>(null);
  private readonly removeBotMoveConfirmation = signal<OperatorConfirmationCopy | null>(null);
  private readonly automaticRollCallKey = signal<string | null>(null);
  private readonly confirmations = computed<OperatorSurfaceConfirmations | null>(
    () => this.status()?.operator_surface.confirmations ?? null,
  );
  readonly poisonedConfirmation = computed(
    () => this.confirmations()?.mark_poisoned ?? null,
  );
  readonly crashRecoveryConfirmation = computed(
    () => this.confirmations()?.crash_recovery_override ?? null,
  );
  readonly retireReplaceConfirmation = computed(
    () =>
      this.retireReplaceMoveConfirmation() ??
      this.confirmations()?.retire_replace ??
      null,
  );
  readonly removeBotConfirmation = computed(
    () => this.removeBotMoveConfirmation() ?? this.confirmations()?.remove_bot ?? null,
  );
  readonly errorMessage = computed<string | null>(
    () => this.mutationError() ?? this.statusError(),
  );
  private readonly displayClock = toSignal(timer(0, 1_000), { initialValue: 0 });
  readonly sourceFreshness = computed(() => {
    this.displayClock();
    const status = this.status();
    if (!status) return [];
    const receivedAtMs = this.surface.snapshotReceivedAtMs();
    const elapsed = receivedAtMs === null ? 0 : Math.max(0, Date.now() - receivedAtMs);
    const rows: { label: string; state: string; age_ms: number | null }[] = [
      { label: 'Surface snapshot', state: 'SNAPSHOT', age_ms: elapsed },
    ];
    const freshness = status.operator_surface.runtime_freshness;
    if (freshness) {
      rows.push(
        { label: 'Command loop', state: freshness.command_loop.state, age_ms: addAge(freshness.command_loop.age_ms, elapsed) },
        { label: 'Broker runtime', state: freshness.broker.state, age_ms: addAge(freshness.broker.age_ms, elapsed) },
        { label: 'Bar loop', state: freshness.bar_loop.state, age_ms: addAge(freshness.bar_loop.age_ms, elapsed) },
        { label: 'Runtime control plane', state: freshness.control_plane.state, age_ms: addAge(freshness.control_plane.age_ms, elapsed) },
      );
    }
    const controlPlane = status.operator_surface.control_plane;
    if (controlPlane) {
      rows.push({
        label: 'Daemon observation',
        state: controlPlane.state,
        age_ms: sourceAgeAtDisplay(status.fetched_at_ms, controlPlane.last_success_ms, elapsed),
      });
    }
    const brokerObservation = status.operator_surface.broker_observation_consistency;
    if (brokerObservation) {
      rows.push({
        label: 'Broker comparison',
        state: brokerObservation.verdict,
        age_ms: sourceAgeAtDisplay(
          status.fetched_at_ms,
          brokerObservation.compared_at_ms,
          elapsed,
        ),
      });
    }
    const reconciliation = status.operator_surface.reconciliation;
    if (reconciliation) {
      rows.push({
        label: 'Reconciliation',
        state: reconciliation.state,
        age_ms: sourceAgeAtDisplay(
          status.fetched_at_ms,
          reconciliation.broker_observed_at_ms ?? reconciliation.last_reconcile_ms,
          elapsed,
        ),
      });
    }
    return rows;
  });

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
  readonly renderedPrimaryRemediation = computed<PresentedAction | null>(() =>
    presentTraderRemediation(this.traderGuidance()?.primary_remediation ?? null),
  );

  readonly tradingModeLabel = computed(() => {
    const verdict = this.status()?.operator_surface.broker.safety_verdict;
    if (verdict === 'PAPER_ONLY') return 'Paper';
    if (verdict === 'UNSAFE') return 'Live';
    return 'Mode unknown';
  });
  readonly gracefulStopAvailable = computed(() => {
    const status = this.status();
    const lifecycle = status?.daily_lifecycle.display_status;
    return (
      status?.desired_state?.state === 'RUNNING' &&
      (lifecycle === 'On duty' ||
        (lifecycle === 'Sick bay' && status.operator_surface.host_process.state === 'UNREACHABLE'))
    );
  });
  readonly gracefulStopLivenessUnproven = computed(
    () => this.status()?.operator_surface.host_process.state === 'UNREACHABLE',
  );

  constructor() {
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
    effect(() => {
      const status = this.status();
      if (!status || this.readOnly() || this.busyAction() !== null) return;
      if (!this.needsRollCallOffer(status)) return;
      const runId = status.operator_surface.host_process.start_capability.run_id;
      const key = `${status.strategy_instance_id}:${runId ?? 'no-run'}`;
      if (this.automaticRollCallKey() === key) return;
      this.automaticRollCallKey.set(key);
      void this.prepareRollCallOfferOnly();
    });
    this.destroyRef.onDestroy(() => {
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
    const remediation = this.traderGuidance()?.primary_remediation ?? null;
    if (remediation !== null) this.invokeTraderRemediation(remediation);
  }

  invokeTraderRemediation(remediation: TraderPrimaryRemediation): void {
    switch (remediation.kind) {
      case 'none':
        break;
      case 'invoke_capability':
        if (remediation.capability === 'resume') void this.dispatchResumeIntent();
        else void this.dispatchEndDayNow();
        break;
      case 'focus_action':
        if (remediation.action === 'mark_poisoned') this.openTypedHalt();
        else if (remediation.action === 'stop') void this.dispatchStop();
        else this.openAccounts();
        break;
      case 'redeploy':
        this.onGateRedeploy();
        break;
      case 'open_runbook':
        this.onGateOpenRunbook(remediation.slug);
        break;
      case 'invoke_endpoint':
        if (remediation.endpoint === 'reconcile_instance') void this.dispatchReconcileNow();
        break;
    }
  }

  invokeStreamAction(action: BotEventStreamCommand): void {
    switch (action) {
      case 'start_process':
        void this.dispatchStartProcess();
        break;
      case 'resume':
        void this.dispatchResumeIntent();
        break;
      case 'pause':
        void this.dispatchEndDayNow();
        break;
      case 'flatten_and_pause':
        void this.dispatchFlattenAndPause();
        break;
      case 'stop':
        void this.dispatchStop();
        break;
      case 'mark_poisoned':
        this.openTypedHalt();
        break;
      case 'fresh_run':
        this.onGateRedeploy();
        break;
      default: {
        const unreachable: never = action;
        this.mutationError.set(`Unsupported stream action: ${String(unreachable)}`);
      }
    }
  }

  onSettingsRequested(): void {
    this.onGateRedeploy();
  }

  async dispatchStartProcess(): Promise<void> {
    const id = this.instanceId();
    const cap = this.status()?.operator_surface.host_process.start_capability;
    if (!id || this.mutationsDisabled() || !cap || !canStartHostProcess(cap)) return;
    const request = this.startRequestWithRollCallOffer(cap.request);
    if (!request) {
      await this.prepareAndStartWithRollCall();
      return;
    }
    await this.startHostProcess(cap.run_id, request);
  }

  // Opening the attestation dialog is the only entry point. Recording recovery
  // evidence clears a safety gate, so the operator must explicitly confirm the
  // account is flat before we post confirm_account_flat — never on a bare click.
  dispatchCrashRecoveryOverride(): void {
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    if (!this.requireConfirmation(this.crashRecoveryConfirmation())) return;
    this.crashRecoveryConfirmOpen.set(true);
  }

  cancelCrashRecoveryConfirm(): void {
    this.crashRecoveryConfirmOpen.set(false);
  }

  async confirmCrashRecoveryOverride(): Promise<void> {
    if (!this.crashRecoveryConfirmOpen()) return;
    this.crashRecoveryConfirmOpen.set(false);
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    this.busyAction.set('crash_recovery_override');
    this.mutationError.set(null);
    try {
      await this.liveRuns.recordCrashRecoveryOverride(id, {
        confirm_account_flat: true,
        approved_by: 'operator',
      });
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
    if (!id || this.mutationsDisabled()) return;
    this.busyAction.set('end_day_now');
    this.mutationError.set(null);
    try {
      const response = await this.liveRuns.endDayNow(id, { force: false });
      this.surface.establishPending(response);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('stop', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchFlattenAndPause(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    this.busyAction.set('flatten_and_pause');
    this.mutationError.set(null);
    try {
      const response = await this.liveRuns.flattenAndPause(id, {
        action: 'pause',
        reason: 'Flatten and pause',
        updated_by: 'operator',
      });
      this.surface.establishPending(response);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('flatten', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchRosterChange(onRoster: boolean): Promise<void> {
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    const action: BotLifecycleActionId = onRoster ? 'add_to_roster' : 'take_off_roster';
    this.busyAction.set(action);
    this.mutationError.set(null);
    try {
      await this.liveRuns.setBotLifecycleRoster(id, {
        on_roster: onRoster,
        updated_by: 'operator',
        reason: onRoster ? 'Add to roster' : 'Take off roster',
      });
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchReconcileNow(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    this.busyAction.set('reconcile_now');
    this.mutationError.set(null);
    try {
      await this.liveRuns.reconcileInstance(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('reconcile', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchAccountReconcileNow(): Promise<void> {
    const accountId = this.boundAccountId();
    if (!accountId || this.mutationsDisabled()) return;
    this.busyAction.set('account_reconcile_now');
    this.mutationError.set(null);
    try {
      await this.broker.reconcileAccount(accountId);
      this.startupAutomation.set({
        phase: 'ready',
        label: 'Account check requested',
        detail: 'Waiting for the current account proof to appear in the cockpit.',
      });
    } catch (err) {
      this.mutationError.set(this.humanError(err));
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
    void this.router.navigate(route.commands, route.fragment ? { fragment: route.fragment } : {});
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
        this.openTerminalRetireReplaceConfirm(move.confirmation ?? null);
        break;
      case 'remove':
        this.openRemoveBotConfirm(move.confirmation ?? null);
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

  openAccounts(): void {
    void this.router.navigate(['/broker/accounts']);
  }

  private openWhyForUnavailableMove(): void {
    this.mutationError.set('This blocker action is only available on the deploy form.');
  }

  openTypedHalt(): void {
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    if (!this.requireConfirmation(this.poisonedConfirmation())) return;
    this.typedHaltInstanceId.set(id);
    this.typedHaltOpen.set(true);
  }

  closeTypedHalt(): void {
    this.typedHaltOpen.set(false);
    this.typedHaltInstanceId.set(null);
  }

  async confirmTypedHalt(): Promise<void> {
    const id = this.instanceId();
    if (!id || id !== this.typedHaltInstanceId() || this.mutationsDisabled()) return;
    this.busyAction.set('mark_poisoned');
    this.mutationError.set(null);
    this.typedHaltOpen.set(false);
    this.typedHaltInstanceId.set(null);
    try {
      await this.liveRuns.issueInstanceCommand(id, { verb: 'MARK_POISONED' });
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('mark-poisoned', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  openRetireReplaceConfirm(): void {
    if (this.isActionDisabled('retire_replace')) return;
    if (!this.requireConfirmation(this.retireReplaceConfirmation())) return;
    this.retireReplaceMoveConfirmation.set(null);
    this.retireReplaceConfirmOpen.set(true);
  }

  openTerminalRetireReplaceConfirm(confirmation: OperatorConfirmationCopy | null = null): void {
    if (!this.instanceId() || this.mutationsDisabled()) return;
    if (!this.requireConfirmation(confirmation ?? this.confirmations()?.retire_replace ?? null)) return;
    this.retireReplaceMoveConfirmation.set(confirmation);
    this.retireReplaceConfirmOpen.set(true);
  }

  cancelRetireReplaceConfirm(): void {
    this.retireReplaceConfirmOpen.set(false);
    this.retireReplaceMoveConfirmation.set(null);
  }

  async confirmRetireReplace(): Promise<void> {
    if (!this.retireReplaceConfirmOpen()) return;
    this.retireReplaceConfirmOpen.set(false);
    this.retireReplaceMoveConfirmation.set(null);
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    this.busyAction.set('retire_replace');
    this.mutationError.set(null);
    try {
      await this.liveRuns.retireAndReplace(id, {
        confirm_account_flat: true,
        replacement_requested: true,
        updated_by: 'operator',
        reason: 'Retire & Replace',
      });
      this.onGateRedeploy();
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  openRemoveBotConfirm(confirmation: OperatorConfirmationCopy | null = null): void {
    if (!this.instanceId() || this.mutationsDisabled()) return;
    if (!this.requireConfirmation(confirmation ?? this.confirmations()?.remove_bot ?? null)) return;
    this.removeBotMoveConfirmation.set(confirmation);
    this.removeBotConfirmOpen.set(true);
  }

  cancelRemoveBotConfirm(): void {
    this.removeBotConfirmOpen.set(false);
    this.removeBotMoveConfirmation.set(null);
  }

  async confirmRemoveBot(): Promise<void> {
    if (!this.removeBotConfirmOpen()) return;
    this.removeBotConfirmOpen.set(false);
    this.removeBotMoveConfirmation.set(null);
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
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
    if (this.status() === null || this.mutationsDisabled()) return true;
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

  private needsRollCallOffer(status: LiveInstanceStatus): boolean {
    const lifecycle = status.daily_lifecycle;
    const capability = status.operator_surface.host_process.start_capability;
    return (
      lifecycle.on_roster &&
      lifecycle.phase === 'OFF_DUTY' &&
      lifecycle.primary_action === null &&
      canStartHostProcess(capability)
    );
  }

  private async prepareRollCallOfferOnly(): Promise<void> {
    await this.prepareRollCallOffer({
      busyAction: 'startup_automation',
      offerFound: () => {
        this.startupAutomation.set({
          phase: 'ready',
          label: 'Start offer ready',
          detail: 'Roll call issued a fresh offer; waiting for the cockpit snapshot to show Start.',
        });
      },
    });
  }

  private async prepareAndStartWithRollCall(): Promise<void> {
    await this.prepareRollCallOffer({
      busyAction: 'startup_automation_start',
      offerFound: async (cap, offer) => {
        this.startupAutomation.set({
          phase: 'running',
          label: 'Starting bot',
          detail: 'Roll call issued a fresh offer; starting the bot with that offer.',
        });
        await this.startHostProcess(cap.run_id, {
          ...cap.request,
          roll_call_offer_id: offer.offer_id,
        });
      },
    });
  }

  private async prepareRollCallOffer(options: {
    busyAction: string;
    offerFound: (
      cap: StartReadyCapability,
      offer: BotRollCallOffer,
    ) => void | Promise<void>;
  }): Promise<void> {
    const status = this.status();
    const id = this.instanceId();
    const cap = status?.operator_surface.host_process.start_capability;
    if (
      !id ||
      !status ||
      this.mutationsDisabled() ||
      !cap ||
      !canStartHostProcess(cap) ||
      !cap.run_id ||
      !cap.request
    ) {
      return;
    }
    const readyCap: StartReadyCapability = {
      run_id: cap.run_id,
      request: cap.request,
    };
    this.busyAction.set(options.busyAction);
    this.mutationError.set(null);
    this.startupAutomation.set({
      phase: 'running',
      label: 'Verifying account',
      detail: this.accountStartGate()?.operator_reason ?? 'Checking current account proof before roll call.',
    });
    try {
      const response = await this.liveRuns.runRollCall();
      const offer = this.rollCallOfferForCurrentBot(response.offers, id, readyCap.run_id);
      if (!offer) {
        this.startupAutomation.set({
          phase: 'blocked',
          label: 'Roll call finished',
          detail: 'This bot did not receive a current start offer. Check the lifecycle blockers below.',
        });
        return;
      }
      await options.offerFound(readyCap, offer);
    } catch (err) {
      this.startupAutomation.set({
        phase: 'blocked',
        label: 'Startup automation stopped',
        detail: this.humanError(err),
      });
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private rollCallOfferForCurrentBot(
    offers: readonly BotRollCallOffer[],
    instanceId: string,
    runId: string,
  ): BotRollCallOffer | null {
    return offers.find(
      (offer) => offer.strategy_instance_id === instanceId && offer.run_id === runId,
    ) ?? null;
  }

  private async startHostProcess(runId: string, request: HostRunnerStartRequest): Promise<void> {
    this.busyAction.set('start_process');
    this.mutationError.set(null);
    this.startupAutomation.set({
      phase: 'running',
      label: 'Verifying account',
      detail: this.accountStartGate()?.operator_reason ?? 'Checking current account proof before start.',
    });
    try {
      const response = await this.liveRuns.startHostRunner(runId, request);
      this.surface.establishPending(response);
      this.startupAutomation.set({
        phase: 'ready',
        label: 'Startup request sent',
        detail: 'The host runner accepted the start request; waiting for live runtime proof.',
      });
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('start', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private async setIntent(action: 'resume' | 'stop', label: string): Promise<void> {
    const id = this.instanceId();
    if (!id || this.mutationsDisabled()) return;
    this.busyAction.set(action);
    this.mutationError.set(null);
    try {
      const response = await this.liveRuns.setInstanceDesiredState(id, {
        action,
        reason: label,
        updated_by: 'operator',
      });
      this.surface.establishPending(response);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage(action, err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private operationErrorMessage(operation: OperationKind, err: unknown): string {
    const error = toOperationError(operation, err);
    this.surface.establishPending(error);
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

  private boundAccountId(): string | null {
    const accountId =
      this.status()?.operator_surface.account_clerk?.account_id ??
      this.status()?.start_defaults?.account_id ??
      null;
    return accountId?.trim() || null;
  }

  private mutationsDisabled(): boolean {
    return this.busyAction() !== null || this.readOnly();
  }

  private requireConfirmation(confirmation: OperatorConfirmationCopy | null): boolean {
    if (confirmation) return true;
    this.mutationError.set(MISSING_CONFIRMATION_COPY_ERROR);
    return false;
  }
}

function addAge(ageAtSnapshotMs: number | null, elapsedMs: number): number | null {
  return ageAtSnapshotMs === null ? null : ageAtSnapshotMs + elapsedMs;
}

function sourceAgeAtDisplay(
  snapshotAtMs: number,
  sourceAtMs: number | null,
  localElapsedMs: number,
): number | null {
  return sourceAtMs === null
    ? null
    : Math.max(0, snapshotAtMs - sourceAtMs) + localElapsedMs;
}
