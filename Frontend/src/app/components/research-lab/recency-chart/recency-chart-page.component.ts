import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { MessageService } from "primeng/api";
import { filter, firstValueFrom, map } from "rxjs";

import {
  RECENCY_HERO_QUERY,
  RECENCY_TRADES_QUERY,
  SOFT_DELETE_RECENCY_RUN_MUTATION,
  type RecencyHeroQueryResultItem,
  type RecencyHeroQueryResult,
  type RecencyTradeQueryResultItem,
  type RecencyTradesQueryResult,
} from "../../../graphql/recency-chart.query";
import { RecencySwimlaneComponent } from "./recency-swimlane/recency-swimlane.component";
import type { RecencySwimlaneTrade } from "./recency-swimlane/recency-swimlane-layout";
import { RecencyLaunchConfigComponent } from "./recency-launch-config/recency-launch-config.component";
import { RecencyTradeFocusComponent } from "./recency-trade-focus/recency-trade-focus.component";
import { computeDisplayMode, computeDisplayWindow } from "./recency-display-mode";
import { filterToHeroAndExpanded, groupKey, type HeroKey } from "./recency-hero-fold";

const SIX_MONTHS_MS = 1000 * 60 * 60 * 24 * 30 * 6;

/**
 * Recency Chart page: fetches the accumulated trade projection + the
 * Python-authored hero selection, applies symbol/strategy view toggles
 * (D19), the hero/fold combo classification (D5), and the display
 * mode/window (D18), then renders the swimlane virtualized to that
 * window with a click-to-pin focus panel (D9). Soft-delete (D17) is an
 * optimistic local filter: the mutation is the source of truth, but a
 * successful delete is hidden immediately rather than waiting on a
 * refetch of the six-month trades query.
 */
