import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaPositionsTableComponent } from './alpaca-positions-table.component';
import { AlpacaTraderActivityTimelineComponent } from './alpaca-trader-activity-timeline.component';
import { AlpacaTraderHeroComponent } from './alpaca-trader-hero.component';
import { AlpacaTraderLensDataService } from './alpaca-trader-lens-data.service';

type TraderScope = 'today' | '30d' | '60d';

const SCOPE_LABELS: Readonly<Record<TraderScope, string>> = {
  today: 'Today',
  '30d': '30D',
  '60d': '60D',
};

const SCOPE_OPTIONS: readonly TraderScope[] = ['today', '30d', '60d'];

/** Outcomes-focused account view. Historical scopes gain their data in Contract C1. */
@Component({
  selector: 'app-alpaca-trader-lens',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaPositionsTableComponent,
    AlpacaTraderActivityTimelineComponent,
    AlpacaTraderHeroComponent,
  ],
  templateUrl: './alpaca-trader-lens.component.html',
  styleUrl: './alpaca-trader-lens.component.scss',
  providers: [AlpacaTraderLensDataService],
})
export class AlpacaTraderLensComponent {
  protected readonly accountData = inject(AlpacaDeskAccountDataService);
  protected readonly traderData = inject(AlpacaTraderLensDataService);
  protected readonly scope = signal<TraderScope>('today');
  protected readonly scopeLabels = SCOPE_LABELS;
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

  protected selectScope(scope: TraderScope): void {
    this.scope.set(scope);
  }
}
