import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  viewChild,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import type { BotHistoryUrl } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import {
  BotHistoryService,
  botHistoryQuery,
  historyAccountName,
  type FleetBotHistoryPage,
} from './bot-history.service';
import { HistoryFiltersComponent, type HistoryFilterChange } from './history-filters.component';
import { HistoryTableComponent } from './history-table.component';

/**
 * The History tab (#2574): every bot across every account — Live, Paper and
 * Shadow, each with its Dry Runs, cleared bots included — newest first, with
 * when it ran, how it ended, its trades and its money.
 *
 * It is the same list from every account's workspace, so it is lane-scoped
 * like Settings, and opening it pre-selects no filter: the filters live in the
 * URL (`BotHistoryUrl`: Home's Finished fold links here with `?status=cleared`,
 * a bot's own page with `?account=…&bot=…`). One read per page, through the
 * fleet coordinator, only when the owner opens, filters, pages or refreshes
 * -- never polled.
 *
 * While the next page is read the last one stays on screen, marked busy, so
 * the list, the pager and the owner's focus stay put; after paging, focus
 * moves to the page count above the list.
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
  // `BotHistoryUrl`'s values, bound by the router (`withComponentInputBinding`).
  readonly account = input<string>();
  readonly status = input<string>();
  readonly world = input<string>();
  readonly symbol = input<string>();
  readonly bot = input<string>();
  readonly page = input<string>();

  private readonly service = inject(BotHistoryService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly injector = inject(Injector);
  private readonly listStatus = viewChild<ElementRef<HTMLElement>>('listStatus');

  protected readonly query = computed(() => botHistoryQuery(
    {
      account: this.account(),
      status: this.status(),
      world: this.world(),
      symbol: this.symbol(),
      bot: this.bot(),
      page: this.page(),
    },
    // Until the directory is read every account is taken at its word; then
    // one no lane serves is treated as unset.
    (clerkId) => this.fleetDirectory.value() === undefined || this.fleetDirectory.lane('alpaca', clerkId) !== undefined,
  ));

  protected readonly history = resource({
    params: () => this.query(),
    loader: ({ params }) => this.service.read(params),
  });

  /** The page on screen: the newest read, kept while the next one loads. */
  protected readonly shown = linkedSignal<FleetBotHistoryPage | undefined, FleetBotHistoryPage | null>({
    source: () => (this.history.hasValue() ? this.history.value() : undefined),
    computation: (page, previous) => page ?? previous?.value ?? null,
  });

  /** The list itself could not be read (the coordinator did not answer). */
  protected readonly failed = computed(() => this.history.error() !== undefined);

  /** Each account (or Dry Run, or bot) that could not be read, named. */
  protected readonly gaps = computed(() =>
    (this.shown()?.gaps ?? []).map((gap) => ({
      key: `${gap.clerk_id}/${gap.strategy_instance_id ?? ''}`,
      gap,
      accountName: historyAccountName(this.fleetDirectory, gap.broker, gap.clerk_id, gap.account_id),
    })),
  );

  protected readonly pageCount = computed(() => {
    const shown = this.shown();
    return shown === null ? 1 : Math.max(1, Math.ceil(shown.total / shown.page_size));
  });

  /** The list's one line of status, announced as it changes. */
  protected readonly statusText = computed(() => {
    const shown = this.shown();
    if (this.history.isLoading() && (shown === null || this.failed())) return "Reading every account's bots…";
    if (shown === null || this.failed()) return '';
    if (shown.rows.length === 0) return 'No bots match these filters.';
    return `Page ${shown.page} of ${this.pageCount()} · ${shown.total} bots`;
  });

  protected refresh(): void {
    this.history.reload();
  }

  /** A changed filter starts the list again from its first page. */
  protected filter(change: HistoryFilterChange): void {
    this.navigate({ [change.param]: change.value, page: null });
  }

  protected goToPage(page: number): void {
    this.navigate({ page: page === 1 ? null : String(page) });
    afterNextRender({ write: () => this.listStatus()?.nativeElement.focus() }, { injector: this.injector });
  }

  private navigate(queryParams: { readonly [K in keyof BotHistoryUrl]?: string | null }): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams, queryParamsHandling: 'merge' });
  }
}
