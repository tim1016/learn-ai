import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import type { TickerRange } from '../../../shared/ticker-range-picker';
import { IndicatorCatalogService } from '../../../shared/indicator-catalog/indicator-catalog.service';

import {
  DataLabChartComponent,
} from '../data-lab-chart/data-lab-chart.component';
import type {
  DataLabSessionChartSnapshot,
} from '../../../services/data-lab-session.service';
import { QualityModalComponent } from '../quality-modal/quality-modal.component';
import { IndicatorConfigModalComponent } from '../indicator-config-modal/indicator-config-modal.component';
import type { ActiveIndicatorEntry } from '../active-indicator-card/active-indicator-card.component';
import { ExploreScopeDrawerComponent } from './explore-scope-drawer/explore-scope-drawer.component';
import {
  ExploreIndicatorControlsComponent,
} from './explore-indicator-controls/explore-indicator-controls.component';
import type { IndicatorPickerAdd } from '../../../shared/indicator-picker/indicator-picker.component';
import type { ChartSeriesColorToken } from '../../../shared/trading-chart/chart-series-color-tokens';
import { ExploreHeadlinesComponent } from './explore-headlines/explore-headlines.component';

import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import {
  buildChartRequestBody,
  mapSessionToWire,
  utcDayEndMs,
  utcMsToIsoDate,
} from '../data-lab-request-mapper';

/**
 * Data Lab Explore (PRD §7.3).
 *
 * Chart-first. Fetch triggers are: the explicit **Refresh chart** button, a
 * shell-issued refresh request (preset click / Apply scope — the operator
 * changed the committed scope on purpose and the chart follows; 2026-09-13
 * product decision superseding PRD §14's refresh-only rule), this view's own
 * Edit-scope drawer Apply, the one-shot timeframe auto-correct re-fetch, and
 * auto-load on mount when a committed scope already exists. Scope/recipe
 * edits that do NOT commit mark the chart stale and the last good chart
 * stays visible behind an "Out of date" badge. The chart request body
 * always comes from the pure mapper ({@link buildChartRequestBody}) — one
 * path.
 */
