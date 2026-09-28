import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  resource,
  signal,
  viewChildren,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';

import type { ActivityPeriod, PortfolioHistoryRange } from '../../../../api/alpaca.types';
import { BrokersService, sqliteTimelineQueryFromParams } from '../../../../services/brokers.service';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import { AlpacaDeskAccountDataService } from '../alpaca-desk-account-data.service';
import { AlpacaPortfolioHistoryChartComponent } from '../alpaca-portfolio-history-chart.component';
import { AlpacaPortfolioReconciliationProofComponent } from '../alpaca-portfolio-reconciliation-proof.component';
import { AlpacaTraderActivityTableComponent } from '../alpaca-trader-activity-table.component';
import { AlpacaActivityFeesComponent } from './alpaca-activity-fees.component';
import { AlpacaActivityRecordsComponent, type ActivityRecordsWindow } from './alpaca-activity-records.component';
import { AlpacaActivityStatementComponent } from './alpaca-activity-statement.component';

interface PeriodOption {
  readonly id: ActivityPeriod;
  readonly label: string;
  /** The account value curve this period draws, or `null` for Today, which
   * shows its money statement instead. */
  readonly curve: PortfolioHistoryRange | null;
}

const PERIODS: readonly PeriodOption[] = [
  { id: 'today', label: 'Today', curve: null },
  { id: '30d', label: '30D', curve: '30D' },
  { id: '60d', label: '60D', curve: '60D' },
];

/** The data plane's own ceiling for one activity read. */
const MAX_ACTIVITIES = 100;

/**
 * An account's Activity tab (PRD #2560): its history and records in one
 * view, for Today, 30D or 60D.
 *
 * Every period shows fees per bot and the orders and cash moves; Today adds
 * its money statement, and 30D/60D the account value curve with its equity
 * check. The sync check heads the page, and the order records and recovery
 * sit folded at its foot. Each period's window is the data plane's own
 * calendar anchor: the browser names the period, never an instant, and adds
 * up no money.
 */
@Component({
  selector: 'app-alpaca-activity-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaActivityFeesComponent,
    AlpacaActivityRecordsComponent,
    AlpacaActivityStatementComponent,
    AlpacaPortfolioHistoryChartComponent,
    AlpacaPortfolioReconciliationProofComponent,
    AlpacaTraderActivityTableComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-activity-page.component.html',
  styleUrl: './alpaca-activity.scss',
})
export class AlpacaActivityPageComponent {
  private readonly brokers = inject(BrokersService);
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly route = inject(ActivatedRoute);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });
  private readonly periodRadios = viewChildren<ElementRef<HTMLButtonElement>>('periodRadio');

  protected readonly periods = PERIODS;
  protected readonly period = signal<ActivityPeriod>('today');
  protected readonly selected = computed(
    () => PERIODS.find((option) => option.id === this.period()) ?? PERIODS[0],
  );

  protected readonly target = this.accountData.target;
  protected readonly accountId = this.accountData.accountId;
  protected readonly fence = this.accountData.fence;

  /** One read per period: its fees per owner and, with them, its statement. */
  protected readonly fees = resource({
    params: () => {
      const target = this.target();
      return target === null ? undefined : { target, period: this.period() };
    },
    loader: ({ params }) => this.brokers.getPeriodFees(params.target, params.period),
  });
  protected readonly feeView = computed(() => (this.fees.hasValue() ? this.fees.value() : null));
  protected readonly feesFailed = computed(() => this.fees.error() !== undefined);

  protected readonly activities = resource({
    params: () => {
      const target = this.target();
      return target === null ? undefined : { target, period: this.period() };
    },
    loader: ({ params }) =>
      this.brokers.listActivities(params.target, { period: params.period, limit: MAX_ACTIVITIES }),
  });
  protected readonly activityRows = computed(() =>
    this.activities.hasValue() ? this.activities.value() : undefined,
  );
  protected readonly activitiesFailed = computed(() => this.activities.error() !== undefined);

  protected readonly proof = resource({
    params: () => {
      const target = this.target();
      const curve = this.selected().curve;
      return target === null || curve === null ? undefined : { target, curve };
    },
    loader: ({ params }) => this.brokers.getPortfolioHistoryProof(params.target, params.curve),
  });
  protected readonly proofView = computed(() => (this.proof.hasValue() ? this.proof.value() : undefined));
  protected readonly proofFailed = computed(() => this.proof.error() !== undefined);

  /** The instants the period's order records span — the ones its fees were
   * read over, so the fold never shows another window than the page. */
  protected readonly recordsWindow = computed<ActivityRecordsWindow | null>(() => {
    const view = this.feeView();
    if (view === null || view.period !== this.period() || view.period_start_ms == null) return null;
    return { fromMs: view.period_start_ms, toMs: view.observed_at_ms };
  });

  protected readonly timelineQuery = computed(() => sqliteTimelineQueryFromParams(this.queryParams()));

  /** The latest Clerk↔Alpaca sync check, from the workspace's shared status
   * read. `null` while there is none to show. */
  protected readonly syncCheck = computed(() =>
    this.accountData.clerkStatus.hasValue()
      ? (this.accountData.clerkStatus.value().latest_reconciliation ?? null)
      : null,
  );
  /** Why there is no sync check to show: still reading, the read failed, or
   * the account has never been checked — three different facts. */
  protected readonly syncMissing = computed(() => {
    if (this.accountData.clerkStatus.error() !== undefined) return 'Unavailable right now';
    if (this.accountData.clerkStatus.isLoading()) return 'Checking…';
    return 'Not checked yet';
  });

  protected select(period: ActivityPeriod): void {
    this.period.set(period);
  }

  /** Arrow keys, Home and End move the choice (the ARIA radio group pattern). */
  protected onPeriodKeydown(event: KeyboardEvent, index: number): void {
    const count = PERIODS.length;
    const next =
      event.key === 'ArrowRight' || event.key === 'ArrowDown' ? (index + 1) % count
      : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? (index - 1 + count) % count
      : event.key === 'Home' ? 0
      : event.key === 'End' ? count - 1
      : null;
    if (next === null) return;
    event.preventDefault();
    this.select(PERIODS[next].id);
    this.periodRadios()[next]?.nativeElement.focus();
  }
}
