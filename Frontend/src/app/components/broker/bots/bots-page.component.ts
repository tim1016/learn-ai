import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { TableModule } from 'primeng/table';
import { TabsModule } from 'primeng/tabs';
import { TagModule } from 'primeng/tag';

import type { AccountTriageResponse } from '../../../api/account-reconciliation.types';
import type { DeployPreflightResponse } from '../../../api/operator-blocker.types';
import type {
  BotCatalogRow,
  BotCatalogTone,
  BotCatalogTradingMode,
  BotLifecycleCondition,
  BotAttendanceCell,
  BotEveningReport,
  BotLifecycleDisplayStatus,
  BotRollCallSummary,
} from '../../../api/live-instances.types';
import type { HostRunnerStartRequest } from '../../../api/live-runs.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { AccountFreezeBannerComponent } from '../account-freeze-banner/account-freeze-banner.component';
import {
  CohortLaunchDialogComponent,
  type CohortLaunchCandidate,
  type CohortLaunchPreflightCandidate,
} from '../cohort-launch-dialog/cohort-launch-dialog.component';
import { CohortLaunchMonitorComponent } from '../cohort-launch-monitor/cohort-launch-monitor.component';
import { fmtInteger, fmtSignedCurrency, fmtTimestampLocal } from '../format';
import { lifecycleConditionCureTarget } from '../lib/condition-cure-actions';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

type AttentionFilter = 'all' | 'needs-attention' | 'healthy';
type BotModeTab = BotCatalogTradingMode;
type LifecycleFilter = 'all' | BotLifecycleDisplayStatus;
type BotLaunchProgressPhase = 'idle' | 'preparing' | 'running' | 'blocked' | 'complete';
type BotLaunchRowStatus = 'queued' | 'starting' | 'accepted' | 'blocked';
type TagSeverity = 'success' | 'warn' | 'danger' | 'secondary';

const EMPTY_ROLL_CALL_SUMMARY: BotRollCallSummary = {
  ready: 0,
  off_roster: 0,
  sick_bay: 0,
  on_duty: 0,
  off_duty: 0,
  retired: 0,
  generated_at_ms: null,
  session_date: null,
  effective_stop_ms: null,
};

const EMPTY_LAUNCH_PROGRESS: BotLaunchProgress = {
  phase: 'idle',
  title: '',
  detail: '',
  activeBotId: null,
  rows: [],
};

const BOT_TONE_SEVERITY: Record<BotCatalogTone, TagSeverity> = {
  positive: 'success',
  warning: 'warn',
  danger: 'danger',
  neutral: 'secondary',
};

interface BotTableRow {
  id: string;
  name: string;
  latestRunId: string | null;
  needsAttention: boolean;
  tradingMode: BotCatalogTradingMode;
  symbolsLabel: string;
  displayStatus: BotLifecycleDisplayStatus;
  statusReason: string | null;
  sickBayCondition: BotLifecycleCondition | null;
  presenceLabel: string;
  attentionBadge: BotCatalogRow['daily_lifecycle']['attention_badge'];
  attentionSeverity: TagSeverity;
  exposure: string;
  openPositions: number | null;
  totalPnl: number | null;
  errorCount: number;
  lastRunSortMs: number;
  lastRunAtMs: number | null;
  lastRunLabel: string;
  startRequest: HostRunnerStartRequest | null;
  startOfferId: string | null;
  startOfferExpiresAtMs: number | null;
  attendance: readonly BotAttendanceCell[];
  searchText: string;
}

interface BotLaunchRowProgress {
  botId: string;
  botName: string;
  status: BotLaunchRowStatus;
  detail: string;
  reasonCode: string | null;
}

interface BotLaunchProgress {
  phase: BotLaunchProgressPhase;
  title: string;
  detail: string;
  activeBotId: string | null;
  rows: readonly BotLaunchRowProgress[];
}

