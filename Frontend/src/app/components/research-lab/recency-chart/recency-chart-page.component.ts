import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { MessageService } from "primeng/api";
import { filter, firstValueFrom, map } from "rxjs";

import {
  RECENCY_HERO_QUERY,
  RECENCY_TRADES_QUERY,
  SOFT_DELETE_RECENCY_RUN_MUTATION,
  type SoftDeleteRecencyRunMutationResult,
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

// Matches the launch config's MAX_MONTHS accumulation cap (D11: no
// product cap on generation) — fetching the full supported range and
// letting the swimlane's own virtualization narrow what's rendered
// avoids single-symbol mode (full history) hitting a fetch-level
// truncation that display-window filtering alone can't undo.
const MAX_FETCH_WINDOW_MS = 1000 * 60 * 60 * 24 * 30 * 24;

/**
 * Recency Chart page: fetches the accumulated trade projection + the
 * Python-authored hero selection, applies symbol/strategy view toggles
 * (D19), the hero/fold combo classification (D5), and the display
 * mode/window (D18), then renders the swimlane virtualized to that
 * window with a click-to-pin focus panel (D9). Soft-delete (D17) is an
 * membership-aware refetch after mutation. A trade may belong to several
 * runs, so deleting one run must never hide evidence that still has a live
 * membership.
 */
@Component({
  selector: "app-recency-chart-page",
  imports: [RecencySwimlaneComponent, RecencyLaunchConfigComponent, RecencyTradeFocusComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-chart-page.component.html",
  styleUrls: ["./recency-chart-page.component.scss"],
})
export class RecencyChartPageComponent {
  private readonly apollo = inject(Apollo);
  private readonly messageService = inject(MessageService);

  private readonly fetchWindowEndMs = Date.now();
  private readonly fetchWindowStartMs = this.fetchWindowEndMs - MAX_FETCH_WINDOW_MS;

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

  readonly trades = computed<RecencySwimlaneTrade[]>(() => this.tradesResource.value() ?? []);
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
  readonly visibleStrategies = computed<string[]>(() =>
    this.distinctStrategies().filter((strategy) => this.isStrategyVisible(strategy)),
  );

  private readonly toggleFilteredTrades = computed<RecencySwimlaneTrade[]>(() => {
    const hiddenSymbols = this.hiddenSymbols();
    const hiddenStrategies = this.hiddenStrategies();
    if (hiddenSymbols.size === 0 && hiddenStrategies.size === 0) return this.trades();
    return this.trades().filter((t) => !hiddenSymbols.has(t.symbol) && !hiddenStrategies.has(t.strategyKey));
  });

  readonly displayMode = computed(() => computeDisplayMode(this.visibleSymbols()));

  readonly displayWindow = computed(() => {
    const earliestEntryMs = this.toggleFilteredTrades().reduce<number | null>(
      (min, t) => (min === null || t.entryMs < min ? t.entryMs : min),
      null,
    );
    return computeDisplayWindow(this.displayMode(), Date.now(), { earliestEntryMs });
  });

  private readonly heroResource = rxResource<
    RecencyHeroQueryResultItem[],
    { from: number; to: number; symbols: string[]; strategies: string[] }
  >({
    params: () => ({
      from: this.displayWindow().start,
      to: this.displayWindow().end,
      symbols: this.visibleSymbols(),
      strategies: this.visibleStrategies(),
    }),
    stream: ({ params }) =>
      this.apollo
        .watchQuery<RecencyHeroQueryResult>({
          query: RECENCY_HERO_QUERY,
          variables: {
            fromMs: params.from,
            toMs: params.to,
            symbols: params.symbols.length > 0 ? params.symbols : null,
            strategies: params.strategies.length > 0 ? params.strategies : null,
          },
          fetchPolicy: "network-only",
        })
        .valueChanges.pipe(
          filter((result) => !result.loading),
          map((result): RecencyHeroQueryResultItem[] => {
            return (result.data?.recencyHero as RecencyHeroQueryResultItem[] | undefined) ?? [];
          }),
        ),
  });
  readonly heroes = computed<HeroKey[]>(() => this.heroResource.value() ?? []);

  /** Hero-combo-by-default view (D5): folded combos only when their group is expanded. */
  readonly displayedTrades = computed<RecencySwimlaneTrade[]>(() =>
    filterToHeroAndExpanded(this.toggleFilteredTrades(), this.heroes(), this.expandedGroups()),
  );

  readonly selectedTrade = signal<RecencySwimlaneTrade | null>(null);

  onTradeSelected(trade: RecencySwimlaneTrade): void {
    this.selectedTrade.set(trade);
  }

  async onDeleteRequested(recencyRunId: number): Promise<void> {
    try {
      const result = await firstValueFrom(
        this.apollo.mutate<SoftDeleteRecencyRunMutationResult>({
          mutation: SOFT_DELETE_RECENCY_RUN_MUTATION,
          variables: { runId: recencyRunId },
        }),
      );
      if (result.data?.softDeleteRecencyRun.code) {
        throw new Error(result.data.softDeleteRecencyRun.message);
      }
    } catch {
      this.messageService.add({
        severity: "error",
        summary: "Delete failed",
        detail: "Could not delete this recency run. Try again.",
      });
      return;
    }

    this.selectedTrade.set(null);
    this.tradesResource.reload();
    this.heroResource.reload();
  }

  onLaunchCompleted(): void {
    this.tradesResource.reload();
    this.heroResource.reload();
  }
}

function toggleMembership(set: ReadonlySet<string>, value: string): ReadonlySet<string> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}
