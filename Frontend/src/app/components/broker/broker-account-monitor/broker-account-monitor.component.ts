import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { PageGuideComponent } from '../../../shared/page-guide/page-guide.component';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { DialogModule } from 'primeng/dialog';
import { DataSourceComponent } from '../../../shared/data-source/data-source.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { AccountTruthBoardComponent } from '../account-truth-board/account-truth-board.component';
import type { OperatorBlockerMoveEvent } from '../shared/operator-blocker-list/operator-blocker-list.component';
import { AccountFreezeBannerComponent } from '../account-freeze-banner/account-freeze-banner.component';
import { LegacyStaleClaimCureComponent } from '../legacy-stale-claim-cure/legacy-stale-claim-cure.component';
import { ManagedBrokerExposureMeterComponent } from '../managed-broker-exposure-meter/managed-broker-exposure-meter.component';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import { LiveRunsService } from '../../../services/live-runs.service';
import type { FleetContamination, GateResultStatus } from '../../../api/live-instances.types';
import type {
  AccountTruthResponse,
  IbkrPnLTick,
  IbkrPosition,
  IbkrPositionsSnapshot,
} from '../../../api/broker-models';
import type {
  AccountConditionRow,
  AccountReconciliationReceipt,
  AccountTriageResponse,
} from '../../../api/account-reconciliation.types';
import type { AccountClerkHealth, HostRunnerHealth } from '../../../api/live-runs.types';
import {
  fmtBrokerExpiryDate,
  fmtCurrency,
  fmtDateNy,
  fmtDurationRemaining,
  fmtSignedCurrency,
  fmtSignedNumber,
} from '../format';
import { accountConditionActionKind, conditionActionLabel } from '../lib/condition-cure-actions';

interface PositionRow {
  position: IbkrPosition;
  pnl: IbkrPnLTick | null;
}

interface AccountPrimaryAction {
  kind: 'reconcile' | 'resolveExposure' | 'clearFreeze';
  title: string;
  detail: string;
  buttonLabel: string;
  condition?: AccountConditionRow;
}

interface AccountConditionGroups {
  account: AccountConditionRow[];
  bot: AccountConditionRow[];
}

type AccountOutcomeProjectionKey =
  'active_bot' | 'recovery_bot' | 'manual_override' | 'unattributed' | 'unobservable';

type AccountOutcomeProjectionStatus = 'verified' | 'attention' | 'frozen' | 'empty';

interface AccountOutcomeProjectionRow {
  key: AccountOutcomeProjectionKey;
  label: string;
  status: AccountOutcomeProjectionStatus;
  statusLabel: string;
  evidence: readonly AccountOutcomeEvidencePart[];
  effect: string;
}

interface AccountOutcomeEvidencePart {
  value: string;
  receiptLabel?: true;
}

interface ConditionOwnerFact {
  label: string;
  value: string;
}

/**
 * /broker/account-monitor — live account-level + per-position P&L.
 *
 * Subscribes to two SSE streams:
 *   * ``/api/broker/pnl/stream`` for the headline Day / Unrealized /
 *     Realized P&L card (one tick ≈ once per debounce window).
 *   * ``/api/broker/pnl/positions/stream?con_ids=...`` for per-row
 *     market value, position size, and per-position daily P&L.
 *
 * Positions are fetched once on mount; the streams update marks in
 * place. When the consumer leaves the page, the EventSource ``close``
 * triggers backend ``cancelPnL`` / ``cancelPnLSingle`` so we don't
 * leak server-side subscriptions.
 */
@Component({
  selector: 'app-broker-account-monitor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeaderComponent,
    PageGuideComponent,
    DataSourceComponent,
    SectionErrorComponent,
    RouterLink,
    DialogModule,
    AccountTruthBoardComponent,
    AccountFreezeBannerComponent,
    LegacyStaleClaimCureComponent,
    ManagedBrokerExposureMeterComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  styleUrl: './broker-account-monitor.component.scss',
  templateUrl: './broker-account-monitor.component.html',
})
export class BrokerAccountMonitorComponent {
  private readonly broker = inject(BrokerService);
  private readonly liveRuns = inject(LiveRunsService);
  private readonly health = inject(BrokerHealthService);
  readonly bannerState = this.health.bannerState;
  private readonly injector = inject(Injector);
  private readonly route = inject(ActivatedRoute);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly fragment = toSignal(this.route.fragment, {
    initialValue: null,
  });

