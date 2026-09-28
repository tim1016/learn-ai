import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  resource,
  signal,
  viewChild,
  viewChildren,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';

import type { ActivityPeriod, ActivityPeriodRead, PortfolioHistoryRange } from '../../../../api/alpaca.types';
import { BrokersService, sqliteTimelineQueryFromParams } from '../../../../services/brokers.service';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import { FeeAttributionComponent } from '../../../broker/fee-attribution/fee-attribution.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk-account-data.service';
import { AlpacaPortfolioHistoryChartComponent } from '../alpaca-portfolio-history-chart.component';
import { AlpacaPortfolioReconciliationProofComponent } from '../alpaca-portfolio-reconciliation-proof.component';
import { AlpacaTraderActivityTableComponent } from '../alpaca-trader-activity-table.component';
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

/** The older stretches "Load older" read after a period's first read, kept
 * only while they continue that same read. */
interface OlderActivity {
  readonly first: ActivityPeriodRead;
  readonly reads: readonly ActivityPeriodRead[];
  readonly loading: boolean;
  readonly failed: boolean;
}

/**
 * An account's Activity tab (PRD #2560): its history and records in one
 * view, for Today, 30D or 60D.
 *
 * Every period shows fees per bot and the orders and cash moves; Today adds
 * its money statement, and 30D/60D the account value curve with its equity
 * check. The sync check heads the page, and the order records and recovery
 * sit folded at its foot. Each period's window is the data plane's own
 * calendar anchor: the browser names the period, never an instant, and adds
 * up no money. A period with more orders and cash moves than one bounded
 * read reaches says so, and reads older ones on request.
 */
@Component({
  selector: 'app-alpaca-activity-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaActivityRecordsComponent,
    AlpacaActivityStatementComponent,
    AlpacaPortfolioHistoryChartComponent,
    AlpacaPortfolioReconciliationProofComponent,
    AlpacaTraderActivityTableComponent,
    FeeAttributionComponent,
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
  private readonly injector = inject(Injector);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });
  private readonly periodRadios = viewChildren<ElementRef<HTMLButtonElement>>('periodRadio');
  private readonly activityCount = viewChild<ElementRef<HTMLElement>>('activityCount');

  protected readonly periods = PERIODS;
  protected readonly period = signal<ActivityPeriod>('today');
  protected readonly selected = computed(
    () => PERIODS.find((option) => option.id === this.period()) ?? PERIODS[0],
  );

  protected readonly target = this.accountData.target;
  protected readonly accountId = this.accountData.accountId;
  protected readonly fence = this.accountData.fence;

  /** Today's statement, read only while Today is the period shown. */
  protected readonly today = resource({
    params: () => {
      const target = this.target();
      return target === null || this.period() !== 'today' ? undefined : { target };
    },
    loader: ({ params }) => this.brokers.getTodayStatement(params.target),
  });
  protected readonly todayView = computed(() => (this.today.hasValue() ? this.today.value() : null));
  protected readonly todayFailed = computed(() => this.today.error() !== undefined);

  /** The period's newest orders and cash moves: one bounded read. */
  protected readonly activities = resource({
    params: () => {
      const target = this.target();
      return target === null ? undefined : { target, period: this.period() };
    },
    loader: ({ params }) => this.brokers.getActivityPeriod(params.target, params.period),
  });
  private readonly firstRead = computed(() => (this.activities.hasValue() ? this.activities.value() : null));
  private readonly older = signal<OlderActivity | null>(null);
  /** "Load older" state for the current first read only: a new period or a
   * re-read starts again from the newest rows. */
  private readonly olderState = computed(() => {
    const first = this.firstRead();
    const older = this.older();
    return first !== null && older?.first === first ? older : null;
  });
  private readonly lastRead = computed(() => this.olderState()?.reads.at(-1) ?? this.firstRead());
  protected readonly activityRows = computed(() => {
    const first = this.firstRead();
    return first === null
      ? undefined
      : [first, ...(this.olderState()?.reads ?? [])].flatMap((read) => read.evidence.activities);
  });
  protected readonly activitiesFailed = computed(() => this.activities.error() !== undefined);
  /** Whether every order and cash move in the period has been read. */
  protected readonly activitiesComplete = computed(() => this.lastRead()?.evidence.history_complete ?? true);
  protected readonly olderToken = computed(() => this.lastRead()?.evidence.next_page_token ?? null);
  protected readonly loadingOlder = computed(() => this.olderState()?.loading ?? false);
  protected readonly olderFailed = computed(() => this.olderState()?.failed ?? false);

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

  /** The instants the period's order records span — the ones its orders and
   * cash moves were read over, so the fold never shows another window. */
  protected readonly recordsWindow = computed<ActivityRecordsWindow | null>(() => {
    const first = this.firstRead();
    return first === null || first.period !== this.period()
      ? null
      : { fromMs: first.period_start_ms, toMs: first.observed_at_ms };
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

  /** Read the next older stretch of the period's orders and cash moves. */
  protected async loadOlder(): Promise<void> {
    const target = this.target();
    const first = this.firstRead();
    const token = this.olderToken();
    if (target === null || first === null || token === null || this.loadingOlder()) return;
    const reads = this.olderState()?.reads ?? [];
    this.older.set({ first, reads, loading: true, failed: false });
    try {
      const read = await this.brokers.getActivityPeriod(target, first.period, token);
      // Another period's older reads may have replaced this state meanwhile; never clobber them.
      if (this.older()?.first !== first) return;
      this.older.set({ first, reads: [...reads, read], loading: false, failed: false });
      if (read.evidence.history_complete) {
        // "Load older" is gone; keep focus on what replaced it.
        afterNextRender(() => this.activityCount()?.nativeElement.focus(), { injector: this.injector });
      }
    } catch {
      if (this.older()?.first === first) this.older.set({ first, reads, loading: false, failed: true });
    }
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
