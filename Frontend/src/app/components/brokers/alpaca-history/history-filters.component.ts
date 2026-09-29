import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';

import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneDisplayNameText } from '../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService, verdictModeChip } from '../../../services/alpaca-live-verdict.service';
import { InstrumentCardComponent } from '../../../shared/ticker-range-picker/parts/instrument-card.component';
import type { TickerOption, TickerRange } from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import {
  BOT_HISTORY_STATUSES,
  BOT_HISTORY_WORLDS,
  type BotHistoryQuery,
  type BotHistoryQueryParams,
} from './bot-history.service';

/** One filter the owner changed: which URL parameter, and its new value
 * (`null` clears it). */
export interface HistoryFilterChange {
  readonly param: keyof Omit<BotHistoryQueryParams, 'page'>;
  readonly value: string | null;
}

/**
 * History's filters (#2574): account, status, world and symbol. Each account
 * is named with its mode worded, as everywhere. The symbol filter is the
 * shared instrument card over the symbols the history itself holds -- a
 * closed list the page owns outright, so a pick is never gated on the lake.
 */
@Component({
  selector: 'app-history-filters',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [InstrumentCardComponent],
  templateUrl: './history-filters.component.html',
  styleUrl: './history-filters.component.scss',
})
export class HistoryFiltersComponent {
  readonly query = input.required<BotHistoryQuery>();
  /** Every symbol the history's read rows trade. */
  readonly symbols = input.required<readonly string[]>();

  readonly filterChanged = output<HistoryFilterChange>();

  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);

  protected readonly statuses = BOT_HISTORY_STATUSES;
  protected readonly worlds = BOT_HISTORY_WORLDS;

  protected readonly accounts = computed(() =>
    this.fleetDirectory.lanesOf('alpaca').map((lane) => {
      const name = this.fleetDirectory.displayNameOf('alpaca', lane.clerk_id);
      const mode = verdictModeChip(this.liveVerdicts.stateFor(lane.clerk_id)).mode;
      return { clerkId: lane.clerk_id, label: `${name === null ? lane.clerk_id : laneDisplayNameText(name)} · ${mode}` };
    }),
  );

  protected readonly symbolUniverse = computed<readonly TickerOption[]>(() =>
    this.symbols().map((symbol) => ({ symbol, name: symbol })),
  );

  protected readonly symbolRange = computed<TickerRange>(() => ({
    symbol: this.query().symbol ?? '',
    from: '',
    to: '',
    resolution: 'daily',
  }));

  protected choose(param: HistoryFilterChange['param'], event: Event): void {
    const value = event.target instanceof HTMLSelectElement ? event.target.value : '';
    this.filterChanged.emit({ param, value: value === '' ? null : value });
  }

  protected pickSymbol(range: TickerRange): void {
    if (range.symbol && range.symbol !== this.query().symbol) {
      this.filterChanged.emit({ param: 'symbol', value: range.symbol });
    }
  }

  protected anySymbol(): void {
    this.filterChanged.emit({ param: 'symbol', value: null });
  }
}
