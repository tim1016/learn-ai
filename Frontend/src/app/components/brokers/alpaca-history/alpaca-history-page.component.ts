import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { HISTORY_QUERY_PARAMS } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneDisplayNameText } from '../../../fleet/fleet-directory.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BotHistoryService, botHistoryQuery } from './bot-history.service';
import { HistoryFiltersComponent, type HistoryFilterChange } from './history-filters.component';
import { HistoryTableComponent } from './history-table.component';

/**
 * The History tab (#2574): every bot across every account — Live, Paper and
 * Shadow, each with its Dry Runs, cleared bots included — newest first, with
 * when it ran, how it ended, its trades and its money.
 *
 * It is the same list from every account's workspace, so it is lane-scoped
 * like Settings, and opening it pre-selects no filter: the filters live in the
 * URL (Home's Finished fold links here with `?status=cleared`). One read per
 * page, through the fleet coordinator, only when the owner opens, filters,
 * pages or refreshes -- never polled.
 *
 * An account the coordinator could not read is named above the list, never
 * silently left out.
 */
@Component({
  selector: 'app-alpaca-history-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HistoryFiltersComponent, HistoryTableComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './alpaca-history-page.component.html',
  styleUrl: './alpaca-history-page.component.scss',
})
export class AlpacaHistoryPageComponent {
  // The URL's filters, bound by the router (`withComponentInputBinding`).
  readonly account = input<string | undefined>(undefined);
  readonly status = input<string | undefined>(undefined);
  readonly world = input<string | undefined>(undefined);
  readonly symbol = input<string | undefined>(undefined);
  readonly page = input<string | undefined>(undefined);

  private readonly service = inject(BotHistoryService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  protected readonly query = computed(() => botHistoryQuery({
    account: this.account(),
    status: this.status(),
    world: this.world(),
    symbol: this.symbol(),
    page: this.page(),
  }));

  protected readonly history = resource({
    params: () => this.query(),
    loader: ({ params }) => this.service.read(params),
  });

  protected readonly result = computed(() => (this.history.hasValue() ? this.history.value() : null));

  /** The list itself could not be read (the coordinator did not answer). */
  protected readonly failed = computed(() => this.history.error() !== undefined);

  /** Each account (or Dry Run) that could not be read, named. */
  protected readonly gaps = computed(() =>
    (this.result()?.gaps ?? []).map((gap) => {
      const name = this.fleetDirectory.displayNameOf(gap.broker, gap.clerk_id);
      return {
        key: `${gap.clerk_id}/${gap.strategy_instance_id ?? ''}`,
        gap,
        accountName: name === null ? (gap.account_id ?? gap.clerk_id) : laneDisplayNameText(name),
      };
    }),
  );

  protected readonly pageCount = computed(() => {
    const result = this.result();
    return result === null ? 1 : Math.max(1, Math.ceil(result.total / result.page_size));
  });

  protected refresh(): void {
    this.history.reload();
  }

  /** A changed filter starts the list again from its first page. */
  protected filter(change: HistoryFilterChange): void {
    this.navigate({ [HISTORY_QUERY_PARAMS[change.param]]: change.value, [HISTORY_QUERY_PARAMS.page]: null });
  }

  protected goToPage(page: number): void {
    this.navigate({ [HISTORY_QUERY_PARAMS.page]: page === 1 ? null : String(page) });
  }

  private navigate(queryParams: Record<string, string | null>): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams, queryParamsHandling: 'merge' });
  }
}