  readonly positionsLoading = signal(false);
  readonly positionsError = signal<unknown>(null);
  readonly positionsSnapshot = signal<IbkrPositionsSnapshot | null>(null);
  readonly fleetContamination = signal<FleetContamination | null>(null);
  readonly fleetContaminationError = signal<unknown>(null);
  readonly truthLoading = signal(false);
  readonly truthError = signal<unknown>(null);
  readonly accountTruth = signal<AccountTruthResponse | null>(null);
  readonly clerkHealth = signal<HostRunnerHealth | null>(null);
  readonly accountReconciliation = signal<AccountReconciliationReceipt | null>(null);
  readonly accountTriage = signal<AccountTriageResponse | null>(null);
  readonly accountReconciliationNowMs = signal(Date.now());
  readonly accountReconciliationLoading = signal(false);
  readonly accountReconciliationError = signal<unknown>(null);
  readonly accountReconciliationAutomationSaving = signal(false);
  readonly accountReconciliationAutomationError = signal<unknown>(null);
  readonly accountTriageLoading = signal(false);
  readonly accountTriageError = signal<unknown>(null);
  readonly accountCureError = signal<unknown>(null);
  readonly accountFreezeClearLoading = signal(false);
  readonly exposureResolutionCondition = signal<AccountConditionRow | null>(null);
  readonly exposureResolutionLoading = signal<'flatten' | 'override' | null>(null);
  readonly exposureOverrideReason = signal(
    'Operator reviewed and accepts the current account exposure.',
  );
  readonly exposureOverrideReasonMissing = computed(
    () => this.exposureOverrideReason().trim().length === 0,
  );
  readonly accountReconciliationAccountId = computed(() => {
    const truth = this.accountTruth();
    return truth === null ? null : this.reconciliationAccountIdForTruth(truth);
  });
  readonly accountClerk = computed<AccountClerkHealth | null>(() => {
    const truth = this.accountTruth();
    const accountId = truth === null ? null : this.reconciliationAccountIdForTruth(truth);
    if (accountId === null) return null;
    return this.clerkHealth()?.clerks?.find((clerk) => clerk.account_id === accountId) ?? null;
  });
  readonly accountConditions = computed(() => this.accountTriage()?.conditions ?? []);
  readonly accountConditionGroups = computed<AccountConditionGroups>(() => {
    const account: AccountConditionRow[] = [];
    const bot: AccountConditionRow[] = [];
    for (const condition of this.accountConditions()) {
      if (condition.scope === 'bot') {
        bot.push(condition);
      } else {
        account.push(condition);
      }
    }
    return { account, bot };
  });
  readonly accountFreezeBanner = computed(() => this.accountTriage()?.freeze_banner ?? null);
  readonly accountReconciliationAutomationPolicy = computed(
    () => this.accountTriage()?.reconciliation_automation_policy ?? null,
  );
  readonly accountObservation = computed(() => this.accountTriage()?.account_observation ?? null);
  readonly accountOutcomeProjectionRows = computed(() =>
    buildAccountOutcomeProjectionRows(
      this.accountTruth(),
      this.accountTriage(),
      this.accountReconciliation(),
      this.accountReconciliationExpired(),
    ),
  );
  readonly accountReconciliationValidUntilMs = computed(() => {
    const receipt = this.accountReconciliation();
    const triage = this.accountTriage();
    if (
      receipt !== null &&
      triage?.account_reconciliation_receipt?.receipt_id === receipt.receipt_id
    ) {
      return triage.account_reconciliation_valid_until_ms ?? receipt.expires_at_ms;
    }
    return receipt?.expires_at_ms ?? null;
  });
  readonly accountReconciliationExpired = computed(() => {
    const validUntilMs = this.accountReconciliationValidUntilMs();
    return validUntilMs !== null && validUntilMs < this.accountReconciliationNowMs();
  });
  readonly accountReconciliationRemainingLabel = computed(() => {
    const validUntilMs = this.accountReconciliationValidUntilMs();
    return validUntilMs === null
      ? 'Expired'
      : fmtDurationRemaining(validUntilMs - this.accountReconciliationNowMs());
  });
  readonly accountHasOpenBrokerExposure = computed(() =>
    this.truthHasOpenBrokerExposure(this.accountTruth()),
  );
  readonly accountNeedsPostFlattenReconcile = computed(() => {
    const receipt = this.accountReconciliation();
    return (
      receipt !== null &&
      !this.accountReconciliationExpired() &&
      (receipt.exposure_resolution === 'unresolved' ||
        receipt.final_gate_result.operator_next_step === 'RESOLVE_EXPOSURE') &&
      !this.accountHasOpenBrokerExposure()
    );
  });
  readonly accountReconciliationTone = computed(() => this.accountReconciliationDisplayGate());
  readonly accountReconciliationDisplayState = computed(() =>
    this.accountReconciliationExpired()
      ? 'NOT_PROVEN'
      : (this.accountReconciliation()?.state ?? 'NOT_PROVEN'),
  );
  readonly accountReconciliationDisplayGate = computed<GateResultStatus>(() =>
    this.accountReconciliationExpired()
      ? 'unknown'
      : (this.accountReconciliation()?.final_gate_result.status ?? 'unknown'),
  );
  readonly accountReconciliationDisplayGateLabel = computed(() => {
    const status = this.accountReconciliationDisplayGate();
    return status === 'unknown' ? 'Not yet proven' : status;
  });
  readonly accountReconciliationReason = computed(() =>
    this.accountReconciliationExpired()
      ? 'Not yet proven: the account reconciliation receipt is stale. Run account reconcile again.'
      : (this.accountReconciliation()?.final_gate_result.operator_reason ??
        'Not yet proven: no account-level reconciliation receipt has been recorded for this account.'),
  );
  readonly primaryExposureResolutionCondition = computed(
    () =>
      this.accountTriage()?.conditions.find(
        (condition) =>
          accountConditionActionKind(condition) === 'resolveExposure' &&
          !!condition.owner.strategy_instance_id,
      ) ?? null,
  );
  readonly accountPrimaryAction = computed<AccountPrimaryAction | null>(() => {
    const receipt = this.accountReconciliation();
    const truth = this.accountTruth();
    const exposureResolutionCondition = this.primaryExposureResolutionCondition();

    if (
      exposureResolutionCondition !== null &&
      receipt !== null &&
      !this.accountReconciliationExpired() &&
      (receipt.exposure_resolution === 'unresolved' ||
        receipt.final_gate_result.operator_next_step === 'RESOLVE_EXPOSURE') &&
      this.accountHasOpenBrokerExposure()
    ) {
      return {
        kind: 'resolveExposure',
        title: 'Flatten unresolved exposure',
        detail: this.unresolvedBrokerExposureDetail(),
        buttonLabel: 'Resolve exposure',
        condition: exposureResolutionCondition,
      };
    }

    if (this.accountTriage()?.clear_freeze_actionable === true) {
      return {
        kind: 'clearFreeze',
        title: 'Clear account freeze',
        detail:
          'Fresh account proof is clean and the broker account is flat. Clear the freeze to unblock new starts.',
        buttonLabel: 'Clear freeze',
      };
    }

    if (
      this.accountReconciliationDisplayGate() === 'pass' &&
      truth?.final_verdict !== 'not_proven'
    ) {
      return null;
    }

    return {
      kind: 'reconcile',
      title: 'Run account reconcile',
      detail: this.accountReconciliationActionDetail(),
      buttonLabel: 'Run account reconcile',
    };
  });
  readonly accountReconciliationActionDetail = computed(() => {
    const truth = this.accountTruth();
    if (truth?.final_verdict === 'not_proven') {
      return (
        'Account truth is not proven. Run reconciliation to refresh ownership evidence, ' +
        'then clear any remaining sick-bay blockers.'
      );
    }
    if (this.accountNeedsPostFlattenReconcile()) {
      return 'Broker account is flat now. Run account reconcile to record the filled flatten order, then clear the account freeze.';
    }
    if (this.accountReconciliationExpired()) {
      if (truth !== null && !this.accountHasOpenBrokerExposure()) {
        return 'Broker account is flat. Run account reconcile to refresh proof, then clear the account freeze.';
      }
      return 'The account reconciliation receipt is stale. Refresh the account proof before deploying.';
    }
    if (this.accountReconciliation() === null) {
      return 'No account reconciliation receipt has been recorded for this account yet.';
    }
    return this.accountReconciliationReason();
  });
  readonly unresolvedBrokerExposureDetail = computed(() => {
    const exposures =
      this.accountTruth()?.symbol_exposures.filter((row) => row.quantity !== 0) ?? [];
    if (exposures.length === 0) {
      return (
        'Broker exposure is not flat. Close or account for the remaining live position, ' +
        'then run account reconcile again.'
      );
    }
    const summary = exposures
      .map((row) => `${row.symbol} ${fmtSignedNumber(row.quantity, 0)} (${row.owner_label})`)
      .join(', ');
    return (
      `${summary} remains unresolved. Use Resolve exposure on this page to flatten ` +
      'through the owner bot, wait for the fill, then run account reconcile again.'
    );
  });
  readonly accountSickBayDetail = computed(() => {
    const triage = this.accountTriage();
    if (triage === null) return '';
    if (this.accountHasOpenBrokerExposure()) return triage.summary_detail;
    if (this.accountReconciliationExpired()) {
      return 'Broker account is flat. Account sick bay is waiting for a fresh reconciliation receipt.';
    }
    if (triage.clear_freeze_actionable) {
      return 'Broker account is flat with fresh proof. Clear the account freeze when ready.';
    }
    return triage.summary_detail;
  });

