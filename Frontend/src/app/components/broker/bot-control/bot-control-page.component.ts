import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { TabsModule } from 'primeng/tabs';

import type {
  BotLifecycleAction,
  BotLifecycleActionId,
  BrokerSafetyVerdict,
  FleetAccountSummary,
  LifecycleChartNode,
  LifecycleProjectionEventRow,
  LiveInstanceStatus,
  MutationRungReceipt,
  OperatorSurfaceCurrentRisk,
  OperatorSurfaceSubmitReadiness,
  OperatorNotice,
  RiskPosture,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ActiveBotSidebarNoticeService } from '../../../shell/active-bot-sidebar-notice.service';
import type { ActiveBotSidebarNotice } from '../../../shell/active-bot-sidebar-notice.service';
import { ActivityTabComponent } from './tabs/activity-tab.component';
import { TypedHaltConfirmComponent } from './reused/typed-halt-confirm/typed-halt-confirm.component';
import { redeployQueryParamsForStatus } from './lib/redeploy-query-params';
import { resolveOperatorRunbookRoute } from './lib/operator-runbook-routes';
import { canStartHostProcess, startHostProcessFromCapability } from './lib/start-host-process';
import {
  renderTraderRemediation,
  type RenderedAction,
  type RendererDispatch,
} from './lib/suggested-action-renderer';
import {
  renderOperatorNoticeAction,
  type OperatorNoticeDispatch,
} from './lib/operator-notice-action-renderer';
import { toOperationError, type OperationKind } from '../operation-error';
import { operatorPillTone, type OperatorPillTone } from '../operator-severity';
import { OverviewActionsComponent } from './overview-tab/overview-actions.component';
import { OverviewTabComponent } from './overview-tab/overview-tab.component';
import { TraderGuidanceTimelineComponent } from './overview-tab/trader-guidance-timeline.component';
import { WorkbenchAuditPanelComponent } from './workbench-audit-panel.component';
import { BotControlSidePanelComponent } from './bot-control-side-panel.component';
import { boundRunIdForStatus } from './lib/bound-run-id';
import { OperatorNoticeComponent } from '../../operator-notice/operator-notice.component';

const POLL_INTERVAL_MS = 4_000;
const TIMELINE_LIMIT = 5;
const POISONED_CONFIRM_MESSAGE =
  'Flagging this instance as POISONED is IRREVERSIBLE: the current run can never resume on its run_id. Recovery requires a fresh deployment (new run_id) after you reconcile the account.';
const FLATTEN_CONFIRM_MESSAGE =
  'Flatten & pause sends a market-flattening request for any owned positions and then pauses the bot. Positions may be closed at the next available price. Confirm to proceed.';
const CRASH_RECOVERY_CONFIRM_MESSAGE =
  'Recording recovery evidence clears the crash-retired start gate and lets this bot run again. Only confirm if you have verified in IBKR that the broker account is FLAT with no open orders. This writes audited safety evidence.';
const RETIRE_REPLACE_CONFIRM_MESSAGE =
  'Retire & Replace permanently retires this bot instance, then opens replacement deploy with the current lineage. Confirm only after you have verified the broker account is flat with no open orders.';
const TIMELINE_PROJECTION_UNAVAILABLE =
  'Projection unavailable; current snapshot remains file-backed.';

type LegacyBotControlAction = 'flatten_and_pause' | 'mark_poisoned';
type WorkbenchTab = 'activity' | 'audit';
type PosturePillTone = 'ok' | 'attention' | 'warn' | 'neutral' | 'muted';

interface PosturePill {
  readonly label: string;
  readonly value: string;
  readonly tone: PosturePillTone;
}

interface ConnectionPill {
  readonly symbol: string | null;
  readonly state: string;
  readonly tone: OperatorPillTone;
}

interface LifecycleTimelinePaneState {
  readonly statusKey: string | null;
  readonly rows: LifecycleProjectionEventRow[];
  readonly projectionAvailable: boolean;
  readonly canonicalFallbackRequired: boolean;
  readonly notice: string | null;
}

interface LifecycleTimelineRequestContext {
  readonly statusKey: string;
  readonly params: {
    readonly account_id: string | null;
    readonly strategy_instance_id: string;
    readonly run_id: string | null;
    readonly limit: number;
  };
}

const EMPTY_TIMELINE_STATE: LifecycleTimelinePaneState = {
  statusKey: null,
  rows: [],
  projectionAvailable: false,
  canonicalFallbackRequired: true,
  notice: null,
};