@Component({
  selector: 'app-data-lab-explore',
  imports: [
    AssetIdentityComponent,
    DataLabChartComponent,
    QualityModalComponent,
    IndicatorConfigModalComponent,
    ExploreScopeDrawerComponent,
    ExploreIndicatorControlsComponent,
    ExploreHeadlinesComponent,
  ],
  templateUrl: './explore.component.html',
  styleUrls: ['./explore.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExploreComponent {
  readonly store = inject(DataLabWorkspaceStore);
  private readonly catalog = inject(IndicatorCatalogService);

  readonly chartComponent = viewChild<DataLabChartComponent>('chartComponent');

  /** Whether the chart has ever been requested — the canvas is hidden via
   *  @if until the first explicit refresh (PRD §16 empty state). */
  readonly chartRendered = signal(false);
  readonly chartRefreshing = signal(false);
  /** True between handing the chart component a fetch and that fetch
   * settling — distinguishes "not started" from "finished" when the
   * chart's `loading` signal is observed. */
  private readonly chartFetchIssued = signal(false);
  /** Whether the in-flight (or just-settled) fetch delivered data. The
   * settle effect clears the stale flag only when this is true — a failed
   * fetch keeps the old chart marked out of date. */
  private chartLoadedSinceFetch = false;
  /** Scope identity of the in-flight chart request (ticker + window +
   *  session; NOT timeframe — the auto-correct commits a timeframe by
   *  design). A timeframe rejection for a request whose scope no longer
   *  matches the committed one is stale advice and gets discarded. */
  private chartScopeKeyInFlight: string | null = null;
  readonly computeAllIndicators = signal(false);

  // ── Drawers ───────────────────────────────────────────────
  readonly scopeDrawerOpen = signal(false);
  readonly indicatorsDrawerOpen = signal(false);
  readonly chipsExpanded = signal(false);

  /** Two-way picker state for the Edit-scope drawer. String dates live
   *  ONLY here and convert to int64 ms at the apply boundary. */
  readonly scopeRange = signal<TickerRange>({
    symbol: '',
    from: '',
    to: '',
    resolution: 'minute',
    autoFetch: false,
  });

  constructor() {
    this.catalog.load();
    this.syncScopeRangeFromDraft();

    // A saved-session chart snapshot restored by the shell renders cached
    // bars with no HTTP call; the chart is marked stale meanwhile.
    effect(
      () => {
        const snapshot = this.store.restoredChartSnapshot() as DataLabSessionChartSnapshot | null;
        if (!snapshot) return;
        untracked(() => {
          this.chartRendered.set(true);
          this.store.clearRestoredChartSnapshot();
          setTimeout(() => {
            this.chartComponent()?.loadCachedData({
              bars: snapshot.bars,
              indicators: snapshot.indicators,
              quality: snapshot.quality,
              allowedTimeframes: snapshot.allowedTimeframes,
              estimatedBarsPerTimeframe: snapshot.estimatedBarsPerTimeframe,
              recommendedTimeframe: snapshot.recommendedTimeframe,
              visibleIndicatorIds: snapshot.visibleIndicatorIds,
              timeframe: snapshot.timeframe,
              barSources: snapshot.barSources ?? null,
            });
          });
        });
      },
      { allowSignalWrites: true },
    );

    // Settle an in-flight refresh from the chart component's own lifecycle:
    // `loading` flips false in the fetch's finally block, on success AND on
    // failure. Until then chartRefreshing stays true (the refresh button
    // stays disabled, so requests can't overlap). The stale flag clears
    // ONLY when the settled fetch actually loaded data — a failed fetch
    // must leave the old bars visibly out of date, not looking current
    // (that includes the timeframe auto-correct's exhausted-retry fallthrough,
    // where no replacement data ever arrives). The fetchIssued guard keeps
    // the freshly mounted chart (viewChild present, loading still false, no
    // fetch started) from settling the refresh early. A refresh request
    // that arrived mid-flight is consumed here, after the settle — that is
    // what makes rapid preset clicks coalesce to one fetch for the newest
    // scope instead of dropping the newest click.
    effect(
      () => {
        const chart = this.chartComponent();
        if (!chart) return;
        const loading = chart.loading();
        untracked(() => {
          if (!loading && this.chartFetchIssued() && this.chartRefreshing()) {
            this.chartFetchIssued.set(false);
            this.chartRefreshing.set(false);
            if (this.chartLoadedSinceFetch) {
              this.store.settleChartRequest();
            } else {
              this.store.markChartStale();
            }
            if (this.hasUnconsumedRefreshRequest()) {
              this.consumeRefreshRequest();
              this.refreshChart();
            }
          }
        });
      },
      { allowSignalWrites: true },
    );

    // Shell-issued refresh requests (preset click, Apply scope). Consumed
    // immediately when idle; left pending for the settle effect above when a
    // fetch is already in flight.
    effect(
      () => {
        const tick = this.store.chartRefreshRequests();
        if (tick === 0 || tick === this.lastConsumedRefreshTick) return;
        untracked(() => {
          if (!this.canRefresh()) return;
          this.consumeRefreshRequest();
          this.refreshChart();
        });
      },
      { allowSignalWrites: true },
    );

    // Auto-load on mount (2026-09-13): a committed scope present when Explore
    // mounts renders immediately instead of waiting for an explicit refresh.
    // A restored saved-session snapshot renders cached bars instead — that
    // path keeps its no-fetch, stale-badge semantics.
    if (this.hasCommittedScope() && !this.store.restoredChartSnapshot()) {
      this.refreshChart();
    }
  }

  // ── Shell refresh requests ─────────────────────────────────
  // Initialized from the store, not zero: a remount (operator returning
  // from Export/Validate) must not replay a tick a previous Explore
  // instance already consumed. A tick queued while Explore was unmounted
  // needs no replay either — the mount auto-load below fetches the newest
  // committed scope, which is what that tick was asking for.
  private lastConsumedRefreshTick = this.store.chartRefreshRequests();

  private hasUnconsumedRefreshRequest(): boolean {
    return this.store.chartRefreshRequests() > this.lastConsumedRefreshTick;
  }

  private consumeRefreshRequest(): void {
    this.lastConsumedRefreshTick = this.store.chartRefreshRequests();
  }

  // ── Committed scope → chart inputs ────────────────────────
  readonly chartTicker = computed(() => this.store.committedTicker());
  readonly chartFromDate = computed(() => {
    const w = this.store.committedWindow();
    return w ? utcMsToIsoDate(w.startMsUtc) : '';
  });
  readonly chartToDate = computed(() => {
    const w = this.store.committedWindow();
    return w ? utcMsToIsoDate(w.endMsUtc) : '';
  });
  readonly chartTimeframe = computed(() => {
    // Chart policy reads the COMMITTED scope — a fetch must never mix a
    // committed ticker/window with uncommitted draft bar policy.
    const d = this.store.committedScope();
    if (!d) return '1D';
    if (d.timespan === 'minute') return `${d.multiplier}m`;
    if (d.timespan === 'hour') return `${d.multiplier}h`;
    if (d.timespan === 'day') return d.multiplier === 1 ? '1D' : `${d.multiplier}D`;
    if (d.timespan === 'week') return `${d.multiplier}W`;
    if (d.timespan === 'month') return `${d.multiplier}M`;
    return '1D';
  });
  readonly hasCommittedScope = computed(
    () => !!this.store.committedTicker() && !!this.store.committedWindow(),
  );

  /** Wire-vocabulary session for the chart component's own POST body —
   *  Python only recognizes `rth`, while the store's default is `regular`. */
  readonly chartSession = computed(() =>
    mapSessionToWire(this.store.committedScope()?.session ?? this.store.draft().session),
  );

  readonly canRefresh = computed(() => this.hasCommittedScope() && !this.chartRefreshing());

  /** The one and only chart fetch trigger (FR-003). Bar policy comes from
   *  the committed scope, never the draft. */
  refreshChart(): void {
    const scope = this.store.committedScope();
    if (!scope) return;
    const body = buildChartRequestBody({
      ticker: scope.ticker,
      window: scope.window,
      timeframe: this.chartTimeframe(),
      session: scope.session,
      forwardFill: scope.forwardFill,
      adjusted: scope.adjusted,
      indicators: this.store.indicators(),
      computeAllIndicators: this.computeAllIndicators(),
    });
    // Records the request signature WITHOUT clearing the stale flag — the
    // settle effect above clears chartRefreshing and (on a loaded fetch)
    // the stale flag when the chart component's fetch finishes.
    this.store.recordChartRequest(JSON.stringify(body));
    this.chartLoadedSinceFetch = false;
    this.chartScopeKeyInFlight = this.committedScopeKey();
    this.chartRendered.set(true);
    this.chartRefreshing.set(true);
    // Defer so a first-time @if mount can populate the viewchild.
    setTimeout(() => {
      const chart = this.chartComponent();
      if (!chart) {
        // No fetch happened, so nothing settles: the stale flag set by the
        // commit stays, correctly describing the rendered-but-old chart.
        this.chartRefreshing.set(false);
        return;
      }
      this.chartFetchIssued.set(true);
      chart.fetchData();
    });
  }

  /** Identity of the currently committed chart scope for stale-response
   *  correlation — everything except bar policy, which the auto-correct
   *  path is allowed to change. */
  private committedScopeKey(): string {
    const s = this.store.committedScope();
    return s ? JSON.stringify([s.ticker, s.window.startMsUtc, s.window.endMsUtc, s.session]) : '';
  }

  onChartDataLoaded(event: DataLabSessionChartSnapshot): void {
    this.chartLoadedSinceFetch = true;
    this.store.setLatestChartSnapshot(event);
    this.lastQuality.set(event.quality);
    // A successful load disarms the auto-correct retry budget — the next
    // rejection starts a fresh correction cycle.
    this.timeframeAutoRetried.set(false);
  }

  /** One auto-correct re-fetch per successful load. The recommended timeframe
   *  is by construction allowed for the range, so a second rejection means
   *  something else is wrong and re-fetching in a loop would only burn
   *  requests — fall back to the stale badge and let the operator decide. */
  readonly timeframeAutoRetried = signal(false);

  onChartTimeframeRejected(event: { requested: string; recommended: string }): void {
    // A rejection is only advice about the request it answered. If the
    // committed scope moved on (preset or Apply scope landed mid-flight),
    // the queued refresh will fetch the new window — a recommendation
    // computed for the old range must not hijack the new scope's bar policy.
    if (this.chartScopeKeyInFlight !== this.committedScopeKey()) {
      this.store.markChartStale();
      return;
    }
    // Server-authored recovery choice (PRD §16): apply the recommendation to
    // the draft scope AND commit it — chart fetches read the committed scope,
    // so without the commit the next explicit refresh would resend the just
    // rejected timeframe forever.
    const parsed = /^(\d+)([mhDWM])$/.exec(event.recommended);
    if (!parsed) return;
    const multiplier = parseInt(parsed[1], 10);
    const timespan =
      parsed[2] === 'm' ? 'minute' :
      parsed[2] === 'h' ? 'hour' :
      parsed[2] === 'D' ? 'day' :
      parsed[2] === 'W' ? 'week' : 'month';
    this.store.patchDraft({ timespan, multiplier, timeframe: event.recommended });
    const committed = this.store.commitScope();
    if (committed.ok && !this.timeframeAutoRetried()) {
      this.timeframeAutoRetried.set(true);
      // Route through the store request (not a direct refreshChart): the
      // rejection fires while the failed fetch is still settling, and the
      // request-consumption machinery serializes this fetch after the settle.
      this.store.requestChartRefresh();
    } else {
      this.store.markChartStale();
    }
  }

  // ── Quality / provenance status row ───────────────────────
  readonly lastQuality = signal<DataLabSessionChartSnapshot['quality'] | null>(null);
  readonly qualityModalOpen = signal(false);

  readonly qualitySummary = computed(() => {
    const q = this.lastQuality();
    if (!q) return 'No chart yet — refresh to load quality evidence.';
    const issues = q.gaps_found + q.duplicates_removed + q.missing_sessions + q.synthetic_bars;
    return issues === 0
      ? 'Quality clean — no gaps, duplicates, or synthetic bars.'
      : `Quality issues found: ${issues} total (gaps ${q.gaps_found}, duplicates ${q.duplicates_removed}, missing sessions ${q.missing_sessions}, synthetic ${q.synthetic_bars}).`;
  });

  // ── Edit-scope drawer ─────────────────────────────────────
  private syncScopeRangeFromDraft(): void {
    const draft = this.store.draft();
    this.scopeRange.set({
      symbol: draft.ticker,
      from: utcMsToIsoDate(draft.window.startMsUtc),
      to: utcMsToIsoDate(draft.window.endMsUtc),
      resolution: draft.timespan === 'hour' ? 'hour' : draft.timespan === 'minute' ? 'minute' : 'daily',
      autoFetch: false,
    });
  }

  openScopeDrawer(): void {
    this.syncScopeRangeFromDraft();
    this.scopeDrawerOpen.set(true);
  }

  /** Apply the drawer's picker state: string dates → int64 ms at this
   *  boundary only, then commit through the store. A successful commit
   *  requests a chart refresh — this Apply must behave like the shell's
   *  identically labelled action, not strand a stale chart behind an
   *  updated scope. */
  applyScopeDrawer(): void {
    const range = this.scopeRange();
    const start = new Date(`${range.from}T00:00:00Z`).getTime();
    const end = utcDayEndMs(new Date(`${range.to}T00:00:00Z`).getTime());
    this.store.patchDraft({
      ticker: range.symbol,
      window: { startMsUtc: start, endMsUtc: end },
      timespan: range.resolution === 'daily' ? 'day' : range.resolution,
    });
    const committed = this.store.commitScope();
    this.scopeDrawerOpen.set(false);
    if (committed.ok) this.store.requestChartRefresh();
  }

  // ── Indicators drawer + chips ─────────────────────────────
  readonly catalogCategories = this.catalog.categories;
  readonly catalogLoading = this.catalog.loading;
  readonly catalogLoadFailed = this.catalog.failed;

  readonly activeIndicatorCount = computed(() => this.store.indicators().length);

  readonly chipEntries = computed(() =>
    this.store.indicators().map((i) => ({
      instance: i,
      label: `${i.canonicalKey}(${Object.values(i.params).join(', ')})`,
    })),
  );

  onPickerAdd(event: IndicatorPickerAdd): void {
    const info = this.catalog.get(event.name);
    const params = { ...event.params };
    if (info) {
      for (const p of info.configurable_params) {
        if (!(p.name in params)) params[p.name] = p.default;
      }
    }
    this.store.addIndicator(event.name, params);
  }

  removeIndicator(id: string): void {
    this.store.removeIndicator(id);
  }

  onColorTokenSelected(instanceId: string, token: ChartSeriesColorToken): void {
    this.store.setColorToken(instanceId, token);
  }

  // ── Indicator configure modal ─────────────────────────────
  readonly configuringInstanceId = signal<string | null>(null);

  readonly configuringEntry = computed<ActiveIndicatorEntry | null>(() => {
    const id = this.configuringInstanceId();
    if (!id) return null;
    const instance = this.store.indicators().find((i) => i.id === id);
    return instance ? { name: instance.canonicalKey, params: { ...instance.params } } : null;
  });

  readonly configuringParamConfigs = computed(() => {
    const entry = this.configuringEntry();
    return entry ? (this.catalog.get(entry.name)?.configurable_params ?? []) : [];
  });

  readonly activeIndicatorNames = computed(() =>
    Array.from(new Set(this.store.indicators().map((i) => i.canonicalKey))),
  );

  onModalParamChange(change: { name: string; value: number }): void {
    const id = this.configuringInstanceId();
    if (!id) return;
    const instance = this.store.indicators().find((i) => i.id === id);
    if (!instance) return;
    // Marks the chart stale via the store on success (PRD §9). The identity
    // is parameter-aware — follow the instance to its new id.
    const next = this.store.updateIndicator(id, { ...instance.params, [change.name]: change.value });
    if (next) this.configuringInstanceId.set(next);
  }

  onModalResetDefaults(): void {
    const id = this.configuringInstanceId();
    if (!id) return;
    const instance = this.store.indicators().find((i) => i.id === id);
    if (!instance) return;
    const defaults = this.catalog.defaultParams(instance.canonicalKey);
    const next = this.store.updateIndicator(id, defaults);
    if (next) this.configuringInstanceId.set(next);
  }

  onModalResetParam(paramName: string): void {
    const id = this.configuringInstanceId();
    if (!id) return;
    const instance = this.store.indicators().find((i) => i.id === id);
    if (!instance) return;
    const def = this.catalog.get(instance.canonicalKey)?.configurable_params.find(
      (p) => p.name === paramName,
    );
    if (!def) return;
    const next = this.store.updateIndicator(id, { ...instance.params, [paramName]: def.default });
    if (next) this.configuringInstanceId.set(next);
  }

  onModalAddRelated(name: string): void {
    this.store.addIndicator(name, this.catalog.defaultParams(name));
  }

  onModalRemoveRelated(name: string): void {
    const match = [...this.store.indicators()].reverse().find((i) => i.canonicalKey === name);
    if (match) this.store.removeIndicator(match.id);
  }

  onModalAddPreview(payload: { key: string; params: Record<string, number> }): void {
    this.store.addIndicator(payload.key, payload.params);
  }

  onModalVisibleChange(open: boolean): void {
    if (!open) this.configuringInstanceId.set(null);
  }
}