  private readonly accountStream = signal<SseStream<IbkrPnLTick> | null>(null);
  private readonly positionStream = signal<SseStream<IbkrPnLTick> | null>(null);

  readonly accountTick = computed(() => this.accountStream()?.latest() ?? null);
  readonly accountStatus = computed(() => this.accountStream()?.status() ?? 'idle');
  readonly accountError = computed(() => this.accountStream()?.lastError() ?? null);

  readonly positionsStatus = computed(() => this.positionStream()?.status() ?? 'idle');
  readonly positionsStreamError = computed(() => this.positionStream()?.lastError() ?? null);

  /**
   * Latest per-conId tick. We can't trust the stream's ``data`` array
   * by itself because every contract emits in turn — instead, fold the
   * stream into a Map keyed by con_id whenever ``data()`` changes.
   */
  private readonly perPositionTicks = signal<Map<number, IbkrPnLTick>>(new Map());

  readonly rows = computed<PositionRow[]>(() => {
    const snap = this.positionsSnapshot();
    if (snap === null) return [];
    const ticks = this.perPositionTicks();
    return snap.positions.map((p) => ({
      position: p,
      pnl: ticks.get(p.con_id) ?? null,
    }));
  });

  readonly canStream = computed(() => {
    const h = this.health.health();
    return h !== null && h.connected;
  });

  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly fmtSignedNumber = fmtSignedNumber;
  readonly fmtDateNy = fmtDateNy;
  readonly fmtBrokerExpiryDate = fmtBrokerExpiryDate;

