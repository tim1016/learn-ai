import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';

import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneConfirmedAccount } from '../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService, verdictModeChip } from '../../../services/alpaca-live-verdict.service';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import type { TickerOption } from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import {
  BOT_HISTORY_STATUSES,
  BOT_HISTORY_WORLDS,
  historyAccountName,
  type BotHistoryFilter,
  type BotHistoryQuery,
} from './bot-history.service';

/** One filter the owner changed, and its new value (`null` clears it). */
export interface HistoryFilterChange {
  readonly param: BotHistoryFilter;
  readonly value: string | null;
}

/**
 * History's filters (#2574): account, status, world and symbol, and — when a
 * bot's own page opened the list — that one bot. Each account is named with
 * its mode worded, as everywhere. The symbol filter is the shared symbol
 * picker over the symbols the history itself holds: a closed list the page
 * owns outright (ADR 0066), so a pick is never gated on the lake.
 */
@Component({
  selector: 'app-history-filters',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SymbolPickerComponent],
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
      const name = historyAccountName(this.fleetDirectory, 'alpaca', lane.clerk_id, laneConfirmedAccount(lane));
      const mode = verdictModeChip(this.liveVerdicts.stateFor(lane.clerk_id)).mode;
      return { clerkId: lane.clerk_id, label: `${name} · ${mode}` };
    }),
  );

  protected readonly symbolUniverse = computed<readonly TickerOption[]>(() =>
    this.symbols().map((symbol) => ({ symbol, name: symbol })),
  );

  protected choose(param: BotHistoryFilter, value: string): void {
    this.filterChanged.emit({ param, value: value === '' ? null : value });
  }
}