@Component({
  selector: "app-recency-chart-page",
  standalone: true,
  imports: [RecencySwimlaneComponent, RecencyLaunchConfigComponent, RecencyTradeFocusComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-chart-page.component.html",
  styleUrls: ["./recency-chart-page.component.scss"],
})
export class RecencyChartPageComponent {
  private readonly apollo = inject(Apollo);
  private readonly messageService = inject(MessageService);

  private readonly fetchWindowEndMs = Date.now();
  private readonly fetchWindowStartMs = this.fetchWindowEndMs - SIX_MONTHS_MS;

  private readonly tradesResource = rxResource<RecencyTradeQueryResultItem[], { from: number; to: number }>({
    params: () => ({ from: this.fetchWindowStartMs, to: this.fetchWindowEndMs }),
    stream: ({ params }) =>
      this.apollo
        .watchQuery<RecencyTradesQueryResult>({
          query: RECENCY_TRADES_QUERY,
          variables: { fromMs: params.from, toMs: params.to, symbols: null, strategies: null },
          fetchPolicy: "network-only",
        })
        .valueChanges.pipe(
          filter((result) => !result.loading),
          map((result): RecencyTradeQueryResultItem[] => {
            // Apollo types partial/cache data as DeepPartialObject<T>; the
            // GraphQL schema guarantees these fields (see
            // recency-chart.query.ts), so this narrows back to the actual
            // contract — same idiom as run-report.component.ts.
            return (result.data?.recencyTrades as RecencyTradeQueryResultItem[] | undefined) ?? [];
          }),
        ),
  });

  private readonly heroResource = rxResource<RecencyHeroQueryResultItem[], object>({
    params: () => ({}),
    stream: () =>
      this.apollo
        .watchQuery<RecencyHeroQueryResult>({
          query: RECENCY_HERO_QUERY,
          variables: { symbols: null, strategies: null },
          fetchPolicy: "network-only",
        })
        .valueChanges.pipe(
          filter((result) => !result.loading),
          map((result): RecencyHeroQueryResultItem[] => {
            return (result.data?.recencyHero as RecencyHeroQueryResultItem[] | undefined) ?? [];
          }),
        ),
  });

  private readonly deletedRunIds = signal<ReadonlySet<number>>(new Set());

  readonly trades = computed<RecencySwimlaneTrade[]>(() =>
    (this.tradesResource.value() ?? []).filter((t) => !this.deletedRunIds().has(t.recencyRunId)),
  );
  readonly heroes = computed<HeroKey[]>(() => this.heroResource.value() ?? []);
  readonly isLoading = computed(() => this.tradesResource.isLoading());
  readonly hasTrades = computed(() => this.trades().length > 0);

  private readonly hiddenSymbols = signal<ReadonlySet<string>>(new Set());
  private readonly hiddenStrategies = signal<ReadonlySet<string>>(new Set());
  private readonly expandedGroups = signal<ReadonlySet<string>>(new Set());

  readonly distinctSymbols = computed<string[]>(() =>
    Array.from(new Set(this.trades().map((t) => t.symbol))).sort(),
  );
  readonly distinctStrategies = computed<string[]>(() =>
    Array.from(new Set(this.trades().map((t) => t.strategyKey))).sort(),
  );

  isSymbolVisible(symbol: string): boolean {
    return !this.hiddenSymbols().has(symbol);
  }

  isStrategyVisible(strategyKey: string): boolean {
    return !this.hiddenStrategies().has(strategyKey);
  }

  toggleSymbol(symbol: string): void {
    this.hiddenSymbols.update((hidden) => toggleMembership(hidden, symbol));
  }

  toggleStrategy(strategyKey: string): void {
    this.hiddenStrategies.update((hidden) => toggleMembership(hidden, strategyKey));
  }

  toggleGroupExpanded(symbol: string, strategyKey: string): void {
    this.expandedGroups.update((expanded) => toggleMembership(expanded, groupKey(symbol, strategyKey)));
  }

  isGroupExpanded(symbol: string, strategyKey: string): boolean {
    return this.expandedGroups().has(groupKey(symbol, strategyKey));
  }

  /** One row per distinct (symbol, strategy) pair — the fold/unfold affordance's scope. */
  readonly comboGroups = computed<{ symbol: string; strategyKey: string }[]>(() => {
    const seen = new Map<string, { symbol: string; strategyKey: string }>();
    for (const t of this.toggleFilteredTrades()) {
      seen.set(groupKey(t.symbol, t.strategyKey), { symbol: t.symbol, strategyKey: t.strategyKey });
    }
    return Array.from(seen.values());
  });

  readonly visibleSymbols = computed<string[]>(() => this.distinctSymbols().filter((s) => this.isSymbolVisible(s)));

  private readonly toggleFilteredTrades = computed<RecencySwimlaneTrade[]>(() => {
    const hiddenSymbols = this.hiddenSymbols();
    const hiddenStrategies = this.hiddenStrategies();
    if (hiddenSymbols.size === 0 && hiddenStrategies.size === 0) return this.trades();
    return this.trades().filter((t) => !hiddenSymbols.has(t.symbol) && !hiddenStrategies.has(t.strategyKey));
  });

  /** Hero-combo-by-default view (D5): folded combos only when their group is expanded. */
  readonly displayedTrades = computed<RecencySwimlaneTrade[]>(() =>
    filterToHeroAndExpanded(this.toggleFilteredTrades(), this.heroes(), this.expandedGroups()),
  );

  readonly displayMode = computed(() => computeDisplayMode(this.visibleSymbols()));

  readonly displayWindow = computed(() => {
    const earliestEntryMs = this.displayedTrades().reduce<number | null>(
      (min, t) => (min === null || t.entryMs < min ? t.entryMs : min),
      null,
    );
    return computeDisplayWindow(this.displayMode(), Date.now(), { earliestEntryMs });
  });

  readonly selectedTrade = signal<RecencySwimlaneTrade | null>(null);

  onTradeSelected(trade: RecencySwimlaneTrade): void {
    this.selectedTrade.set(trade);
  }

  async onDeleteRequested(recencyRunId: number): Promise<void> {
    try {
      await firstValueFrom(
        this.apollo.mutate({
          mutation: SOFT_DELETE_RECENCY_RUN_MUTATION,
          variables: { runId: recencyRunId },
        }),
      );
    } catch {
      this.messageService.add({
        severity: "error",
        summary: "Delete failed",
        detail: "Could not delete this recency run. Try again.",
      });
      return;
    }

    this.deletedRunIds.update((ids) => new Set(ids).add(recencyRunId));
    if (this.selectedTrade()?.recencyRunId === recencyRunId) {
      this.selectedTrade.set(null);
    }
  }
}

function toggleMembership(set: ReadonlySet<string>, value: string): ReadonlySet<string> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}