  constructor() {
    void this.refresh();

    effect((onCleanup) => {
      const id = setInterval(() => this.accountReconciliationNowMs.set(Date.now()), 1_000);
      onCleanup(() => clearInterval(id));
    });

    effect((onCleanup) => {
      if (this.fragment() !== 'account-reconciliation-action') return;
      if (this.truthLoading() || this.accountTruth() === null) return;
      this.scheduleAccountReconciliationFocus(onCleanup);
    });

    // Fold per-conId ticks. The stream's ``data`` is a flat list across
    // all contracts (one entry per (con_id, debounce window)); we
    // reduce to last-tick-wins per con_id.
    effect(() => {
      const stream = this.positionStream();
      if (stream === null) {
        this.perPositionTicks.set(new Map());
        return;
      }
      const data = stream.data();
      if (data.length === 0) return;
      this.perPositionTicks.update((prev) => {
        const next = new Map(prev);
        for (const tick of data) {
          if (tick.con_id !== null) next.set(tick.con_id, tick);
        }
        return next;
      });
    });
  }

  async refresh(): Promise<void> {
    await Promise.all([this.loadTruth(), this.loadPositions(), this.loadClerkHealth()]);
  }

  private async loadClerkHealth(): Promise<void> {
    try {
      this.clerkHealth.set(await this.liveRuns.getHostRunnerHealth());
    } catch {
      this.clerkHealth.set(null);
    }
  }

  async loadTruth(): Promise<void> {
    if (!this.canStream()) return;
    this.truthLoading.set(true);
    this.truthError.set(null);
    try {
      const truth = await this.broker.accountTruth();
      this.accountTruth.set(truth);
      const accountId = this.reconciliationAccountIdForTruth(truth);
      if (accountId) {
        await Promise.all([this.loadAccountTriage(accountId), this.loadFleetContamination()]);
      } else {
        this.accountTriage.set(null);
        this.setAccountReconciliation(null);
        this.fleetContamination.set(null);
      }
    } catch (err) {
      this.truthError.set(err);
    } finally {
      this.truthLoading.set(false);
    }
  }

  private async loadFleetContamination(): Promise<void> {
    this.fleetContaminationError.set(null);
    try {
      this.fleetContamination.set(await this.liveRuns.getAccountFleet());
    } catch (err) {
      this.fleetContamination.set(null);
      this.fleetContaminationError.set(err);
    }
  }

  async loadAccountTriage(accountId: string): Promise<void> {
    this.accountTriageLoading.set(true);
    this.accountTriageError.set(null);
    try {
      this.setAccountTriage(await this.broker.accountTriage(accountId));
    } catch (err) {
      this.accountTriageError.set(err);
      this.accountTriage.set(null);
      this.setAccountReconciliation(null);
    } finally {
      this.accountTriageLoading.set(false);
    }
  }

  retryAccountTriage(): void {
    const accountId = this.accountReconciliationAccountId();
    if (accountId) void this.loadAccountTriage(accountId);
  }

  async runAccountReconciliation(): Promise<void> {
    const accountId = this.accountReconciliationAccountId();
    if (!accountId || this.accountReconciliationLoading()) return;
    this.accountReconciliationLoading.set(true);
    this.accountReconciliationError.set(null);
    try {
      const receipt = await this.broker.reconcileAccount(accountId);
      this.setAccountReconciliation(receipt);
      this.accountTruth.set(receipt.account_truth);
      await this.loadAccountTriage(accountId);
    } catch (err) {
      this.accountReconciliationError.set(err);
    } finally {
      this.accountReconciliationLoading.set(false);
    }
  }

  async updateAccountReconciliationAutomation(event: Event): Promise<void> {
    const input = event.target;
    const accountId = this.accountReconciliationAccountId();
    if (
      !(input instanceof HTMLInputElement) ||
      !accountId ||
      this.accountReconciliationAutomationSaving()
    ) {
      return;
    }
    this.accountReconciliationAutomationSaving.set(true);
    this.accountReconciliationAutomationError.set(null);
    try {
      const policy = await this.broker.updateAccountReconciliationAutomation(accountId, {
        enabled: input.checked,
      });
      this.accountTriage.update((triage) =>
        triage === null ? null : { ...triage, reconciliation_automation_policy: policy },
      );
    } catch (err) {
      this.accountReconciliationAutomationError.set(err);
      input.checked = this.accountReconciliationAutomationPolicy()?.enabled ?? false;
    } finally {
      this.accountReconciliationAutomationSaving.set(false);
    }
  }

