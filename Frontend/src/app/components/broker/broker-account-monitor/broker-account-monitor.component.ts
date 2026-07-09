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
import { DataSourceComponent } from '../../../shared/data-source/data-source.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { AccountTruthBoardComponent } from '../account-truth-board/account-truth-board.component';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import { LiveRunsService } from '../../../services/live-runs.service';
import type { GateResultStatus } from '../../../api/live-instances.types';
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
import {
  fmtBrokerExpiryDate,
  fmtCurrency,
  fmtDateNy,
  fmtSignedCurrency,
  fmtSignedNumber,
} from '../format';

interface PositionRow {
  position: IbkrPosition;
  pnl: IbkrPnLTick | null;
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
    AccountTruthBoardComponent,
    ReceiptLabelPipe,
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
  private readonly fragment = toSignal(this.route.fragment, { initialValue: null });

  readonly positionsLoading = signal(false);
  readonly positionsError = signal<unknown>(null);
  readonly positionsSnapshot = signal<IbkrPositionsSnapshot | null>(null);
  readonly truthLoading = signal(false);
  readonly truthError = signal<unknown>(null);
  readonly accountTruth = signal<AccountTruthResponse | null>(null);
  readonly accountReconciliation = signal<AccountReconciliationReceipt | null>(null);
  readonly accountTriage = signal<AccountTriageResponse | null>(null);
  readonly accountReconciliationNowMs = signal(Date.now());
  readonly accountReconciliationLoading = signal(false);
  readonly accountReconciliationError = signal<unknown>(null);
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
  readonly accountConditions = computed(() => this.accountTriage()?.conditions ?? []);
  readonly accountHasFreeze = computed(() =>
    this.accountConditions().some(
      (condition) =>
        condition.scope === 'account' &&
        (condition.condition_type === 'exposure_freeze' ||
          condition.condition_type === 'account_freeze'),
    ),
  );
  readonly accountReconciliationExpired = computed(() => {
    const receipt = this.accountReconciliation();
    return receipt !== null && receipt.expires_at_ms < this.accountReconciliationNowMs();
  });
  readonly accountReconciliationTone = computed(
    () => this.accountReconciliationDisplayGate(),
  );
  readonly accountReconciliationDisplayState = computed(() =>
    this.accountReconciliationExpired()
      ? 'NOT_PROVEN'
      : this.accountReconciliation()?.state ?? 'NOT_PROVEN',
  );
  readonly accountReconciliationDisplayGate = computed<GateResultStatus>(() =>
    this.accountReconciliationExpired()
      ? 'unknown'
      : this.accountReconciliation()?.final_gate_result.status ?? 'unknown',
  );
  readonly accountReconciliationDisplayGateLabel = computed(() => {
    const status = this.accountReconciliationDisplayGate();
    return status === 'unknown' ? 'Not yet proven' : status;
  });
  readonly accountReconciliationReason = computed(
    () =>
      this.accountReconciliationExpired()
        ? 'Not yet proven: the account reconciliation receipt is stale. Run account reconcile again.'
        : this.accountReconciliation()?.final_gate_result.operator_reason ??
          'Not yet proven: no account-level reconciliation receipt has been recorded for this account.',
  );

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
    await Promise.all([this.loadTruth(), this.loadPositions()]);
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
        await this.loadAccountTriage(accountId);
      } else {
        this.accountTriage.set(null);
        this.setAccountReconciliation(null);
      }
    } catch (err) {
      this.truthError.set(err);
    } finally {
      this.truthLoading.set(false);
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

  async flattenExposureFromDialog(): Promise<void> {
    const condition = this.exposureResolutionCondition();
    const accountId = this.accountReconciliationAccountId();
    const strategyInstanceId = condition?.owner.strategy_instance_id;
    if (!condition || !accountId || !strategyInstanceId || this.exposureResolutionLoading() !== null) return;
    this.exposureResolutionLoading.set('flatten');
    this.accountCureError.set(null);
    try {
      await this.liveRuns.emergencyFlattenAccount(strategyInstanceId, {
        account: accountId,
        confirm: true,
      });
      await this.runAccountReconciliation();
      await this.loadAccountTriage(accountId);
      this.exposureResolutionCondition.set(null);
    } catch (err) {
      this.accountCureError.set(err);
    } finally {
      this.exposureResolutionLoading.set(null);
    }
  }

  async acceptExposureOverrideFromDialog(): Promise<void> {
    const condition = this.exposureResolutionCondition();
    const accountId = this.accountReconciliationAccountId();
    if (
      !condition ||
      !accountId ||
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
    switch (condition.cure_action) {
      case 'resolve_exposure':
        return 'Resolve exposure';
      case 'clear_freeze':
        return 'Clear freeze';
      case 'reconcile_now':
        return 'Run account reconcile';
      case 'prove_evidence':
        return 'Prove now';
      case 'retire_replace':
        return 'Retire & Replace';
    }
  }

  gateStatusLabel(status: GateResultStatus): string {
    return status === 'unknown' ? 'Not yet proven' : status;
  }

  conditionActionDisabled(condition: AccountConditionRow): boolean {
    if (condition.cure_action === 'resolve_exposure') return this.exposureResolutionLoading() !== null;
    if (condition.cure_action === 'reconcile_now') return this.accountReconciliationLoading();
    if (condition.cure_action === 'clear_freeze') {
      return !this.accountTriage()?.clear_freeze_actionable || this.accountFreezeClearLoading();
    }
    return true;
  }

  runConditionAction(condition: AccountConditionRow): void {
    if (condition.cure_action === 'reconcile_now') {
      void this.runAccountReconciliation();
    } else if (condition.cure_action === 'clear_freeze') {
      void this.clearAccountFreeze();
    } else if (condition.cure_action === 'resolve_exposure') {
      this.openExposureResolution(condition);
    }
  }

  private reconciliationAccountIdForTruth(truth: AccountTruthResponse): string | null {
    return truth.account_id ?? truth.health.account_id ?? null;
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
      const conIds = positions
        .map((p) => p.con_id)
        .filter((c): c is number => Number.isFinite(c));
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
}
