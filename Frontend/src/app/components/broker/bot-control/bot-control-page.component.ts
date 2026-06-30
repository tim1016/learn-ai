import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { Accordion, AccordionContent, AccordionHeader, AccordionPanel } from 'primeng/accordion';
import { HttpErrorResponse } from '@angular/common/http';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import type {
  FleetAccountSummary,
  LifecycleChartActionId,
  LifecycleChartNode,
  LifecycleChartReceipt,
  LifecycleProjectionEventRow,
  LiveInstanceStatus,
  OperatorNotice,
  OperatorSurfaceControlPlane,
  TraderPrimaryRemediation,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { ActiveBotSidebarNoticeService } from '../../../shell/active-bot-sidebar-notice.service';
import { ActivityTabComponent } from '../cockpit-v2/tabs/activity-tab.component';
import { AuditTabComponent } from '../cockpit-v2/tabs/audit-tab.component';
import { ConfigurationTabComponent } from '../cockpit-v2/tabs/configuration-tab.component';
import { StatusRiskTabComponent } from '../cockpit-v2/tabs/status-risk-tab.component';
import { TypedHaltConfirmComponent } from '../cockpit-v2/reused/typed-halt-confirm/typed-halt-confirm.component';
import type { InnerTab } from '../cockpit-v2/lib/instance-tab-state';
import { redeployQueryParamsForStatus } from '../cockpit-v2/lib/redeploy-query-params';
import { canStartHostProcess, startHostProcessFromCapability } from '../cockpit-v2/lib/start-host-process';
import { fmtTimestampNy } from '../format';
import { OverviewActionsComponent } from './overview-tab/overview-actions.component';
import { OverviewTabComponent } from './overview-tab/overview-tab.component';

const POLL_INTERVAL_MS = 4_000;
const TIMELINE_LIMIT = 5;
const POISONED_CONFIRM_MESSAGE =
  'Flagging this instance as POISONED is IRREVERSIBLE: the current run can never resume on its run_id. Recovery requires a fresh deployment (new run_id) after you reconcile the account.';
const TIMELINE_PROJECTION_UNAVAILABLE =
  'Projection unavailable; current snapshot remains file-backed.';

type BotControlTab = InnerTab;
type BotControlAction = 'resume' | 'pause' | 'flatten_and_pause' | 'stop' | 'mark_poisoned';

interface ControlPlaneBanner {
  readonly state: OperatorSurfaceControlPlane['state'];
  readonly shortLabel: 'attention needed' | 'last known';
  readonly demoted: boolean;
  readonly notice: string | null;
  readonly attemptText: string | null;
  readonly runbookSlug: string | null;
}

interface LifecycleTimelinePaneState {
  readonly rows: LifecycleProjectionEventRow[];
  readonly projectionAvailable: boolean;
  readonly canonicalFallbackRequired: boolean;
  readonly notice: string | null;
}

const EMPTY_TIMELINE_STATE: LifecycleTimelinePaneState = {
  rows: [],
  projectionAvailable: false,
  canonicalFallbackRequired: true,
  notice: null,
};

@Component({
  selector: 'app-bot-control-page',
  imports: [
    CommonModule,
    Accordion,
    AccordionPanel,
    AccordionHeader,
    AccordionContent,
    RouterLink,
    StatusRiskTabComponent,
    OverviewTabComponent,
    ActivityTabComponent,
    AuditTabComponent,
    ConfigurationTabComponent,
    TypedHaltConfirmComponent,
    OverviewActionsComponent,
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
  readonly selectedTab = signal<BotControlTab>('status');
  readonly selectedLifecycleNodeId = signal<string | null>(null);
  readonly statusError = signal<string | null>(null);
  readonly accountSummaryError = signal<string | null>(null);
  readonly mutationError = signal<string | null>(null);
  readonly busyAction = signal<string | null>(null);
  readonly typedHaltOpen = signal<boolean>(false);
  readonly poisonedConfirmMessage = POISONED_CONFIRM_MESSAGE;

  readonly errorMessage = computed<string | null>(
    () => this.mutationError() ?? this.statusError() ?? this.accountSummaryError(),
  );

  readonly brokerEvidenceNotice = computed<OperatorNotice | null>(
    () => this.accountSummary()?.notice ?? null,
  );

  readonly hasTopWarnings = computed<boolean>(
    () => this.brokerEvidenceNotice() !== null || this.controlPlaneBanner() !== null,
  );

  readonly hostRunnerNotice = computed(() => {
    const hostProcess = this.status()?.operator_surface.host_process ?? null;
    if (hostProcess?.state !== 'UNREACHABLE') return null;
    return {
      title: 'Host runner unreachable',
      message: hostProcess.notice ?? 'The host runner cannot be reached for this bot.',
      command: hostProcess.copyable_command,
    };
  });

  readonly controlPlaneBanner = computed<ControlPlaneBanner | null>(() => {
    const cp = this.status()?.operator_surface.control_plane ?? null;
    if (cp === null || cp.state === 'CONNECTED') return null;
    const demoted = cp.state !== 'RETRYING';
    return {
      state: cp.state,
      shortLabel: demoted ? 'last known' : 'attention needed',
      demoted,
      notice: cp.notice,
      attemptText: cp.state === 'RETRYING' && cp.attempt > 0
        ? `retrying · attempt ${cp.attempt}`
        : null,
      runbookSlug: cp.runbook_slug,
    };
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

  readonly timelineRows = computed(() => this.lifecycleTimeline().rows);
  readonly timelineProjectionAvailable = computed(() => this.lifecycleTimeline().projectionAvailable);
  readonly timelineCanonicalFallbackRequired = computed(() => this.lifecycleTimeline().canonicalFallbackRequired);
  readonly timelineNotice = computed(() => this.lifecycleTimeline().notice);

  readonly selectedTabLabel = computed<string>(() => this.tabLabel(this.selectedTab()));

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed()).subscribe((params) => {
      const id = params.get('id');
      const token = ++this.pollToken;
      this.clearPollTimer();
      this.instanceId.set(id);
      this.status.set(null);
      this.lifecycleTimeline.set(EMPTY_TIMELINE_STATE);
      this.selectedTab.set('status');
      this.selectedLifecycleNodeId.set(null);
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
              message: notice.message,
              command: notice.command,
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

  selectTab(tab: BotControlTab): void {
    this.selectedTab.set(tab);
  }

  selectLifecycleNode(node: LifecycleChartNode): void {
    this.selectedLifecycleNodeId.set(node.id);
    this.selectedTab.set(this.tabForLifecycleNode(node));
  }

  nodeTimestamp(node: LifecycleChartNode): string {
    return node.ts_ms_resolved ? fmtTimestampNy(node.ts_ms) : 'timestamp unresolved';
  }

  receiptTimestamp(receipt: LifecycleChartReceipt): string | null {
    if (receipt.ts_ms === null) return null;
    return receipt.ts_ms_resolved ? fmtTimestampNy(receipt.ts_ms) : 'timestamp unresolved';
  }

  trackNodeReceipt(index: number, receipt: LifecycleChartReceipt): string {
    return `${receipt.label}:${receipt.source ?? 'unknown'}:${index}`;
  }

  async dispatchResume(): Promise<void> {
    await this.setIntent('resume', 'Resume');
  }

  async dispatchStartProcess(): Promise<void> {
    const id = this.instanceId();
    const cap = this.status()?.operator_surface.host_process.start_capability;
    if (!id || this.busyAction() || !cap || !canStartHostProcess(cap)) return;
    this.busyAction.set('start_process');
    this.mutationError.set(null);
    try {
      await startHostProcessFromCapability(this.liveRuns, cap);
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.humanError(err));
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

  async dispatchFlattenAndPause(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('flatten_and_pause');
    this.mutationError.set(null);
    try {
      await this.liveRuns.flattenAndPause(id, {
        action: 'pause',
        reason: 'Flatten and pause',
        updated_by: 'operator',
      });
      await this.refreshStatus(id);
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
    window.open(this.runbookHref(slug), '_blank', 'noopener');
  }

  runbookHref(slug: string): string {
    return `/runbooks/${encodeURIComponent(slug)}`;
  }

  dispatchOverviewAction(action: LifecycleChartActionId): void {
    switch (action) {
      case 'start_process':
        void this.dispatchStartProcess();
        break;
      case 'resume':
        void this.dispatchResume();
        break;
      case 'pause':
        void this.dispatchPause();
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
      case 'redeploy':
        this.onGateRedeploy();
        break;
      default: {
        const unreachable: never = action;
        this.mutationError.set(`Unsupported lifecycle action: ${String(unreachable)}`);
      }
    }
  }

  dispatchTraderGuidanceAction(action: TraderPrimaryRemediation): void {
    switch (action.kind) {
      case 'invoke_capability':
        if (action.capability === 'resume') void this.dispatchResume();
        else void this.dispatchPause();
        break;
      case 'focus_action':
        this.selectTab(action.tab);
        break;
      case 'redeploy':
        this.onGateRedeploy();
        break;
      case 'open_runbook':
        this.onGateOpenRunbook(action.slug);
        break;
      case 'invoke_endpoint':
        if (action.endpoint === 'reconcile_instance') void this.dispatchReconcileNow();
        break;
      case 'none':
        break;
      default: {
        const unreachable: never = action;
        this.mutationError.set(`Unsupported trader guidance action: ${String(unreachable)}`);
      }
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
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private tabForLifecycleNode(node: LifecycleChartNode): BotControlTab {
    switch (node.id) {
      case 'deploy':
      case 'preflight':
      case 'activate':
        return 'configuration';
      case 'active':
      case 'submit_order':
      case 'broker_writer':
        return 'activity';
      case 'recovery':
        return 'audit';
      case 'account_safety':
      case 'reconcile':
      default:
        return 'status';
    }
  }

  private tabLabel(tab: BotControlTab): string {
    switch (tab) {
      case 'status':
        return 'Status & Risk';
      case 'activity':
        return 'Activity';
      case 'audit':
        return 'Audit';
      case 'configuration':
        return 'Configuration';
    }
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

  openTypedHalt(): void {
    if (this.isActionDisabled('mark_poisoned')) return;
    this.typedHaltOpen.set(true);
  }

  closeTypedHalt(): void {
    this.typedHaltOpen.set(false);
  }

  async confirmTypedHalt(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('mark_poisoned');
    this.mutationError.set(null);
    this.typedHaltOpen.set(false);
    try {
      await this.liveRuns.issueInstanceCommand(id, { verb: 'MARK_POISONED' });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  redeployQueryParams(): Record<string, string> {
    const s = this.status();
    if (!s) return {};
    return redeployQueryParamsForStatus(s);
  }

  isActionDisabled(action: BotControlAction): boolean {
    const status = this.status();
    if (status === null || this.busyAction() !== null) return true;
    return !status.operator_surface.actions[action].enabled;
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
      void this.refreshLifecycleTimeline(id, status, seq);
    } catch (err) {
      if (this.instanceId() !== id || seq !== this.statusRequestSeq) return;
      this.statusError.set(this.humanError(err));
    }
  }

  private async refreshLifecycleTimeline(
    id: string,
    status: LiveInstanceStatus,
    statusSeq: number,
  ): Promise<void> {
    const seq = ++this.timelineRequestSeq;
    try {
      const timeline = await this.liveRuns.getLifecycleTimeline({
        account_id: status.operator_surface.account_owner?.account_id ?? this.accountSummary()?.account_id ?? null,
        strategy_instance_id: status.strategy_instance_id,
        run_id: status.live_binding?.run_id ?? status.evidence_binding?.run_id ?? null,
        limit: TIMELINE_LIMIT,
      });
      if (this.instanceId() !== id || statusSeq !== this.statusRequestSeq || seq !== this.timelineRequestSeq) return;
      this.lifecycleTimeline.set({
        rows: timeline.rows,
        projectionAvailable: timeline.projection_available,
        canonicalFallbackRequired: timeline.canonical_fallback_required,
        notice: timeline.canonical_fallback_required ? TIMELINE_PROJECTION_UNAVAILABLE : null,
      });
    } catch (err) {
      if (this.instanceId() !== id || statusSeq !== this.statusRequestSeq || seq !== this.timelineRequestSeq) return;
      this.lifecycleTimeline.set({
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

  private async setIntent(
    action: 'resume' | 'pause' | 'stop',
    label: string,
  ): Promise<void> {
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
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private humanError(err: unknown): string {
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