  async clearAccountFreeze(): Promise<void> {
    const accountId = this.accountReconciliationAccountId();
    const triage = this.accountTriage();
    if (!accountId || !triage?.clear_freeze_actionable || this.accountFreezeClearLoading()) return;
    this.accountFreezeClearLoading.set(true);
    this.accountCureError.set(null);
    try {
      const response = await this.broker.clearAccountFreeze(accountId);
      this.setAccountTriage(response.triage);
    } catch (err) {
      this.accountCureError.set(err);
    } finally {
      this.accountFreezeClearLoading.set(false);
    }
  }

  openExposureResolution(condition: AccountConditionRow): void {
    this.accountCureError.set(null);
    this.exposureOverrideReason.set(
      `Operator reviewed and accepts exposure for ${condition.owner.label}.`,
    );
    this.exposureResolutionCondition.set(condition);
  }

  closeExposureResolution(): void {
    if (this.exposureResolutionLoading() !== null) return;
    this.exposureResolutionCondition.set(null);
  }

  setExposureOverrideReason(value: string): void {
    this.exposureOverrideReason.set(value);
  }

  setExposureOverrideReasonFromEvent(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLTextAreaElement) {
      this.setExposureOverrideReason(target.value);
    }
  }

  async flattenExposureFromDialog(): Promise<void> {
    const condition = this.exposureResolutionCondition();
    const accountId = this.accountReconciliationAccountId();
    const strategyInstanceId = condition?.owner.strategy_instance_id;
    if (
      !condition ||
      !accountId ||
      !strategyInstanceId ||
      this.exposureResolutionLoading() !== null
    )
      return;
    this.exposureResolutionLoading.set('flatten');
    this.accountCureError.set(null);
    try {
      await this.liveRuns.emergencyFlattenAccount(strategyInstanceId, {
        account: accountId,
        confirm: true,
      });
      await this.runAccountReconciliation();
      this.exposureResolutionCondition.set(null);
    } catch (err) {
      this.accountCureError.set(err);
    } finally {
      this.exposureResolutionLoading.set(null);
    }
  }

  handleAccountMonitorBlockerMove(event: OperatorBlockerMoveEvent): void {
    const action = event.move.action;
    if (action.kind !== 'confirm_in_form' || typeof document === 'undefined') return;
    document.getElementById(action.anchor)?.focus();
  }

  async acceptExposureOverrideFromDialog(): Promise<void> {
    const condition = this.exposureResolutionCondition();
    const accountId = this.accountReconciliationAccountId();
    if (
      !condition ||
      !accountId ||
      !condition.owner.strategy_instance_id ||
      !condition.owner.run_id ||
      this.exposureResolutionLoading() !== null ||
      this.exposureOverrideReasonMissing()
    ) {
      return;
    }
    this.exposureResolutionLoading.set('override');
    this.accountCureError.set(null);
    try {
      const response = await this.broker.acceptExposureOverride(accountId, {
        reason: this.exposureOverrideReason().trim(),
        strategy_instance_id: condition.owner.strategy_instance_id,
        run_id: condition.owner.run_id,
        bot_order_namespace: null,
      });
      this.setAccountTriage(response.triage);
      this.exposureResolutionCondition.set(null);
    } catch (err) {
      this.accountCureError.set(err);
    } finally {
      this.exposureResolutionLoading.set(null);
    }
  }

  conditionActionLabel(condition: AccountConditionRow): string {
    return conditionActionLabel(condition.cure_action);
  }

  hasConditionAction(condition: AccountConditionRow): boolean {
    const actionKind = accountConditionActionKind(condition);
    if (actionKind === null) {
      return false;
    }
    if (actionKind === 'clearFreeze' && this.accountTriage()?.clear_freeze_actionable === true) {
      return false;
    }
    return true;
  }

  gateStatusLabel(status: GateResultStatus): string {
    return status === 'unknown' ? 'Not yet proven' : status;
  }

  conditionActionDisabled(condition: AccountConditionRow): boolean {
    const actionKind = accountConditionActionKind(condition);
    if (actionKind === 'resolveExposure') return this.exposureResolutionLoading() !== null;
    if (actionKind === 'reconcile') return this.accountReconciliationLoading();
    if (actionKind === 'clearFreeze') {
      return !this.accountTriage()?.clear_freeze_actionable || this.accountFreezeClearLoading();
    }
    return true;
  }

  exposureOverrideActionDisabled(condition: AccountConditionRow): boolean {
    return (
      this.exposureResolutionLoading() !== null ||
      this.exposureOverrideReasonMissing() ||
      !condition.owner.strategy_instance_id ||
      !condition.owner.run_id
    );
  }

  conditionOwnerFacts(condition: AccountConditionRow): ConditionOwnerFact[] {
    const facts: ConditionOwnerFact[] = [];
    if (condition.owner.strategy_instance_id) {
      facts.push({
        label: 'Strategy instance',
        value: condition.owner.strategy_instance_id,
      });
    }
    if (condition.owner.run_id) {
      facts.push({ label: 'Run', value: condition.owner.run_id });
    }
    if (condition.owner.lifecycle_state) {
      facts.push({
        label: 'Lifecycle',
        value: condition.owner.lifecycle_state,
      });
    }
    if (condition.affected_strategy_instance_ids.length > 0) {
      facts.push({
        label: 'Affected',
        value: condition.affected_strategy_instance_ids.join(', '),
      });
    }
    return facts;
  }

  reviveActionVisibleButDisabled(condition: AccountConditionRow): boolean {
    return (
      condition.owner.owner_type === 'bot' &&
      condition.owner.lifecycle_state === 'RETIRED' &&
      !!condition.owner.strategy_instance_id
    );
  }

  primaryActionDisabled(action: AccountPrimaryAction): boolean {
    if (action.kind === 'reconcile') {
      return this.accountReconciliationLoading() || !this.accountReconciliationAccountId();
    }
    if (action.kind === 'clearFreeze') {
      return (
        this.accountFreezeClearLoading() || this.accountTriage()?.clear_freeze_actionable !== true
      );
    }
    if (action.kind === 'resolveExposure') {
      return (
        this.exposureResolutionLoading() !== null || !action.condition?.owner.strategy_instance_id
      );
    }
    return false;
  }

  runPrimaryAccountAction(action: AccountPrimaryAction): void {
    if (action.kind === 'reconcile') {
      void this.runAccountReconciliation();
    } else if (action.kind === 'clearFreeze') {
      void this.clearAccountFreeze();
    } else if (action.kind === 'resolveExposure' && action.condition) {
      this.openExposureResolution(action.condition);
    }
  }

  primaryActionButtonLabel(action: AccountPrimaryAction): string {
    if (action.kind === 'reconcile' && this.accountReconciliationLoading()) {
      return 'Reconciling…';
    }
    if (action.kind === 'clearFreeze' && this.accountFreezeClearLoading()) {
      return 'Clearing…';
    }
    return action.buttonLabel;
  }

  runConditionAction(condition: AccountConditionRow): void {
    const actionKind = accountConditionActionKind(condition);
    if (actionKind === 'reconcile') {
      void this.runAccountReconciliation();
    } else if (actionKind === 'clearFreeze') {
      void this.clearAccountFreeze();
    } else if (actionKind === 'resolveExposure') {
      this.openExposureResolution(condition);
    }
  }

  private reconciliationAccountIdForTruth(truth: AccountTruthResponse): string | null {
    return truth.account_id ?? truth.health.account_id ?? null;
  }

  private truthHasOpenBrokerExposure(truth: AccountTruthResponse | null): boolean {
    return (
      truth?.positions.some((position) => position.quantity !== 0) === true ||
      truth?.symbol_exposures.some((row) => row.quantity !== 0) === true
    );
  }

  private setAccountReconciliation(receipt: AccountReconciliationReceipt | null): void {
    this.accountReconciliationNowMs.set(Date.now());
    this.accountReconciliation.set(receipt);
  }

  private setAccountTriage(triage: AccountTriageResponse | null): void {
    this.accountTriage.set(triage);
    this.setAccountReconciliation(triage?.account_reconciliation_receipt ?? null);
  }

  async loadPositions(): Promise<void> {
    if (!this.canStream()) return;
    this.positionsLoading.set(true);
    this.positionsError.set(null);
    try {
      const snap = await this.broker.positions();
      this.positionsSnapshot.set(snap);
      this.openStreams(snap.positions);
    } catch (err) {
      this.positionsError.set(err);
    } finally {
      this.positionsLoading.set(false);
    }
  }

  private scheduleAccountReconciliationFocus(onCleanup: (cleanupFn: () => void) => void): void {
    let timeoutId: number | null = null;
    let attempts = 0;
    const focus = () => {
      timeoutId = null;
      if (this.focusAccountReconciliationAction()) return;
      attempts += 1;
      if (attempts < 12) {
        timeoutId = window.setTimeout(focus, 50);
      }
    };
    timeoutId = window.setTimeout(focus, 0);
    onCleanup(() => {
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    });
  }

  private focusAccountReconciliationAction(): boolean {
    const target = this.host.nativeElement.querySelector<HTMLElement>(
      '#account-reconciliation-action',
    );
    if (!target) return false;
    target.scrollIntoView({ block: 'center' });
    target.focus();
    return true;
  }

  private openStreams(positions: IbkrPosition[]): void {
    // Tear down any previous streams.
    this.accountStream()?.close();
    this.positionStream()?.close();
    this.accountStream.set(null);
    this.positionStream.set(null);

    const account = runInInjectionContext(this.injector, () =>
      brokerSse<IbkrPnLTick>('/api/broker/pnl/stream?debounce_ms=1000', 'pnl', {
        maxBuffer: 1,
      }),
    );
    this.accountStream.set(account);

    if (positions.length > 0) {
      const conIds = positions.map((p) => p.con_id).filter((c): c is number => Number.isFinite(c));
      const query = conIds.map((c) => `con_ids=${c}`).join('&');
      const positionStream = runInInjectionContext(this.injector, () =>
        brokerSse<IbkrPnLTick>(
          `/api/broker/pnl/positions/stream?${query}&debounce_ms=1000`,
          'pnl',
          { maxBuffer: positions.length * 60 },
        ),
      );
      this.positionStream.set(positionStream);
    }
  }

  trackRow = (_: number, row: PositionRow): number => row.position.con_id;
  trackCondition = (_: number, condition: AccountConditionRow): string =>
    `${condition.scope}:${condition.condition_type}:${condition.owner.owner_id}:${condition.evidence_at_ms}`;
}