@Component({
  selector: 'app-bots-page',
  imports: [
    CommonModule,
    ButtonModule,
    InputTextModule,
    TableModule,
    TabsModule,
    TagModule,
    AccountFreezeBannerComponent,
    CohortLaunchDialogComponent,
    CohortLaunchMonitorComponent,
    ReceiptLabelPipe,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bots-page.component.html',
  styleUrl: './bots-page.component.scss',
})
export class BotsPageComponent {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly broker = inject(BrokerService);
  private readonly health = inject(BrokerHealthService);
  private readonly router = inject(Router);

  readonly bots = signal<BotCatalogRow[]>([]);
  readonly accountTriage = signal<AccountTriageResponse | null>(null);
  readonly rollCall = signal<BotRollCallSummary>(EMPTY_ROLL_CALL_SUMMARY);
  readonly eveningReport = signal<BotEveningReport | null>(null);
  readonly accountTriageError = signal<string | null>(null);
  readonly isLoading = signal<boolean>(true);
  readonly isRunningRollCall = signal<boolean>(false);
  readonly isStartingReady = signal<boolean>(false);
  readonly errorMessage = signal<string | null>(null);
  readonly startAllErrorMessage = signal<string | null>(null);
  readonly conditionActionBotId = signal<string | null>(null);
  readonly conditionActionError = signal<{ botId: string; message: string } | null>(null);
  readonly launchProgress = signal<BotLaunchProgress>(EMPTY_LAUNCH_PROGRESS);
  readonly searchQuery = signal<string>('');
  readonly attentionFilter = signal<AttentionFilter>('all');
  readonly lifecycleFilter = signal<LifecycleFilter>('all');
  readonly activeModeTab = signal<BotModeTab>('paper');
  readonly selectedBotIds = signal<ReadonlySet<string>>(new Set<string>());
  readonly cohortConfirmationOpen = signal<boolean>(false);
  readonly cohortPreflightLoading = signal<boolean>(false);
  readonly cohortPreflightError = signal<string | null>(null);
  readonly cohortPreflight = signal<readonly CohortLaunchPreflightCandidate[]>([]);
  readonly cohortMonitorReloadVersion = signal(0);
  readonly deleteConfirmationOpen = signal<boolean>(false);
  readonly isDeleting = signal<boolean>(false);
  readonly deleteErrorMessage = signal<string | null>(null);

  readonly visibleBots = computed<BotTableRow[]>(() => {
    const query = normalize(this.searchQuery());
    const attentionFilter = this.attentionFilter();
    const lifecycleFilter = this.lifecycleFilter();

    return this.bots()
      .map(toTableRow)
      .filter((row) => {
        if (query && !row.searchText.includes(query)) return false;
        if (attentionFilter === 'needs-attention' && !row.needsAttention) return false;
        if (attentionFilter === 'healthy' && row.needsAttention) return false;
        if (lifecycleFilter !== 'all' && row.displayStatus !== lifecycleFilter) return false;
        return true;
      })
      .sort(compareRowsForTriage);
  });

  readonly liveBots = computed<BotTableRow[]>(() =>
    this.visibleBots().filter((bot) => bot.tradingMode === 'live'),
  );

  readonly paperBots = computed<BotTableRow[]>(() =>
    this.visibleBots().filter((bot) => bot.tradingMode === 'paper'),
  );

  readonly unknownModeBots = computed<BotTableRow[]>(() =>
    this.visibleBots().filter((bot) => bot.tradingMode === 'unknown'),
  );

  readonly activeRows = computed<BotTableRow[]>(() => {
    switch (this.activeModeTab()) {
      case 'live':
        return this.liveBots();
      case 'paper':
        return this.paperBots();
      case 'unknown':
        return this.unknownModeBots();
    }
  });

  readonly activeTabCount = computed(() => this.activeRows().length);
  readonly connectedAccountId = computed(() => this.health.health()?.account_id ?? null);
  readonly readyRows = computed<BotTableRow[]>(() =>
    this.visibleBots().filter((row) => {
      return (
        row.displayStatus === 'Ready' &&
        row.latestRunId !== null &&
        row.startRequest !== null &&
        row.startOfferId !== null
      );
    }),
  );
  readonly rollCallSummaryLine = computed(() => {
    const summary = this.rollCall();
    return [
      `${summary.ready} ready`,
      `${summary.on_duty} on duty`,
      `${summary.sick_bay} in sick bay`,
      `${summary.off_roster} off roster`,
      `${summary.retired} retired`,
    ].join(' · ');
  });
  readonly eveningReportLine = computed(() => this.eveningReport()?.summary ?? null);
  readonly accountFreezeBanner = computed(() => this.accountTriage()?.freeze_banner ?? null);
  readonly activeTabSummary = computed(() =>
    this.isLoading()
      ? `Loading ${this.activeModeTab()} bots...`
      : `${this.activeTabCount()} bots in ${this.activeModeTab()} mode`,
  );

  readonly selectedRows = computed<BotTableRow[]>(() => {
    const selected = this.selectedBotIds();
    return this.visibleBots().filter((row) => selected.has(row.id));
  });

  readonly selectedNamesLabel = computed(() =>
    this.selectedRows().map((row) => row.name).join(', '),
  );

  readonly selectedCount = computed(() => this.selectedBotIds().size);

  readonly allActiveRowsSelected = computed(() => {
    const rows = this.activeRows();
    return rows.length > 0 && rows.every((row) => this.selectedBotIds().has(row.id));
  });

  readonly someActiveRowsSelected = computed(() => {
    const rows = this.activeRows();
    return rows.some((row) => this.selectedBotIds().has(row.id));
  });

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.isLoading.set(true);
    this.errorMessage.set(null);
    try {
      const catalog = await this.liveRuns.getBotCatalog();
      this.bots.set(catalog.bots);
      this.rollCall.set(catalog.roll_call);
      this.eveningReport.set(catalog.evening_report);
      this.retainKnownSelections(catalog.bots);
      void this.refreshAccountTriage();
    } catch (err) {
      this.errorMessage.set(this.humanError(err));
    } finally {
      this.isLoading.set(false);
    }
  }

  async refreshAccountTriage(): Promise<void> {
    this.accountTriageError.set(null);
    if (this.health.health() === null) {
      await this.health.refresh();
    }
    const accountId = this.health.health()?.account_id;
    if (!accountId) {
      this.accountTriage.set(null);
      return;
    }
    try {
      this.accountTriage.set(await this.broker.accountTriage(accountId));
    } catch (err) {
      this.accountTriage.set(null);
      this.accountTriageError.set(this.humanError(err));
    }
  }

  async runRollCall(): Promise<void> {
    if (this.isRunningRollCall()) return;
    this.isRunningRollCall.set(true);
    this.startAllErrorMessage.set(null);
    this.errorMessage.set(null);
    try {
      const response = await this.liveRuns.runRollCall();
      this.rollCall.set(response.summary);
      await this.refresh();
    } catch (err) {
      this.errorMessage.set(this.humanError(err));
    } finally {
      this.isRunningRollCall.set(false);
    }
  }

  async startReadyBots(): Promise<void> {
    const rows = this.readyRows();
    if (rows.length === 0 || this.isStartingReady()) return;
    this.isStartingReady.set(true);
    this.startAllErrorMessage.set(null);
    this.errorMessage.set(null);
    this.launchProgress.set({
      phase: 'preparing',
      title: 'Preparing ready bots',
      detail: 'Refreshing account proof before launch.',
      activeBotId: null,
      rows: rows.map((row) => ({
        botId: row.id,
        botName: row.name,
        status: 'queued',
        detail: 'Queued after preflight.',
        reasonCode: null,
      })),
    });
    try {
      await this.refreshAccountTriage();
      if (this.accountFreezeBanner()) {
        this.blockLaunchProgress('Account sick bay is gating new starts.');
        return;
      }

      this.updateLaunchProgress({
        phase: 'preparing',
        title: 'Running roll call',
        detail: 'Refreshing start offers and daemon state before authorizing the cohort.',
      });
      const rollCall = await this.liveRuns.runRollCall();
      this.rollCall.set(rollCall.summary);
      await this.refresh();

      const refreshedRows = this.readyRows();
      if (refreshedRows.length === 0) {
        this.launchProgress.set({
          phase: 'complete',
          title: 'No ready bots after roll call',
          detail: 'Roll call completed, but no bot has a current start offer.',
          activeBotId: null,
          rows: [],
        });
        return;
      }

      const accountId = this.accountId();
      if (!accountId) {
        this.blockLaunchProgress('No paper broker account is connected for cohort authorization.');
        return;
      }

      this.launchProgress.set({
        phase: 'running',
        title: 'Starting authorized cohort',
        detail: `${refreshedRows.length} bots are starting under one server-authorized receipt.`,
        activeBotId: null,
        rows: refreshedRows.map((row) => ({
          botId: row.id,
          botName: row.name,
          status: 'starting',
          detail: 'Start request is in flight.',
          reasonCode: null,
        })),
      });
      const cohort = await this.liveRuns.launchCohort(accountId, {
        member_strategy_instance_ids: refreshedRows.map((row) => row.id),
        ...(refreshedRows.length === 3 ? { launch_profile: 'paper_three_bot_stagger_v2' as const } : {}),
      });
      this.cohortMonitorReloadVersion.update((version) => version + 1);
      await this.refresh();
      const liveStatusById = new Map(
        this.bots().map((bot) => [bot.strategy_instance_id, bot.daily_lifecycle.display_status]),
      );
      const outcomesById = new Map(
        cohort.outcomes.map((outcome) => [outcome.strategy_instance_id, outcome]),
      );
      if (cohort.outcomes_state !== 'recorded') {
        const outcomesArePending = cohort.outcomes_state === 'pending';
        this.launchProgress.update((current) => ({
          ...current,
          phase: outcomesArePending ? 'running' : 'blocked',
          title: outcomesArePending ? 'Cohort start pending' : 'Cohort outcome receipt unreadable',
          detail: outcomesArePending
            ? `Cohort receipt ${cohort.cohort_id} is durable; wait for the server to record member outcomes.`
            : cohort.outcomes_error
              ?? `Cohort receipt ${cohort.cohort_id} could not be read. Refresh before retrying any start.`,
          rows: refreshedRows.map((row) => ({
            botId: row.id,
            botName: row.name,
            status: outcomesArePending ? 'starting' : 'blocked',
            detail: outcomesArePending
              ? 'Cohort authorization is durable; the server has not yet recorded this member outcome.'
              : 'The server could not read the cohort outcome receipt. Refresh before retrying.',
            reasonCode: null,
          })),
        }));
        return;
      }
      const accepted = refreshedRows.every(
        (row) => outcomesById.get(row.id)?.state === 'accepted',
      );
      this.launchProgress.update((current) => ({
        ...current,
        phase: accepted ? 'complete' : 'blocked',
        title: accepted ? 'Cohort start accepted' : 'Cohort start needs attention',
        detail: `Cohort receipt ${cohort.cohort_id} records server-derived outcomes.`,
        rows: refreshedRows.map((row) => {
          const outcome = outcomesById.get(row.id);
          return outcome?.state === 'accepted'
            ? {
              botId: row.id,
              botName: row.name,
              status: 'accepted',
              detail: `Start accepted; live status is ${liveStatusById.get(row.id) ?? 'unavailable'}.`,
              reasonCode: outcome.reason,
            }
            : {
              botId: row.id,
              botName: row.name,
              status: 'blocked',
              detail: outcome
                ? 'The server did not accept this cohort member.'
                : 'The server did not record a cohort outcome for this bot.',
              reasonCode: outcome?.reason ?? null,
            };
        }),
      }));
    } catch (err) {
      const message = this.humanError(err);
      this.startAllErrorMessage.set(message);
      this.blockLaunchProgress(message);
      await this.refresh();
    } finally {
      this.isStartingReady.set(false);
    }
  }

  async requestCohortStart(): Promise<void> {
    const rows = this.readyRows();
    if (rows.length === 0 || this.isStartingReady() || this.cohortConfirmationOpen()) return;
    this.cohortConfirmationOpen.set(true);
    this.cohortPreflightError.set(null);
    this.cohortPreflight.set([]);
    const accountId = this.accountId();
    if (accountId === null) {
      this.cohortPreflightError.set('No paper broker account is connected for cohort preflight.');
      return;
    }
    const candidates = rows.map(toCohortLaunchCandidate);
    this.cohortPreflightLoading.set(true);
    try {
      const preflight = await Promise.all(
        candidates.map(async (candidate): Promise<CohortLaunchPreflightCandidate> => {
          try {
            const response = await this.liveRuns.deployPreflight({
              strategyKey: candidate.strategyKey,
              accountId,
              instanceId: candidate.strategyInstanceId,
            });
            return preflightCandidate(candidate, response);
          } catch (error) {
            return { candidate, blockers: [], error: this.humanError(error) };
          }
        }),
      );
      this.cohortPreflight.set(preflight);
    } finally {
      this.cohortPreflightLoading.set(false);
    }
  }

  cancelCohortStart(): void {
    this.cohortConfirmationOpen.set(false);
  }

  confirmCohortStart(): void {
    if (this.cohortPreflightLoading() || this.cohortPreflightError() !== null || this.cohortPreflight().some((row) => row.error !== null || row.blockers.length > 0)) {
      return;
    }
    this.cohortConfirmationOpen.set(false);
    void this.startReadyBots();
  }

  setSearchQuery(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) {
      this.searchQuery.set(target.value);
    }
  }

  setAttentionFilter(value: AttentionFilter): void {
    this.attentionFilter.set(value);
  }

  setLifecycleFilter(value: LifecycleFilter): void {
    this.lifecycleFilter.set(value);
  }

  setActiveModeTab(value: string | number | undefined): void {
    if (value === 'live' || value === 'paper' || value === 'unknown') {
      this.activeModeTab.set(value);
    }
  }

  clearFilters(): void {
    this.searchQuery.set('');
    this.attentionFilter.set('all');
    this.lifecycleFilter.set('all');
  }

  openAccounts(event?: Event): void {
    event?.stopPropagation();
    void this.router.navigate(['/broker/accounts']);
  }

  async runConditionCure(row: BotTableRow, event: Event): Promise<void> {
    event.stopPropagation();
    if (this.conditionActionBotId()) return;
    const target = row.sickBayCondition
      ? lifecycleConditionCureTarget(row.sickBayCondition)
      : 'accountMonitor';
    if (
      row.sickBayCondition &&
      target === 'retireReplace'
    ) {
      void this.openBot(row.id);
      return;
    }
    if (target === 'reconcile') {
      await this.reconcileAccountFromRow(row);
      return;
    }
    this.openAccounts();
  }

  isSelected(id: string): boolean {
    return this.selectedBotIds().has(id);
  }

  setBotSelection(id: string, event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) {
      this.toggleBotSelection(id, target.checked);
    }
  }

  setActiveTabSelection(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    const ids = this.activeRows().map((row) => row.id);
    this.selectedBotIds.update((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (target.checked) {
          next.add(id);
        } else {
          next.delete(id);
        }
      }
      return next;
    });
    this.closeDeleteConfirmation();
  }

  toggleBotSelection(id: string, selected = !this.isSelected(id)): void {
    this.selectedBotIds.update((current) => {
      const next = new Set(current);
      if (selected) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
    this.closeDeleteConfirmation();
  }

  clearSelection(): void {
    this.selectedBotIds.set(new Set<string>());
    this.closeDeleteConfirmation();
  }

  requestDeleteSelected(): void {
    if (this.selectedCount() === 0 || this.isDeleting()) return;
    this.deleteErrorMessage.set(null);
    this.deleteConfirmationOpen.set(true);
  }

  cancelDeleteSelected(): void {
    this.closeDeleteConfirmation();
  }

  async confirmDeleteSelected(): Promise<void> {
    const ids = [...this.selectedBotIds()];
    if (ids.length === 0 || this.isDeleting()) return;
    this.isDeleting.set(true);
    this.deleteErrorMessage.set(null);
    try {
      const results = await Promise.allSettled(
        ids.map((id) =>
          this.liveRuns.deleteBot(id, {
            mode: 'soft',
            deleted_by: 'operator',
            reason: 'Deleted from Bots page',
          }),
        ),
      );
      const firstFailure = results.find((result) => result.status === 'rejected');
      if (firstFailure) {
        if (results.some((result) => result.status === 'fulfilled')) {
          await this.refresh();
        }
        this.deleteErrorMessage.set(this.humanError(firstFailure.reason));
        return;
      }
      this.clearSelection();
      await this.refresh();
    } catch (err) {
      this.deleteErrorMessage.set(this.humanError(err));
    } finally {
      this.isDeleting.set(false);
    }
  }

  async openBot(id: string): Promise<void> {
    await this.router.navigate(['/broker/bots', id]);
  }

  formatMoney(value: number | null): string {
    return fmtSignedCurrency(value);
  }

  formatCount(value: number | null): string {
    return fmtInteger(value);
  }

  formatTimestamp(value: number | null): string {
    return fmtTimestampLocal(value);
  }

  attendanceClass(cell: BotAttendanceCell): string {
    return `attendance-dot is-${cell.status}`;
  }

  attendanceTitle(cell: BotAttendanceCell): string {
    return `${cell.session_date} · ${cell.label}`;
  }

  readonly trackByBotId = (_index: number, row: BotTableRow): string => row.id;
  readonly trackByAttendance = (_index: number, cell: BotAttendanceCell): string =>
    `${cell.session_date}:${cell.status}`;

  private closeDeleteConfirmation(): void {
    this.deleteConfirmationOpen.set(false);
    this.deleteErrorMessage.set(null);
  }

  private retainKnownSelections(bots: BotCatalogRow[]): void {
    const knownIds = new Set(bots.map((bot) => bot.strategy_instance_id));
    this.selectedBotIds.update((current) => {
      const next = new Set([...current].filter((id) => knownIds.has(id)));
      return next.size === current.size ? current : next;
    });
  }

  private humanError(err: unknown): string {
    if (err instanceof Error && err.message) return err.message;
    return 'Could not load bots.';
  }

  private accountId(): string | null {
    return this.connectedAccountId();
  }

  private async reconcileAccountFromRow(row: BotTableRow): Promise<void> {
    const accountId = this.accountId();
    if (!accountId) {
      this.conditionActionError.set({
        botId: row.id,
        message: 'No broker account is connected for account reconciliation.',
      });
      return;
    }
    this.conditionActionBotId.set(row.id);
    this.conditionActionError.set(null);
    try {
      await this.broker.reconcileAccount(accountId);
      await this.refreshAccountTriage();
      await this.refresh();
    } catch (err) {
      this.conditionActionError.set({ botId: row.id, message: this.humanError(err) });
    } finally {
      this.conditionActionBotId.set(null);
    }
  }

  private updateLaunchProgress(update: Partial<BotLaunchProgress>): void {
    this.launchProgress.update((current) => ({ ...current, ...update }));
  }

  private blockLaunchProgress(detail: string): void {
    this.launchProgress.update((current) => ({
      ...current,
      phase: 'blocked',
      title: current.title || 'Launch blocked',
      detail,
      activeBotId: null,
      rows: current.rows.map((row) =>
        row.status === 'starting'
          ? { ...row, status: 'blocked', detail }
          : row,
      ),
    }));
  }

  private progressRowsForCanary(
    rows: readonly BotTableRow[],
    canary: BotTableRow,
    status: BotLaunchRowStatus,
    detail: string,
  ): BotLaunchRowProgress[] {
    return rows.map((row) => {
      if (row.id === canary.id) {
        return {
          botId: row.id,
          botName: row.name,
          status,
          detail,
          reasonCode: null,
        };
      }
      return {
        botId: row.id,
        botName: row.name,
        status: 'queued',
        detail: 'Waiting for the canary result before another start.',
        reasonCode: null,
      };
    });
  }

  private progressAfterCanaryStart(
    queuedRows: readonly BotTableRow[],
    canary: BotTableRow,
  ): BotLaunchProgress {
    const refreshed = this.visibleBots().find((row) => row.id === canary.id);
    const status = refreshed?.displayStatus ?? canary.displayStatus;
    if (status === 'Sick bay') {
      return {
        phase: 'blocked',
        title: 'Canary moved to sick bay',
        detail: refreshed?.statusReason ?? `${canary.name} accepted the start but reported a blocker.`,
        activeBotId: canary.id,
        rows: this.progressRowsForCanary(
          queuedRows,
          canary,
          'blocked',
          refreshed?.statusReason ?? 'Start accepted, then a blocker appeared.',
        ),
      };
    }
    return {
      phase: 'complete',
      title: 'Canary start accepted',
      detail: `${canary.name} accepted the start. Refresh before starting the next ready bot.`,
      activeBotId: canary.id,
      rows: this.progressRowsForCanary(
        queuedRows,
        canary,
        'accepted',
        status === 'On duty' ? 'Bot is on duty.' : 'Start accepted; waiting for runtime proof.',
      ),
    };
  }
}

function normalize(value: string): string {
  return value.trim().toLowerCase();
}

function toCohortLaunchCandidate(row: BotTableRow): CohortLaunchCandidate {
  return {
    strategyInstanceId: row.id,
    name: row.name,
    strategyKey: row.startRequest?.strategy ?? '',
  };
}

function preflightCandidate(
  candidate: CohortLaunchCandidate,
  response: DeployPreflightResponse,
): CohortLaunchPreflightCandidate {
  const blockers = response.blockers.filter((blocker) => blocker.condition.severity === 'blocking');
  return {
    candidate,
    blockers,
    error: response.ready || blockers.length > 0
      ? null
      : 'The cohort preflight did not return a launch-ready verdict.',
  };
}

function toTableRow(bot: BotCatalogRow): BotTableRow {
  const symbolsLabel = bot.symbols.length > 0 ? bot.symbols.join(', ') : '—';
  const sickBayCondition = bot.daily_lifecycle.display_status === 'Sick bay'
    ? bot.daily_lifecycle.conditions?.[0] ?? null
    : null;

  return {
    id: bot.strategy_instance_id,
    name: bot.name,
    latestRunId: bot.daily_lifecycle.latest_run_id,
    needsAttention: bot.needs_attention,
    tradingMode: bot.trading_mode,
    symbolsLabel,
    displayStatus: bot.daily_lifecycle.display_status,
    statusReason: bot.daily_lifecycle.reason ?? bot.status_detail,
    sickBayCondition,
    presenceLabel: bot.daily_lifecycle.presence_label,
    attentionBadge: bot.daily_lifecycle.attention_badge,
    attentionSeverity: BOT_TONE_SEVERITY[bot.status_tone],
    exposure: bot.metrics.current_exposure,
    openPositions: bot.metrics.open_positions,
    totalPnl: bot.metrics.pnl.total,
    errorCount: bot.metrics.error_count,
    lastRunSortMs: bot.last_run_at_ms ?? 0,
    lastRunAtMs: bot.last_run_at_ms,
    lastRunLabel: bot.last_run_label,
    startRequest: bot.start_request,
    startOfferId: bot.daily_lifecycle.primary_action?.offer_id ?? null,
    startOfferExpiresAtMs: bot.daily_lifecycle.primary_action?.expires_at_ms ?? null,
    attendance: bot.attendance,
    searchText: normalize([
      bot.name,
      bot.strategy_instance_id,
      symbolsLabel,
      bot.status_label,
      bot.status_detail,
      bot.daily_lifecycle.display_status,
      bot.daily_lifecycle.presence_label,
      bot.daily_lifecycle.reason,
      ...(bot.daily_lifecycle.conditions ?? []).flatMap((condition) => [
        condition.title,
        condition.detail,
        condition.cure_label,
        condition.owner_label,
      ]),
      bot.daily_lifecycle.phase,
      bot.trading_mode,
      bot.engine,
      bot.engine_asset_class,
      bot.desired_state,
      bot.last_run_label,
      bot.last_run_result,
      bot.last_run_detail,
      bot.metrics.current_exposure,
      ...bot.attendance.map((cell) => `${cell.session_date} ${cell.label} ${cell.status}`),
    ].filter((value): value is string => typeof value === 'string').join(' ')),
  };
}

function compareRowsForTriage(a: BotTableRow, b: BotTableRow): number {
  return (
    b.lastRunSortMs - a.lastRunSortMs ||
    a.name.localeCompare(b.name)
  );
}
