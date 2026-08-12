import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import type { PortfolioHistoryRange } from '../../../api/alpaca.types';
import { AccountDeskTransactionHistoryComponent } from '../../broker/account-desk/account-desk-transaction-history.component';
import { AccountDeskTransactionHistoryStore } from '../../broker/account-desk/account-desk-transaction-history-store.service';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaPortfolioHistoryChartComponent } from './alpaca-portfolio-history-chart.component';
import { AlpacaPositionsTableComponent } from './alpaca-positions-table.component';
import { AlpacaTraderActivityTimelineComponent } from './alpaca-trader-activity-timeline.component';
import { AlpacaTraderHeroComponent } from './alpaca-trader-hero.component';
import { AlpacaTraderLensDataService } from './alpaca-trader-lens-data.service';

type TraderScope = 'today' | '30d' | '60d';

interface HistoryWindow {
  readonly fromMs: number;
  readonly toMs: number;
}

const SCOPE_OPTIONS: readonly TraderScope[] = ['today', '30d', '60d'];

const SCOPE_CONFIG = {
  today: { label: 'Today', range: undefined, windowDays: undefined },
  '30d': { label: '30D', range: '30D', windowDays: 30 },
  '60d': { label: '60D', range: '60D', windowDays: 60 },
} as const satisfies Record<
  TraderScope,
  { readonly label: string; readonly range: PortfolioHistoryRange | undefined; readonly windowDays: number | undefined }
>;

const MS_PER_DAY = 24 * 60 * 60 * 1_000;

/** Outcomes-focused account view; historical scopes render the C1 broker curve. */
@Component({
  selector: 'app-alpaca-trader-lens',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaPositionsTableComponent,
    AccountDeskTransactionHistoryComponent,
    AlpacaPortfolioHistoryChartComponent,
    AlpacaTraderActivityTimelineComponent,
    AlpacaTraderHeroComponent,
  ],
  templateUrl: './alpaca-trader-lens.component.html',
  styleUrl: './alpaca-trader-lens.component.scss',
  providers: [AlpacaTraderLensDataService, AccountDeskTransactionHistoryStore],
})
export class AlpacaTraderLensComponent {
  protected readonly accountData = inject(AlpacaDeskAccountDataService);
  protected readonly traderData = inject(AlpacaTraderLensDataService);
  protected readonly scope = signal<TraderScope>('today');
  private readonly historyWindowState = signal<HistoryWindow | null>(null);
  protected readonly scopeConfig = SCOPE_CONFIG;
  protected readonly scopeOptions = SCOPE_OPTIONS;
  protected readonly account = computed(() =>
    this.accountData.account.hasValue() ? this.accountData.account.value() : undefined,
  );
  protected readonly positions = computed(() =>
    this.traderData.positions.hasValue() ? this.traderData.positions.value() : undefined,
  );
  protected readonly activities = computed(() =>
    this.traderData.activities.hasValue() ? this.traderData.activities.value() : undefined,
  );
  protected readonly positionsUnavailable = computed(
    () => this.traderData.positions.error() !== undefined,
  );
  protected readonly activitiesUnavailable = computed(
    () => this.traderData.activities.error() !== undefined,
  );
  protected readonly portfolioHistory = computed(() =>
    this.traderData.portfolioHistory.hasValue()
      ? this.traderData.portfolioHistory.value()
      : undefined,
  );
  protected readonly portfolioHistoryUnavailable = computed(
    () => this.traderData.portfolioHistory.error() !== undefined,
  );
  protected readonly historyWindow = this.historyWindowState.asReadonly();

  protected selectScope(scope: TraderScope): void {
    this.scope.set(scope);
    const config = SCOPE_CONFIG[scope];
    this.traderData.selectPortfolioHistoryRange(config.range);
    const { windowDays: days } = config;
    if (days === undefined) {
      this.historyWindowState.set(null);
      return;
    }
    const toMs = Date.now();
    this.historyWindowState.set({ fromMs: toMs - days * MS_PER_DAY, toMs });
  }
}