function buildAccountOutcomeProjectionRows(
  truth: AccountTruthResponse | null,
  triage: AccountTriageResponse | null,
  receipt: AccountReconciliationReceipt | null,
  receiptExpired: boolean,
): AccountOutcomeProjectionRow[] {
  const ownerSummaries = truth?.owner_summaries ?? [];
  const openExposures = truth?.symbol_exposures.filter((row) => row.quantity !== 0) ?? [];
  const conditions = triage?.conditions ?? [];
  const observation = triage?.account_observation ?? null;
  const staleSources = truth?.source_freshness.filter((row) => row.status !== 'fresh') ?? [];

  const activeBotOwners = ownerSummaries.filter(
    (row) =>
      row.owner_class === 'bot' &&
      (row.owner_binding_state === 'ACTIVE' || row.owner_binding_state === 'DEPLOYED'),
  );
  const retiredBotOwners = ownerSummaries.filter(
    (row) =>
      (row.owner_class === 'bot' || row.owner_class === 'mixed_known') &&
      row.owner_binding_state === 'RETIRED',
  );
  const retiredBotConditions = conditions.filter(
    (condition) =>
      condition.owner.owner_type === 'bot' && condition.owner.lifecycle_state === 'RETIRED',
  );
  const manualOwners = ownerSummaries.filter((row) => row.owner_class === 'manual');
  const unattributedExposures = openExposures.filter(
    (row) => row.owner_class === 'foreign_or_unclaimed',
  );
  const unattributedOwners = ownerSummaries.filter(
    (row) => row.owner_class === 'foreign_or_unclaimed',
  );

  const unobservableEvidence =
    observation !== null && observation.state !== 'VERIFIED'
      ? plainEvidence(observation.reason_line)
      : staleSources.length > 0
        ? summarizeFreshness(staleSources)
        : truth === null
          ? plainEvidence('Account truth has not loaded.')
          : plainEvidence('Account observation and source freshness are usable.');

  return [
    {
      key: 'active_bot',
      label: 'Active bot-owned',
      status: activeBotOwners.length > 0 ? 'verified' : 'empty',
      statusLabel: activeBotOwners.length > 0 ? 'Verified' : 'No evidence',
      evidence:
        activeBotOwners.length > 0
          ? summarizeOwners(activeBotOwners)
          : plainEvidence('No active bot-owned exposure in current account truth.'),
      effect:
        activeBotOwners.length > 0
          ? 'Route orders and cures through the active bot owner.'
          : 'No active bot owner needs operator action.',
    },
    {
      key: 'recovery_bot',
      label: 'Retired bot recovery',
      status:
        retiredBotConditions.length > 0
          ? 'frozen'
          : retiredBotOwners.length > 0
            ? 'attention'
            : 'empty',
      statusLabel:
        retiredBotConditions.length > 0
          ? 'Frozen'
          : retiredBotOwners.length > 0
            ? 'Attention'
            : 'No evidence',
      evidence:
        retiredBotConditions.length > 0
          ? summarizeRetiredConditions(retiredBotConditions)
          : retiredBotOwners.length > 0
            ? summarizeOwners(retiredBotOwners)
            : plainEvidence('No retired bot-owned exposure in current account truth.'),
      effect:
        retiredBotConditions.length > 0
          ? 'Resolve or audit the exposure before any new start.'
          : retiredBotOwners.length > 0
            ? 'Do not revive automatically; wait for guarded account pin evidence.'
            : 'No retired bot recovery path is active.',
    },
    {
      key: 'manual_override',
      label: 'Accepted manual override',
      status:
        receipt?.exposure_resolution === 'accepted_override' && !receiptExpired
          ? 'verified'
          : manualOwners.length > 0 ||
              (receipt?.exposure_resolution === 'accepted_override' && receiptExpired)
            ? 'attention'
            : 'empty',
      statusLabel:
        receipt?.exposure_resolution === 'accepted_override' && !receiptExpired
          ? 'Verified'
          : manualOwners.length > 0 ||
              (receipt?.exposure_resolution === 'accepted_override' && receiptExpired)
            ? 'Attention'
            : 'No evidence',
      evidence:
        receipt?.exposure_resolution === 'accepted_override'
          ? receiptExpired
            ? plainEvidence('The last accepted exposure override is expired.')
            : plainEvidence('Current receipt records exposure_resolution=accepted_override.')
          : manualOwners.length > 0
            ? summarizeOwners(manualOwners)
            : plainEvidence('No manual override is exposed by the current receipt.'),
      effect:
        receipt?.exposure_resolution === 'accepted_override' && !receiptExpired
          ? 'Keep the audited acceptance visible until receipt expiry.'
          : manualOwners.length > 0
            ? 'Require a fresh audited acceptance or flatten before deploy.'
            : 'No manual acceptance is active.',
    },
    {
      key: 'unattributed',
      label: 'Unattributed exposure',
      status:
        unattributedExposures.length > 0 || unattributedOwners.length > 0 ? 'frozen' : 'empty',
      statusLabel:
        unattributedExposures.length > 0 || unattributedOwners.length > 0
          ? 'Frozen'
          : 'No evidence',
      evidence:
        unattributedExposures.length > 0
          ? summarizeExposures(unattributedExposures)
          : unattributedOwners.length > 0
            ? summarizeOwners(unattributedOwners)
            : plainEvidence('No foreign or unclaimed exposure is visible.'),
      effect:
        unattributedExposures.length > 0 || unattributedOwners.length > 0
          ? 'Keep the account frozen until the exposure is flattened or audited.'
          : 'No unattributed exposure is blocking starts.',
    },
    {
      key: 'unobservable',
      label: 'Unobservable account',
      status:
        observation !== null && observation.state !== 'VERIFIED'
          ? 'frozen'
          : staleSources.length > 0 || truth === null
            ? 'attention'
            : 'verified',
      statusLabel:
        observation !== null && observation.state !== 'VERIFIED'
          ? 'Frozen'
          : staleSources.length > 0 || truth === null
            ? 'Attention'
            : 'Verified',
      evidence: unobservableEvidence,
      effect:
        observation !== null && observation.state !== 'VERIFIED'
          ? 'Refresh account verification before deploy.'
          : staleSources.length > 0 || truth === null
            ? 'Refresh reconciliation evidence before deploy.'
            : 'Verification evidence is current enough for this view.',
    },
  ];
}

