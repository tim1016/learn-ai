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
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import {
  IndicatorPickerAdd,
  IndicatorPickerComponent,
} from '../../../shared/indicator-picker/indicator-picker.component';
import {
  ChartSeriesColorPickerComponent,
} from '../../../shared/trading-chart/chart-series-color-picker.component';
import type { ChartSeriesColorToken } from '../../../shared/trading-chart/chart-series-color-tokens';
import {
  TickerRangePickerComponent,
  type TickerRange,
} from '../../../shared/ticker-range-picker';
import { IndicatorCatalogService } from '../../../shared/indicator-catalog/indicator-catalog.service';
import { NewsService } from '../../../services/news.service';
import type { NewsArticle } from '../../../services/news.service';

import {
  DataLabChartComponent,
} from '../data-lab-chart/data-lab-chart.component';
import type {
  DataLabSessionChartSnapshot,
} from '../../../services/data-lab-session.service';
import { QualityModalComponent } from '../quality-modal/quality-modal.component';
import { IndicatorConfigModalComponent } from '../indicator-config-modal/indicator-config-modal.component';
import type { ActiveIndicatorEntry } from '../active-indicator-card/active-indicator-card.component';

import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import { buildChartRequestBody, utcMsToIsoDate } from '../data-lab-request-mapper';
import { windowToNewsQuery, NEWS_MAX_HEADLINES } from './news-window-adapter';

/** Local news-section state beyond the store's coarse newsState. */
type NewsSectionState = 'idle' | 'loading' | 'ready' | 'stale' | 'error' | 'rate-limited';

/**
 * Data Lab Explore (PRD §7.3).
 *
 * Chart-first. The ONLY trigger for chart fetches is the explicit
 * **Refresh chart** button — scope/recipe edits mark the chart stale and
 * the last good chart stays visible behind an "Out of date" badge. The
 * chart request body always comes from the pure mapper
 * ({@link buildChartRequestBody}) — one path.
 */
@Component({
  selector: 'app-data-lab-explore',
  imports: [
    RouterLink,
    AssetIdentityComponent,
    TimestampDisplayComponent,
    IndicatorPickerComponent,
    ChartSeriesColorPickerComponent,
    TickerRangePickerComponent,
    DataLabChartComponent,
    QualityModalComponent,
    IndicatorConfigModalComponent,
  ],
  templateUrl: './explore.component.html',
  styleUrls: ['./explore.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExploreComponent {
  readonly store = inject(DataLabWorkspaceStore);
  private readonly catalog = inject(IndicatorCatalogService);
  private readonly newsService = inject(NewsService);

  readonly chartComponent = viewChild<DataLabChartComponent>('chartComponent');

  /** Whether the chart has ever been requested — the canvas is hidden via
   *  @if until the first explicit refresh (PRD §16 empty state). */
  readonly chartRendered = signal(false);
  readonly chartRefreshing = signal(false);
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

    // A committed-window change invalidates already-loaded headlines —
    // mark the section stale instead of silently refetching (PRD §11).
    effect(
      () => {
        this.store.committedWindow();
        untracked(() => {
          if (this.newsArticles().length > 0 && this.newsState() === 'ready') {
            this.newsState.set('stale');
            this.store.setNewsState('stale');
          }
        });
      },
      { allowSignalWrites: true },
    );

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
    const d = this.store.draft();
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

  readonly canRefresh = computed(() => this.hasCommittedScope() && !this.chartRefreshing());

  /** The one and only chart fetch trigger (FR-003). */
  refreshChart(): void {
    const window = this.store.committedWindow();
    const ticker = this.store.committedTicker();
    if (!window || !ticker) return;
    const draft = this.store.draft();
    const body = buildChartRequestBody({
      ticker,
      window,
      timeframe: this.chartTimeframe(),
      session: draft.session,
      forwardFill: draft.forwardFill,
      adjusted: draft.adjusted,
      indicators: this.store.indicators(),
      computeAllIndicators: this.computeAllIndicators(),
    });
    this.store.recordChartRequest(JSON.stringify(body));
    this.chartRendered.set(true);
    this.chartRefreshing.set(true);
    // Defer so a first-time @if mount can populate the viewchild.
    setTimeout(() => {
      const chart = this.chartComponent();
      if (!chart) {
        this.chartRefreshing.set(false);
        return;
      }
      chart.fetchData();
      this.chartRefreshing.set(false);
    });
  }

  onChartDataLoaded(event: DataLabSessionChartSnapshot): void {
    this.store.setLatestChartSnapshot(event);
    this.lastQuality.set(event.quality);
  }

  onChartTimeframeRejected(event: { requested: string; recommended: string }): void {
    // Server-authored recovery choice (PRD §16): apply the recommendation to
    // the draft scope; the user still refreshes explicitly.
    const parsed = /^(\d+)([mhDWM])$/.exec(event.recommended);
    if (!parsed) return;
    const multiplier = parseInt(parsed[1], 10);
    const timespan =
      parsed[2] === 'm' ? 'minute' :
      parsed[2] === 'h' ? 'hour' :
      parsed[2] === 'D' ? 'day' :
      parsed[2] === 'W' ? 'week' : 'month';
    this.store.patchDraft({ timespan, multiplier });
    this.store.markChartStale();
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
   *  boundary only, then commit through the store. */
  applyScopeDrawer(): void {
    const range = this.scopeRange();
    const start = new Date(`${range.from}T00:00:00Z`).getTime();
    const end = new Date(`${range.to}T00:00:00Z`).getTime();
    this.store.patchDraft({
      ticker: range.symbol,
      window: { startMsUtc: start, endMsUtc: end },
      timespan: range.resolution === 'daily' ? 'day' : range.resolution,
    });
    this.store.commitScope();
    this.scopeDrawerOpen.set(false);
  }

  // ── Indicators drawer + chips ─────────────────────────────
  readonly catalogCategories = this.catalog.categories;
  readonly catalogLoading = this.catalog.loading;

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

  // ── Collapsed headlines section (PRD §11) ─────────────────
  readonly newsExpanded = signal(false);
  readonly newsState = signal<NewsSectionState>('idle');
  readonly newsArticles = signal<readonly NewsArticle[]>([]);
  readonly newsError = signal('');

  readonly newsHeadlines = computed(() => this.newsArticles().slice(0, NEWS_MAX_HEADLINES));

  toggleNews(): void {
    this.newsExpanded.update((v) => !v);
    // Lazy: fetch only when expanded (PRD §11 / FR-010).
    if (this.newsExpanded() && this.newsState() === 'idle') this.fetchNews();
  }

  refreshNews(): void {
    this.fetchNews();
  }

  private async fetchNews(): Promise<void> {
    const ticker = this.store.committedTicker();
    const window = this.store.committedWindow();
    if (!ticker || !window) {
      this.newsState.set('idle');
      return;
    }
    this.newsState.set('loading');
    this.store.setNewsState('loading');
    this.newsError.set('');
    try {
      const result = await firstValueFrom(
        this.newsService.news(windowToNewsQuery(ticker, window)),
      );
      this.newsArticles.set(result.articles);
      this.newsState.set('ready');
      this.store.setNewsState('ready');
    } catch (e: unknown) {
      const status = (e as { status?: number }).status;
      if (status === 429) {
        this.newsState.set('rate-limited');
        this.store.setNewsState('rate-limited');
        this.newsError.set('News vendor rate limit reached. Try again shortly.');
      } else {
        this.newsState.set('error');
        this.store.setNewsState('error');
        this.newsError.set(e instanceof Error ? e.message : String(e));
      }
    }
  }
}
