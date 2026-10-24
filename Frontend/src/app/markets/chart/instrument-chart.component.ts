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

import { IndicatorCatalogService } from '../../shared/indicator-catalog/indicator-catalog.service';
import type { IndicatorPickerAdd } from '../../shared/indicator-picker/indicator-picker.component';
import type { ChartSeriesColorToken } from '../../shared/trading-chart/chart-series-color-tokens';
import type { DataLabSessionChartSnapshot } from '../../services/data-lab-session.service';
import type { ActiveIndicatorEntry } from '../../components/data-lab/active-indicator-card/active-indicator-card.component';
import { IndicatorConfigModalComponent } from '../../components/data-lab/indicator-config-modal/indicator-config-modal.component';
import { mapSessionToWire, utcMsToIsoDate } from '../../components/data-lab/data-lab-request-mapper';
import { barTimeframeFor, parseChartTimeframe } from '../../components/data-lab/data-lab-scope';
import { DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import { InstrumentContextService } from '../instrument-context.service';
import { ChartHeadlinesComponent } from './chart-headlines/chart-headlines.component';
import { chartRequestIdentity, chartTimeframeOf } from './chart-request';
import { ChartIndicatorControlsComponent } from './chart-indicator-controls/chart-indicator-controls.component';
import { DataLabChartComponent } from './data-lab-chart/data-lab-chart.component';
import { QualityModalComponent } from './quality-modal/quality-modal.component';

/**
 * An instrument's Chart tab: candles, indicators and headlines for the
 * committed context (PRD #3490 R09, R10).
 *
 * The chart follows the committed context and loads when it changes. There is
 * no Apply and no Refresh: the shared pickers commit, a newly committed,
 * distinct request loads once, and the same request arriving again does
 * nothing. A request is identified by the whole body it posts (instrument,
 * window, bar policy, session, indicators), so no part of it can change
 * without the chart loading again, and the chart component drops the answer
 * to any request a newer one replaced, so a late answer never fills a context
 * it was not asked for.
 *
 * Opening the chart keeps ADR 0049 A6: the chart endpoint ensures the
 * completed sessions first and says in its answer where the bars came from.
 * Coming back to this tab with the context unchanged shows the held answer
 * and asks nothing.
 */
@Component({
  selector: 'app-instrument-chart',
  imports: [
    DataLabChartComponent,
    QualityModalComponent,
    IndicatorConfigModalComponent,
    ChartIndicatorControlsComponent,
    ChartHeadlinesComponent,
  ],
  templateUrl: './instrument-chart.component.html',
  styleUrl: './instrument-chart.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InstrumentChartComponent {
  readonly store = inject(DataLabWorkspaceStore);
  private readonly context = inject(InstrumentContextService);
  private readonly catalog = inject(IndicatorCatalogService);

  readonly chartComponent = viewChild<DataLabChartComponent>('chartComponent');

  /** The request the chart on screen was last asked for; a repeat of it asks nothing. */
  private requested: string | null = null;

  /** Whether a load for the latest request has not settled yet. */
  readonly loading = signal(false);

  // ── Drawers ───────────────────────────────────────────────
  readonly indicatorsDrawerOpen = signal(false);
  readonly chipsExpanded = signal(false);

  constructor() {
    this.catalog.load();

    // The one trigger: the committed scope and the recipe, as the request body
    // they make. An unchanged body is the same request. It waits for the chart
    // to be on screen, so the request is sent to something that can answer it.
    effect(() => {
      const scope = this.store.committedScope();
      const indicators = this.store.indicators();
      if (scope === null || this.chartComponent() === undefined) return;
      const identity = chartRequestIdentity(scope, indicators);
      untracked(() => this.request(identity));
    });
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
    return d === null ? '1D' : chartTimeframeOf(d);
  });
  readonly hasCommittedScope = computed(
    () => !!this.store.committedTicker() && !!this.store.committedWindow(),
  );

  /** Wire-vocabulary session for the chart component's own POST body —
   *  Python only recognizes `rth`, while the store's default is `regular`. */
  readonly chartSession = computed(() =>
    mapSessionToWire(this.store.committedScope()?.session ?? this.store.draft().session),
  );

  // ── Loading ───────────────────────────────────────────────

  /**
   * One request for `identity`. The same identity again is a repeated
   * emission and does nothing. A held answer to exactly this request (this
   * tab came back with the context unchanged) is shown without a fetch.
   */
  private request(identity: string): void {
    if (identity === this.requested) return;
    this.requested = identity;
    this.lastQuality.set(null);
    this.timeframeAutoCorrected.set(false);
    // Asked before the request is recorded: recording replaces the answer's identity.
    const held = this.heldAnswer(identity);
    if (held === null) {
      this.store.recordChartRequest(identity);
      // At once, not when the request is sent: what is on screen answered another context.
      this.chartComponent()?.clearResult();
    }
    // Deferred so the chart's inputs carry this context when it reads them, and
    // so a second request in the same task replaces this one before it is sent.
    setTimeout(() => this.show(identity, held));
  }

  private show(identity: string, held: DataLabSessionChartSnapshot | null): void {
    // A newer request replaced this one before it ran: it is that one's to send.
    if (identity !== this.requested) return;
    const chart = this.chartComponent();
    if (chart === undefined) return;
    if (held !== null) {
      this.loading.set(false);
      chart.loadCachedData(held);
      this.lastQuality.set(held.quality);
      return;
    }
    this.loading.set(true);
    chart.fetchData();
  }

  /** The answer this store holds to exactly `identity`, if it holds one that settled. */
  private heldAnswer(identity: string): DataLabSessionChartSnapshot | null {
    const snapshot = this.store.latestChartSnapshot() as DataLabSessionChartSnapshot | null;
    return snapshot !== null && !this.store.chartStale() && this.store.lastChartRequestSignature() === identity
      ? snapshot
      : null;
  }

  onChartDataLoaded(event: DataLabSessionChartSnapshot): void {
    this.store.setLatestChartSnapshot(event);
    this.lastQuality.set(event.quality);
    // A successful load disarms the auto-correct retry budget — the next
    // rejection starts a fresh correction cycle.
    this.timeframeAutoCorrected.set(false);
  }

  /** The latest request answered: only a request that loaded data clears the stale mark. */
  onChartSettled(outcome: { readonly loaded: boolean }): void {
    this.loading.set(false);
    if (outcome.loaded) this.store.settleChartRequest();
  }

  /** One auto-correct per successful load. The recommended timeframe is by
   *  construction allowed for the range, so a second rejection means
   *  something else is wrong and committing again would only loop — the
   *  chart's own message stands and the person decides. */
  readonly timeframeAutoCorrected = signal(false);

  onChartTimeframeRejected(event: { requested: string; recommended: string }): void {
    // Only the answer to the latest request reaches here: the chart drops the
    // rejection of one a newer request replaced, so this advice is for the
    // context on screen.
    const parsed = parseChartTimeframe(event.recommended);
    const option = parsed === null ? undefined : barTimeframeFor(parsed.timespan, parsed.multiplier);
    if (option === undefined || this.timeframeAutoCorrected()) return;
    this.timeframeAutoCorrected.set(true);
    this.context.commitTimeframe(option.value);
  }

  // ── Quality / provenance status row ───────────────────────
  readonly lastQuality = signal<DataLabSessionChartSnapshot['quality'] | null>(null);
  readonly qualityModalOpen = signal(false);

  readonly qualitySummary = computed(() => {
    const q = this.lastQuality();
    if (!q) return this.loading() ? 'Loading quality evidence with the chart.' : 'No chart loaded, so no quality evidence.';
    const issues = q.gaps_found + q.duplicates_removed + q.missing_sessions + q.synthetic_bars;
    return issues === 0
      ? 'Quality clean — no gaps, duplicates, or synthetic bars.'
      : `Quality issues found: ${issues} total (gaps ${q.gaps_found}, duplicates ${q.duplicates_removed}, missing sessions ${q.missing_sessions}, synthetic ${q.synthetic_bars}).`;
  });

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
    // The identity is parameter-aware — follow the instance to its new id.
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