function plainEvidence(value: string): AccountOutcomeEvidencePart[] {
  return [{ value }];
}

function receiptEvidence(value: string): AccountOutcomeEvidencePart {
  return { value, receiptLabel: true };
}

function summarizeOwners(
  rows: AccountTruthResponse['owner_summaries'],
): AccountOutcomeEvidencePart[] {
  return plainEvidence(
    summarizeList(
      rows.map(
        (row) =>
          `${row.owner_label}: ${row.owner_binding_state}, positions ${row.position_count}, open orders ${row.open_order_count}, executions ${row.execution_count}`,
      ),
    ),
  );
}

function summarizeExposures(
  rows: AccountTruthResponse['symbol_exposures'],
): AccountOutcomeEvidencePart[] {
  return plainEvidence(
    summarizeList(
      rows.map((row) => `${row.symbol} ${fmtSignedNumber(row.quantity, 0)} (${row.owner_label})`),
    ),
  );
}

function summarizeFreshness(
  rows: AccountTruthResponse['source_freshness'],
): AccountOutcomeEvidencePart[] {
  return summarizeEvidenceParts(
    rows.map((row) => [
      { value: `${row.label}: ${row.status}` },
      ...(row.reason_code ? [{ value: ' (' }, receiptEvidence(row.reason_code), { value: ')' }] : []),
    ]),
  );
}

function summarizeRetiredConditions(rows: AccountConditionRow[]): AccountOutcomeEvidencePart[] {
  return summarizeEvidenceParts(
    rows.map((row) => {
      const owner = row.owner.strategy_instance_id ?? row.owner.owner_id;
      const run = row.owner.run_id === null ? '' : ` / ${row.owner.run_id}`;
      return [{ value: `${owner}${run}: ` }, receiptEvidence(row.detail)];
    }),
  );
}

function summarizeEvidenceParts(
  values: readonly (readonly AccountOutcomeEvidencePart[])[],
): AccountOutcomeEvidencePart[] {
  const visible = values.slice(0, 3);
  const parts: AccountOutcomeEvidencePart[] = [];
  visible.forEach((value, index) => {
    if (index > 0) parts.push({ value: '; ' });
    parts.push(...value);
  });
  if (values.length > visible.length) {
    parts.push({ value: `; +${values.length - visible.length} more` });
  }
  return parts;
}

function summarizeList(values: string[]): string {
  if (values.length <= 3) return values.join('; ');
  return `${values.slice(0, 3).join('; ')}; +${values.length - 3} more`;
}