@Component({
  selector: 'app-bot-control-page',
  imports: [
    TabsModule,
    AssetIdentityComponent,
    OverviewTabComponent,
    ActivityTabComponent,
    TypedHaltConfirmComponent,
    OverviewActionsComponent,
    TraderGuidanceTimelineComponent,
    WorkbenchAuditPanelComponent,
    BotControlSidePanelComponent,
    OperatorNoticeComponent,
  ],
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
  private timelineRequestSeq = 0;
  private destroyed = false;

  readonly instanceId = signal<string | null>(null);
  readonly status = signal<LiveInstanceStatus | null>(null);
  readonly accountSummary = signal<FleetAccountSummary | null>(null);
  readonly lifecycleTimeline = signal<LifecycleTimelinePaneState>(EMPTY_TIMELINE_STATE);
  readonly selectedLifecycleNodeId = signal<string | null>(null);
  readonly highlightedLifecycleNodeId = signal<string | null>(null);
  readonly statusError = signal<string | null>(null);
  readonly accountSummaryError = signal<string | null>(null);
  readonly mutationError = signal<string | null>(null);
  readonly mutationReceipt = signal<MutationRungReceipt | null>(null);
  readonly mutationReceiptWarnings = signal<readonly MutationRungReceipt[]>([]);
  readonly busyAction = signal<string | null>(null);
  readonly typedHaltOpen = signal<boolean>(false);
  private readonly typedHaltInstanceId = signal<string | null>(null);
  readonly flattenConfirmOpen = signal<boolean>(false);
  readonly crashRecoveryConfirmOpen = signal<boolean>(false);
  readonly retireReplaceConfirmOpen = signal<boolean>(false);
  readonly activeWorkbenchTab = signal<WorkbenchTab>('activity');
  readonly poisonedConfirmMessage = POISONED_CONFIRM_MESSAGE;
  readonly flattenConfirmMessage = FLATTEN_CONFIRM_MESSAGE;
  readonly crashRecoveryConfirmMessage = CRASH_RECOVERY_CONFIRM_MESSAGE;
  readonly retireReplaceConfirmMessage = RETIRE_REPLACE_CONFIRM_MESSAGE;

  readonly errorMessage = computed<string | null>(
    () => this.mutationError() ?? this.statusError() ?? this.accountSummaryError(),
  );

  readonly hostRunnerNotice = computed<ActiveBotSidebarNotice | null>(() => {
    const hostProcess = this.status()?.operator_surface.host_process ?? null;
    if (hostProcess?.state === 'WAITING_FOR_HOST') {
      const cap = hostProcess.start_capability;
      return {
        instanceId: this.instanceId() ?? '',
        kind: 'live-binding-invalid',
        summary: 'Live binding invalid.',
        message: hostProcess.notice ?? 'This bot has no active live binding. Bind again before trading.',
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

  readonly noticePlacement = computed(
    () => this.status()?.operator_surface.notice_placement ?? null,
  );
  readonly dominantNotice = computed<OperatorNotice | null>(
    () => this.noticePlacement()?.banner ?? null,
  );
  readonly foldedCriticalNotices = computed<readonly OperatorNotice[]>(
    () => this.noticePlacement()?.banner_folded ?? [],
  );
  readonly attentionNotices = computed<readonly OperatorNotice[]>(
    () => this.noticePlacement()?.attention ?? [],
  );
  readonly quietStatusNotices = computed<readonly OperatorNotice[]>(
    () => this.noticePlacement()?.quiet_status ?? [],
  );

  readonly posturePills = computed<PosturePill[]>(() => {
    const os = this.status()?.operator_surface;
    if (!os) return [];
    return [
      this.brokerProofPill(os.broker.safety_verdict),
      this.submitPill(os.submit_readiness),
      this.exposurePill(os.current_risk),
    ];
  });

  readonly connectionPill = computed<ConnectionPill | null>(() => {
    const status = this.status();
    if (!status) return null;
    const condition = status.operator_surface.broker.connection_condition;
    return {
      symbol: status.symbol,
      state: condition.title,
      tone: operatorPillTone(condition.severity),
    };
  });
  readonly accountFreezeGate = computed(() => {
    return (
      this.status()?.operator_surface.readiness_gates.find(
        (gate) =>
          gate.gate_result.status === 'freeze' &&
          gate.gate_result.gate_id.startsWith('account.'),
      )?.gate_result ?? null
    );
  });
  readonly accountFreezeReason = computed(() => {
    const reason = this.accountFreezeGate()?.operator_reason;
    return reason ? formatReceiptLabel(reason) : null;
  });

  readonly rightPaneNode = computed<LifecycleChartNode | null>(() => {
    const status = this.status();
    if (!status) return null;
    const selectedId = this.selectedLifecycleNodeId();
    if (selectedId) {
      const selected = this.findLifecycleNode(status, selectedId);
      if (selected) return selected;
    }
    const graph = status.lifecycle_chart.global_graph;
    return graph.nodes.find((node) => node.id === graph.primary_node_id) ?? null;
  });
  readonly traderGuidance = computed(() => this.status()?.operator_surface.trader_guidance ?? null);
  readonly renderedPrimaryRemediation = computed<RenderedAction | null>(() => {
    const remediation = this.traderGuidance()?.primary_remediation ?? null;
    return renderTraderRemediation(remediation, this.primaryRemediationDispatch);
  });
  readonly timelineRows = computed(() => this.lifecycleTimeline().rows);
  readonly timelineProjectionAvailable = computed(() => this.lifecycleTimeline().projectionAvailable);
  readonly timelineCanonicalFallbackRequired = computed(() => this.lifecycleTimeline().canonicalFallbackRequired);
  readonly timelineNotice = computed(() => this.lifecycleTimeline().notice);
  readonly lifecycleToolbarActions = computed<BotLifecycleAction[]>(() => {
    const lifecycle = this.status()?.daily_lifecycle;
    if (!lifecycle) return [];
    return [
      ...(lifecycle.primary_action ? [lifecycle.primary_action] : []),
      ...lifecycle.ambient_actions,
    ];
  });

  private readonly primaryRemediationDispatch: RendererDispatch = {
    invokeCapability: (capability) => {
      if (capability === 'resume') void this.dispatchResume();
      else void this.dispatchPause();
    },
    focus: (_tab, action) => {
      const targetNodeId = this.targetNodeForAction(action);
      if (targetNodeId) this.selectedLifecycleNodeId.set(targetNodeId);
    },
    redeploy: () => this.onGateRedeploy(),
    openRunbook: (slug) => this.onGateOpenRunbook(slug),
    invokeEndpoint: (endpoint) => {
      if (endpoint === 'reconcile_instance') void this.dispatchReconcileNow();
    },
  };

  openAccountMonitor(): void {
    void this.router.navigate(['/broker/account-monitor'], {
      fragment: 'account-reconciliation-action',
    });
  }

  private readonly runtimeNoticeDispatch: OperatorNoticeDispatch = {
    redeploy: () => this.onGateRedeploy(),
    openRunbook: (slug) => this.onGateOpenRunbook(slug),
    focusTarget: (target) => this.selectActionTargetNode(target),
    renewControlPlaneLease: () => { void this.dispatchRenewControlPlaneLease(); },
  };

  // Receipt focus targets actuate the named mutation; anything else falls
  // back to chart-node selection like a runtime notice.
  private readonly mutationReceiptDispatch: OperatorNoticeDispatch = {
    redeploy: () => this.onGateRedeploy(),
    openRunbook: (slug) => this.onGateOpenRunbook(slug),
    focusTarget: (target) => {
      if (target === 'crash_recovery_override') void this.dispatchCrashRecoveryOverride();
      else if (target === 'start_process') void this.dispatchStartProcess();
      else if (target === 'reconcile_now') void this.dispatchReconcileNow();
      else this.selectActionTargetNode(target);
    },
    renewControlPlaneLease: () => { void this.dispatchRenewControlPlaneLease(); },
  };

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed()).subscribe((params) => {
      const id = params.get('id');
      const token = ++this.pollToken;
      this.clearPollTimer();
      this.instanceId.set(id);
      this.status.set(null);
      this.lifecycleTimeline.set(EMPTY_TIMELINE_STATE);
      this.selectedLifecycleNodeId.set(null);
      this.highlightedLifecycleNodeId.set(null);
      this.typedHaltOpen.set(false);
      this.typedHaltInstanceId.set(null);
      this.flattenConfirmOpen.set(false);
      this.crashRecoveryConfirmOpen.set(false);
      this.retireReplaceConfirmOpen.set(false);
      this.mutationReceipt.set(null);
      this.mutationReceiptWarnings.set([]);
      this.activeWorkbenchTab.set('activity');
      if (id) {
        void this.refresh(id).finally(() => this.scheduleNextPoll(id, token));
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
      this.activeBotSidebarNotice.clearForInstance(this.instanceId());
    });
  }

  selectLifecycleNode(node: LifecycleChartNode): void {
    this.selectedLifecycleNodeId.set(node.id);
  }

  async dispatchResume(): Promise<void> {
    await this.setIntent('resume', 'Resume');
  }

  async dispatchStartProcess(): Promise<void> {
    const id = this.instanceId();
    const cap = this.status()?.operator_surface.host_process.start_capability;
    if (!id || this.busyAction() || !cap || !canStartHostProcess(cap)) return;
    this.busyAction.set('start_process');
    this.clearMutationOutcome();
    try {
      const response = await startHostProcessFromCapability(this.liveRuns, cap);
      this.applyMutationReceipt(response);
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
    this.clearMutationOutcome();
    try {
      const response = await this.liveRuns.recordCrashRecoveryOverride(id, {
        confirm_account_flat: true,
        approved_by: 'operator',
      });
      this.applyMutationReceipt(response);
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('recovery-override', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchPause(): Promise<void> {
    await this.setIntent('pause', 'Pause');
  }

  async dispatchStop(): Promise<void> {
    await this.setIntent('stop', 'Stop');
  }

  async dispatchEndDayNow(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('end_day_now');
    this.clearMutationOutcome();
    try {
      const response = await this.liveRuns.endDayNow(id, { force: false });
      this.applyMutationReceipt(response);
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
    this.clearMutationOutcome();
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

  async dispatchFlattenAndPause(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('flatten_and_pause');
    this.clearMutationOutcome();
    try {
      const response = await this.liveRuns.flattenAndPause(id, {
        action: 'pause',
        reason: 'Flatten and pause',
        updated_by: 'operator',
      });
      this.applyMutationReceipt(response);
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('flatten', err));
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

  runbookHref(slug: string): string | null {
    const route = resolveOperatorRunbookRoute(slug, this.instanceId());
    if (route === null) return null;
    return route.commands.map((part) => encodeURI(part)).join('/');
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

  selectActionTargetNode(target: string): void {
    const status = this.status();
    if (!status) return;
    const nodeId = this.findLifecycleNode(status, target)
      ? target
      : this.targetNodeForAction(target);
    if (nodeId) this.selectedLifecycleNodeId.set(nodeId);
  }

  setHighlightedLifecycleNode(nodeId: string | null): void {
    this.highlightedLifecycleNodeId.set(nodeId);
  }

  invokePrimaryRemediation(action: RenderedAction): void {
    action.invoke();
  }

  handleRuntimeNoticeAction(notice: OperatorNotice): void {
    const action = renderOperatorNoticeAction(notice, this.runtimeNoticeDispatch);
    if (action === null) {
      this.mutationError.set('Notice action is not executable because required action evidence is missing.');
      return;
    }
    action.invoke();
  }

  handleMutationReceiptAction(receipt: MutationRungReceipt): void {
    renderOperatorNoticeAction(receipt, this.mutationReceiptDispatch)?.invoke();
  }

  setActiveWorkbenchTab(value: string | number | undefined): void {
    if (value === 'activity' || value === 'audit') {
      this.activeWorkbenchTab.set(value);
    }
  }

  async dispatchReconcileNow(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('reconcile_now');
    this.clearMutationOutcome();
    try {
      const response = await this.liveRuns.reconcileInstance(id);
      this.applyMutationReceipt(response);
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('reconcile', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  async dispatchRenewControlPlaneLease(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('renew_control_plane_lease');
    this.mutationError.set(null);
    try {
      await this.liveRuns.renewControlPlaneLease();
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('renew-lease', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private brokerProofPill(verdict: BrokerSafetyVerdict): PosturePill {
    const tone: PosturePillTone =
      verdict === 'PAPER_ONLY' ? 'ok' : verdict === 'UNSAFE' ? 'attention' : 'muted';
    return { label: 'Broker proof', value: formatReceiptLabel(verdict), tone };
  }

  private submitPill(readiness: OperatorSurfaceSubmitReadiness): PosturePill {
    // submit_readiness.label is backend-authored trader prose — rendered as-is.
    return { label: 'Submit', value: readiness.label, tone: readiness.can_submit ? 'ok' : 'warn' };
  }

  private exposurePill(risk: OperatorSurfaceCurrentRisk): PosturePill {
    const posture: RiskPosture = risk.posture;
    const tone: PosturePillTone =
      posture === 'UNKNOWN' ? 'muted' : posture === 'MIXED' ? 'warn' : 'neutral';
    return { label: 'Exposure', value: formatReceiptLabel(posture), tone };
  }

  private targetNodeForAction(actionId: string): string | null {
    const status = this.status();
    if (!status) return null;
    return status.lifecycle_chart.actions.find((action) => action.id === actionId)?.target_node_id ?? null;
  }

  private findLifecycleNode(status: LiveInstanceStatus, nodeId: string): LifecycleChartNode | null {
    const chart = status.lifecycle_chart;
    const globalNode = chart.global_graph.nodes.find((node) => node.id === nodeId);
    if (globalNode) return globalNode;
    for (const graph of Object.values(chart.subgraphs)) {
      const subgraphNode = graph.nodes.find((node) => node.id === nodeId);
      if (subgraphNode) return subgraphNode;
    }
    return null;
  }

  openFlattenConfirm(): void {
    if (this.isLegacyActionDisabled('flatten_and_pause')) return;
    this.flattenConfirmOpen.set(true);
  }

  cancelFlattenConfirm(): void {
    this.flattenConfirmOpen.set(false);
  }

  async confirmFlatten(): Promise<void> {
    if (!this.flattenConfirmOpen()) return;
    this.flattenConfirmOpen.set(false);
    // Re-check eligibility at confirm time: a poll may have disabled the action
    // (lost live binding, positions already flat) while the dialog was open, so
    // never fire the market-flattening mutation from a stale confirmation.
    if (this.isLegacyActionDisabled('flatten_and_pause')) return;
    await this.dispatchFlattenAndPause();
  }

  openTypedHalt(): void {
    if (this.isLegacyActionDisabled('mark_poisoned')) return;
    const id = this.instanceId();
    if (!id) return;
    this.typedHaltInstanceId.set(id);
    this.typedHaltOpen.set(true);
  }

  openRetireReplaceConfirm(): void {
    if (this.isActionDisabled('retire_replace')) return;
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
    this.clearMutationOutcome();
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

  closeTypedHalt(): void {
    this.typedHaltOpen.set(false);
    this.typedHaltInstanceId.set(null);
  }

  async confirmTypedHalt(): Promise<void> {
    const id = this.instanceId();
    if (!id || id !== this.typedHaltInstanceId() || this.busyAction()) return;
    this.busyAction.set('mark_poisoned');
    this.clearMutationOutcome();
    this.typedHaltOpen.set(false);
    this.typedHaltInstanceId.set(null);
    try {
      const response = await this.liveRuns.issueInstanceCommand(id, { verb: 'MARK_POISONED' });
      this.applyMutationReceipt(response);
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.operationErrorMessage('mark-poisoned', err));
    } finally {
      this.busyAction.set(null);
    }
  }

  redeployQueryParams(): Record<string, string> {
    const s = this.status();
    if (!s) return {};
    return redeployQueryParamsForStatus(s);
  }

  isActionDisabled(action: BotLifecycleActionId): boolean {
    const status = this.status();
    if (status === null || this.busyAction() !== null) return true;
    return !this.lifecycleAction(action)?.enabled;
  }

  isLegacyActionDisabled(action: LegacyBotControlAction): boolean {
    const status = this.status();
    if (status === null || this.busyAction() !== null) return true;
    return !status.operator_surface.actions[action].enabled;
  }

  private lifecycleAction(action: BotLifecycleActionId): BotLifecycleAction | null {
    const lifecycle = this.status()?.daily_lifecycle;
    if (!lifecycle) return null;
    if (lifecycle.primary_action?.id === action) return lifecycle.primary_action;
    return lifecycle.ambient_actions.find((candidate) => candidate.id === action) ?? null;
  }

  private async refresh(id: string): Promise<void> {
    await Promise.allSettled([this.refreshStatus(id), this.refreshAccountSummary()]);
  }

  private async refreshStatus(id: string): Promise<void> {
    const seq = ++this.statusRequestSeq;
    try {
      const status = await this.liveRuns.getInstanceStatus(id);
      if (this.instanceId() !== id || seq !== this.statusRequestSeq) return;
      this.status.set(status);
      this.statusError.set(null);
      const timelineContext = this.lifecycleTimelineContext(status);
      if (this.lifecycleTimeline().statusKey !== timelineContext.statusKey) {
        this.lifecycleTimeline.set({
          ...EMPTY_TIMELINE_STATE,
          statusKey: timelineContext.statusKey,
        });
      }
      void this.refreshLifecycleTimeline(id, timelineContext, seq);
    } catch (err) {
      if (this.instanceId() !== id || seq !== this.statusRequestSeq) return;
      this.statusError.set(this.humanError(err));
    }
  }

  private async refreshLifecycleTimeline(
    id: string,
    context: LifecycleTimelineRequestContext,
    statusSeq: number,
  ): Promise<void> {
    const seq = ++this.timelineRequestSeq;
    try {
      const timeline = await this.liveRuns.getLifecycleTimeline(context.params);
      if (this.instanceId() !== id || statusSeq !== this.statusRequestSeq || seq !== this.timelineRequestSeq) return;
      this.lifecycleTimeline.set({
        statusKey: context.statusKey,
        rows: timeline.rows,
        projectionAvailable: timeline.projection_available,
        canonicalFallbackRequired: timeline.canonical_fallback_required,
        notice: timeline.canonical_fallback_required ? TIMELINE_PROJECTION_UNAVAILABLE : null,
      });
    } catch (err) {
      if (this.instanceId() !== id || statusSeq !== this.statusRequestSeq || seq !== this.timelineRequestSeq) return;
      this.lifecycleTimeline.set({
        statusKey: context.statusKey,
        rows: [],
        projectionAvailable: false,
        canonicalFallbackRequired: true,
        notice: this.timelineFallbackNotice(err),
      });
    }
  }

  private async refreshAccountSummary(): Promise<void> {
    try {
      this.accountSummary.set(await this.liveRuns.getAccountSummary());
      this.accountSummaryError.set(null);
    } catch (err) {
      this.accountSummaryError.set(this.humanError(err));
    }
  }

  private lifecycleTimelineContext(status: LiveInstanceStatus): LifecycleTimelineRequestContext {
    const accountId = status.operator_surface.account_owner?.account_id ?? null;
    const runId = boundRunIdForStatus(status);
    return {
      statusKey: [
        status.strategy_instance_id,
        accountId ?? '',
        runId ?? '',
      ].join(':'),
      params: {
        account_id: accountId,
        strategy_instance_id: status.strategy_instance_id,
        run_id: runId,
        limit: TIMELINE_LIMIT,
      },
    };
  }

  private async setIntent(
    action: 'resume' | 'pause' | 'stop',
    label: string,
  ): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set(action);
    this.clearMutationOutcome();
    try {
      const response = await this.liveRuns.setInstanceDesiredState(id, {
        action,
        reason: label,
        updated_by: 'operator',
      });
      this.applyMutationReceipt(response);
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

  private clearMutationOutcome(): void {
    this.mutationError.set(null);
    this.mutationReceipt.set(null);
    this.mutationReceiptWarnings.set([]);
  }

  private applyMutationReceipt(response: {
    rung_receipt?: MutationRungReceipt | null;
    rung_receipt_warnings?: MutationRungReceipt[];
  }): void {
    this.mutationReceipt.set(response.rung_receipt ?? null);
    this.mutationReceiptWarnings.set(response.rung_receipt_warnings ?? []);
  }

  private humanError(err: unknown): string {
    if (err && typeof err === 'object' && 'error' in err && 'message' in err) {
      const httpErr = err as { error?: unknown; message?: unknown };
      const detail = httpErr.error && typeof httpErr.error === 'object'
        ? (httpErr.error as { detail?: unknown }).detail
        : null;
      if (typeof detail === 'string') return detail;
      if (detail && typeof detail === 'object') {
        const body = detail as { message?: unknown; reason_code?: unknown; disabled_reason_code?: unknown };
        const message = typeof body.message === 'string' ? body.message : null;
        const reason = typeof body.reason_code === 'string'
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

  private timelineFallbackNotice(err: unknown): string {
    if (err instanceof HttpErrorResponse && err.status === 503) return TIMELINE_PROJECTION_UNAVAILABLE;
    return 'Lifecycle projection timeline could not be loaded; current snapshot remains file-backed.';
  }

  private scheduleNextPoll(id: string, token: number): void {
    if (this.destroyed || this.instanceId() !== id || token !== this.pollToken) return;
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      void this.refresh(id).finally(() => this.scheduleNextPoll(id, token));
    }, POLL_INTERVAL_MS);
  }

  private clearPollTimer(): void {
    if (this.pollTimer === null) return;
    clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }
}
