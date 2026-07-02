import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { PageGuideComponent } from '../../../shared/page-guide/page-guide.component';
import { RouterLink } from '@angular/router';
import { DataSourceComponent } from '../../../shared/data-source/data-source.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { AccountTruthBoardComponent } from '../account-truth-board/account-truth-board.component';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import type { GateResultStatus } from '../../../api/live-instances.types';
import type {
  AccountTruthResponse,
  IbkrPnLTick,
  IbkrPosition,
  IbkrPositionsSnapshot,
} from '../../../api/broker-models';
import type { AccountReconciliationReceipt } from '../../../api/account-reconciliation.types';
import {
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
  private readonly health = inject(BrokerHealthService);
  readonly bannerState = this.health.bannerState;
  private readonly injector = inject(Injector);

  readonly positionsLoading = signal(false);
  readonly positionsError = signal<unknown>(null);
  readonly positionsSnapshot = signal<IbkrPositionsSnapshot | null>(null);
  readonly truthLoading = signal(false);
  readonly truthError = signal<unknown>(null);
  readonly accountTruth = signal<AccountTruthResponse | null>(null);
  readonly accountReconciliation = signal<AccountReconciliationReceipt | null>(null);
  readonly accountReconciliationCheckedAtMs = signal(Date.now());
  readonly accountReconciliationLoading = signal(false);
  readonly accountReconciliationError = signal<unknown>(null);
  readonly accountReconciliationAccountId = computed(() => {
    const truth = this.accountTruth();
    return truth === null ? null : this.reconciliationAccountIdForTruth(truth);
  });
  readonly accountReconciliationExpired = computed(() => {
    const receipt = this.accountReconciliation();
    return receipt !== null && receipt.expires_at_ms < this.accountReconciliationCheckedAtMs();
  });
  readonly accountReconciliationTone = computed(
    () => this.accountReconciliationDisplayGate(),
  );
  readonly accountReconciliationDisplayState = computed(() =>
    this.accountReconciliationExpired()
      ? 'STALE'
      : this.accountReconciliation()?.state ?? 'UNKNOWN',
  );
  readonly accountReconciliationDisplayGate = computed<GateResultStatus>(() =>
    this.accountReconciliationExpired()
      ? 'unknown'
      : this.accountReconciliation()?.final_gate_result.status ?? 'unknown',
  );
  readonly accountReconciliationReason = computed(
    () =>
      this.accountReconciliationExpired()
        ? 'Receipt expired before this account monitor snapshot. Run account reconcile again.'
        : this.accountReconciliation()?.final_gate_result.operator_reason ?? '',
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

  constructor() {
    void this.refresh();

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
        await this.loadLatestAccountReconciliation(accountId);
      } else {
        this.setAccountReconciliation(null);
      }
    } catch (err) {
      this.truthError.set(err);
    } finally {
      this.truthLoading.set(false);
    }
  }

  async loadLatestAccountReconciliation(accountId: string): Promise<void> {
    this.accountReconciliationLoading.set(true);
    this.accountReconciliationError.set(null);
    try {
      this.setAccountReconciliation(await this.broker.latestAccountReconciliation(accountId));
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 404) {
        this.setAccountReconciliation(null);
      } else {
        this.accountReconciliationError.set(err);
      }
    } finally {
      this.accountReconciliationLoading.set(false);
    }
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
    } catch (err) {
      this.accountReconciliationError.set(err);
    } finally {
      this.accountReconciliationLoading.set(false);
    }
  }

  private reconciliationAccountIdForTruth(truth: AccountTruthResponse): string | null {
    return truth.account_id ?? truth.health.account_id ?? null;
  }

  private setAccountReconciliation(receipt: AccountReconciliationReceipt | null): void {
    this.accountReconciliationCheckedAtMs.set(Date.now());
    this.accountReconciliation.set(receipt);
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
