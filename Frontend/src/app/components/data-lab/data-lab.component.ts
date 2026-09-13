import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { NavigationEnd } from '@angular/router';
import { filter } from 'rxjs';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { RunDockComponent } from '../../shared/run-dock/run-dock.component';
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
} from '../../shared/run-dock/run-dock-source';
import { RunSessionService } from '../../services/run-session.service';
import {
  DataLabSessionService,
  DataLabSessionSummary,
} from '../../services/data-lab-session.service';
import { resolveLegacyDataLabUrl } from './data-lab-ingress';
import { dataLabIndicatorInstanceId } from './data-lab-workspace-store';
import { utcMsToIsoDate } from './data-lab-request-mapper';
import {
  DataLabWorkspaceStore,
  DEFAULT_DATA_LAB_INDICATORS,
  DATA_LAB_WORKSPACE_SCHEMA_VERSION,
} from './data-lab-workspace-store';

/* Pure helpers kept exported for their existing spec pins
 * (data-lab.auto-bar-timeframe.spec.ts, data-lab.parse-chart-timeframe.spec.ts,
 * data-lab.auto-chunk-readout.spec.ts). They predate the workspace redesign
 * and stay here verbatim so those suites keep passing unchanged. */

/**
 * Auto bar-timeframe heuristic — picks a Polygon bar resolution from the
 * calendar-day span of the range so a fetch returns in a reasonable time
 * without asking the user to think in "bars per day". Locked by product
 * on 2026-04-24:
 *
 *   ≤ 5 days       → 1-minute
 *   5–30 days      → 5-minute
 *   30–120 days    → 15-minute
 *   120–365 days   → 1-hour
 *   > 365 days     → 1-hour (Polygon Starter's cap is 2 years anyway)
 */
export function pickAutoBarTimeframe(spanDays: number): string {
  if (spanDays <= 5) return '1m';
  if (spanDays <= 30) return '5m';
  if (spanDays <= 120) return '15m';
  return '1h';
}

export type PolygonTimespan = 'minute' | 'hour' | 'day' | 'week' | 'month';

/**
 * Parse the chart component's timeframe vocabulary ("1m", "5m", "15m",
 * "1h", "4h", "1D", "1W", "1M") into Polygon's (timespan, multiplier)
 * pair. Returns null for unrecognized inputs so callers can ignore
 * vocabulary the dataset endpoint can't honor.
 */
export function parseChartTimeframe(
  timeframe: string,
): { timespan: PolygonTimespan; multiplier: number } | null {
  const match = timeframe.match(/^(\d+)([mhDWM])$/);
  if (!match) return null;
  const multiplier = parseInt(match[1], 10);
  const unit = match[2];
  const timespan: PolygonTimespan | null =
    unit === 'm' ? 'minute' :
    unit === 'h' ? 'hour' :
    unit === 'D' ? 'day' :
    unit === 'W' ? 'week' :
    unit === 'M' ? 'month' :
    null;
  if (!timespan) return null;
  return { timespan, multiplier };
}

/**
 * Layman-friendly readout for the Auto Chunk control. Pure helper so the
 * exact wording can be regression-tested without spinning up a component.
 */
export function formatChunkReadout(
  bars: number,
  autoChunk: boolean,
  polygonLimit: number,
): string {
  const limit = Math.max(1, polygonLimit);
  const chunks = Math.max(1, Math.ceil(bars / limit));
  if (!autoChunk) {
    return `Manual: ${polygonLimit.toLocaleString()} bars per request.`;
  }
  if (chunks === 1) {
    return `1 request · ~${bars.toLocaleString()} bars · single response.`;
  }
  return `Plan runs ${chunks} requests · ~${bars.toLocaleString()} bars · paced if your plan caps requests/min.`;
}

/** Bar-timeframe presets shared by the shell's compact scope bar and the
 *  Export form. User-facing vocabulary mapped onto (timespan, multiplier). */
export interface BarTimeframeOption {
  value: string;
  label: string;
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
}

export const BAR_TIMEFRAMES: readonly BarTimeframeOption[] = [
  { value: '1m', label: '1 min', timespan: 'minute', multiplier: 1 },
  { value: '5m', label: '5 min', timespan: 'minute', multiplier: 5 },
  { value: '15m', label: '15 min', timespan: 'minute', multiplier: 15 },
  { value: '30m', label: '30 min', timespan: 'minute', multiplier: 30 },
  { value: '1h', label: '1 hour', timespan: 'hour', multiplier: 1 },
  { value: '4h', label: '4 hours', timespan: 'hour', multiplier: 4 },
  { value: '1d', label: '1 day', timespan: 'day', multiplier: 1 },
];

/** Parse a strict YYYY-MM-DD string to int64 ms UTC at UTC midnight. The
 *  shared `parseYmd` builds LOCAL-midnight Dates, which would make stored
 *  MsUtc window boundaries shift with the viewer's timezone — every
 *  YMD→ms boundary in this shell therefore goes through `Date.UTC`. Pure;
 *  returns null for malformed or impossible calendar dates. */
export function parseYmdMsUtc(s: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split('-').map(Number);
  const ms = Date.UTC(y, m - 1, d);
  const check = new Date(ms);
  if (
    check.getUTCFullYear() !== y ||
    check.getUTCMonth() !== m - 1 ||
    check.getUTCDate() !== d
  ) {
    return null;
  }
  return ms;
}

/**
 * Data Lab route shell (PRD §7.2).
 *
 * Owns the compact scope bar, the three route tabs, the saved-setups
 * drawer, and one RunDockComponent. All workspace state lives in the
 * route-provided {@link DataLabWorkspaceStore}; child routes inject the
 * same instance. The shell never fetches the chart — URL-supplied state
 * populates the workspace but never auto-fetches (PRD §14).
 */
@Component({
  selector: 'app-data-lab',
  imports: [
    RouterLink,
    RouterLinkActive,
    RouterOutlet,
    PageHeaderComponent,
    AssetIdentityComponent,
    RunDockComponent,
  ],
  templateUrl: './data-lab.component.html',
  styleUrls: ['./data-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [
    // Wire data-lab's RunSessionService into the shared dock's source slot.
    // Unique storage key so data-lab and engine-lab don't share dock state.
    { provide: RUN_DOCK_SOURCE, useExisting: RunSessionService },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: 'run-dock-expanded:data-lab' },
  ],
})
export class DataLabComponent {
  private readonly router = inject(Router);
  private readonly sessionService = inject(DataLabSessionService);
  readonly store = inject(DataLabWorkspaceStore);

  readonly barTimeframes = BAR_TIMEFRAMES;

  // ── Legacy URL ingress (PRD §14) ───────────────────────────
  /** Warnings surfaced by the ingress adapter, shown in a dismissible banner. */
  readonly ingressWarnings = signal<string[]>([]);

  constructor() {
    // Seed the product's thirteen-indicator default recipe into a fresh
    // workspace (PRD §4 / §7.3). Seeding never fetches anything.
    if (this.store.indicators().length === 0) {
      for (const entry of DEFAULT_DATA_LAB_INDICATORS) {
        this.store.addIndicator(entry.canonicalKey, entry.params);
      }
      this.store.markChartStale();
    }
    // Legacy ingress runs once for the URL present at construction and is
    // re-checked on each completed navigation — the guard inside makes the
    // re-check a no-op on canonical URLs.
    this.runLegacyIngress();
    this.router.events
      .pipe(
        filter((e) => e instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe(() => this.runLegacyIngress());
    this.refreshSessionList();
  }

  /** True once the ingress has applied a legacy URL's scope params. The
   *  redirected canonical URL KEEPS the surviving scope params (ticker/
   *  from/to) by design, so the shape check alone can't distinguish
   *  "not yet processed" from "already processed" — without this guard the
   *  NavigationEnd re-check would re-navigate to the same URL forever. */
  private ingressApplied = false;

  /** If the current URL carries legacy Data Lab query state, resolve it and
   *  replaceState-navigate to the canonical child route. URL state may
   *  populate the workspace but NEVER auto-fetches (PRD §14).
   *
   *  A child-route redirect rewrites `/data-lab?mode=build` to
   *  `/data-lab/explore?mode=build` before this shell activates, so the
   *  trigger keys on the legacy `mode` marker, the legacy parent path, or a
   *  canonical child URL carrying legacy scope keys — that last shape is
   *  what a `/data-quality?ticker=…` bookmark looks like AFTER Angular's
   *  redirect preserves its query string onto `/data-lab/validate`.
   *
   *  Public so the ingress spec can drive it against a stubbed
   *  `router.url`. */
  runLegacyIngress(): void {
    if (this.ingressApplied) return;
    const url = this.router.url;
    const parsed = new URL(url, 'https://data-lab.invalid');
    const query = parsed.searchParams;
    const path = parsed.pathname.replace(/\/+$/, '');
    const hasLegacyScopeKeys = ['ticker', 'from', 'to', 'trading-session', 'sessionId'].some(
      (k) => query.get(k) !== null,
    );
    const isLegacyShape =
      query.get('mode') !== null ||
      path === '/data-lab' ||
      path === '/data-quality' ||
      (path.startsWith('/data-lab/') && hasLegacyScopeKeys);
    if (!isLegacyShape) return;
    this.ingressApplied = true;

    const resolution = resolveLegacyDataLabUrl(url);
    this.applyLegacyScope(resolution.params);
    // Set unconditionally: a valid legacy URL must clear warnings left by an
    // earlier invalid one, not layer over them.
    this.ingressWarnings.set(resolution.warnings);
    this.router.navigate([resolution.route], {
      queryParams: resolution.params,
      replaceUrl: true,
    });
  }

  /** Apply surviving legacy scope params to the workspace draft and commit.
   *  Dates convert string→int64 ms here — the only string-date boundary. */
  private applyLegacyScope(params: Record<string, string>): void {
    const patch: Parameters<DataLabWorkspaceStore['patchDraft']>[0] = {};
    if (params['ticker']) patch.ticker = params['ticker'];
    const from = params['from'] ? parseYmdMsUtc(params['from']) : null;
    const to = params['to'] ? parseYmdMsUtc(params['to']) : null;
    if (from !== null || to !== null) {
      const draft = this.store.draft();
      patch.window = {
        startMsUtc: from !== null ? from : draft.window.startMsUtc,
        endMsUtc: to !== null ? to : draft.window.endMsUtc,
      };
    }
    if (params['trading-session']) {
      patch.session = params['trading-session'] === 'extended' ? 'extended' : 'regular';
    }
    if (Object.keys(patch).length > 0) {
      this.store.patchDraft(patch);
      this.store.commitScope();
    }
  }

  dismissIngressWarnings(): void {
    this.ingressWarnings.set([]);
  }

  // ── Compact scope bar ─────────────────────────────────────
  readonly draftTicker = computed(() => this.store.draft().ticker);
  readonly draftFromIso = computed(() => utcMsToIsoDate(this.store.draft().window.startMsUtc));
  readonly draftToIso = computed(() => utcMsToIsoDate(this.store.draft().window.endMsUtc));
  readonly draftTimeframeValue = computed(() => {
    const d = this.store.draft();
    const match = BAR_TIMEFRAMES.find(
      (b) => b.timespan === d.timespan && b.multiplier === d.multiplier,
    );
    return match ? match.value : 'custom';
  });
  readonly scopeCommitError = signal<string | null>(null);

  /** Committed scope readout for the compact summary. */
  readonly committedSummary = computed(() => {
    const ticker = this.store.committedTicker();
    const window = this.store.committedWindow();
    if (!ticker || !window) return 'No committed scope yet';
    const tf = this.draftTimeframeValue() === 'custom'
      ? `${this.store.draft().multiplier}${this.store.draft().timespan}`
      : this.draftTimeframeValue();
    return `${ticker} · ${utcMsToIsoDate(window.startMsUtc)} → ${utcMsToIsoDate(window.endMsUtc)} · ${tf} · ${this.store.draft().session}`;
  });

  onTickerInput(event: Event): void {
    this.store.patchDraft({ ticker: (event.target as HTMLInputElement).value });
  }

  onFromInput(event: Event): void {
    const parsedMs = parseYmdMsUtc((event.target as HTMLInputElement).value);
    if (parsedMs === null) return;
    const draft = this.store.draft();
    this.store.patchDraft({
      window: { ...draft.window, startMsUtc: parsedMs },
    });
  }

  onToInput(event: Event): void {
    const parsedMs = parseYmdMsUtc((event.target as HTMLInputElement).value);
    if (parsedMs === null) return;
    const draft = this.store.draft();
    this.store.patchDraft({
      window: { ...draft.window, endMsUtc: parsedMs },
    });
  }

  onTimeframeChange(event: Event): void {
    const entry = BAR_TIMEFRAMES.find(
      (b) => b.value === (event.target as HTMLSelectElement).value,
    );
    if (!entry) return;
    this.store.patchDraft({
      timespan: entry.timespan,
      multiplier: entry.multiplier,
      timeframe: entry.timespan === 'day' && entry.multiplier === 1 ? 'day' : entry.value,
    });
  }

  onSessionChange(event: Event): void {
    this.store.patchDraft({
      session: (event.target as HTMLSelectElement).value === 'extended' ? 'extended' : 'regular',
    });
  }

  /** Commit the draft scope. Edits before this never trigger fetches. */
  applyScope(): void {
    const result = this.store.commitScope();
    this.scopeCommitError.set(result.ok ? null : result.error);
  }

  // ── Saved setups drawer ───────────────────────────────────
  readonly sessionsDrawerOpen = signal(false);
  readonly savedSessions = signal<DataLabSessionSummary[]>([]);
  readonly activeSessionId = signal<string | null>(null);
  readonly savingSession = signal(false);
  readonly sessionName = signal('');
  readonly renamingSessionId = signal<string | null>(null);
  readonly renameValue = signal('');

  async refreshSessionList(): Promise<void> {
    this.savedSessions.set(await this.sessionService.listSessions());
  }

  toggleSessionsDrawer(): void {
    this.sessionsDrawerOpen.update((v) => !v);
    if (this.sessionsDrawerOpen()) this.refreshSessionList();
  }

  /** Saving requires a committed scope — building the config from the
   *  uncommitted draft could persist a setup the chart never followed. */
  readonly canSaveSession = computed(() => !!this.store.committedScope());

  private sessionConfigFromStore(): {
    ticker: string;
    fromDate: string;
    toDate: string;
    session: 'rth' | 'extended';
    forwardFill: boolean;
    adjusted: boolean;
    entries: { name: string; params: Record<string, number> }[];
  } | null {
    const scope = this.store.committedScope();
    if (!scope) return null;
    return {
      ticker: scope.ticker,
      fromDate: utcMsToIsoDate(scope.window.startMsUtc),
      toDate: utcMsToIsoDate(scope.window.endMsUtc),
      session: scope.session === 'extended' ? 'extended' : 'rth',
      forwardFill: scope.forwardFill,
      adjusted: scope.adjusted,
      entries: this.store.indicators().map((i) => ({
        name: i.canonicalKey,
        params: { ...i.params },
      })),
    };
  }

  async saveSession(): Promise<void> {
    const config = this.sessionConfigFromStore();
    if (!config) return;
    this.savingSession.set(true);
    try {
      // Persist the latest chart snapshot ONLY when it still matches the
      // committed configuration: `chartStale` is set by any scope/recipe
      // edit after the last settled fetch, so a stale chart means the
      // snapshot depicts a config the saved setup no longer has. Clear it
      // rather than persisting an unrelated render.
      const snapshot = (this.store.chartStale() ? null : this.store.latestChartSnapshot()) as never;
      const activeId = this.activeSessionId();
      if (activeId) {
        await this.sessionService.updateSession(
          activeId,
          config,
          snapshot,
          this.sessionName() || config.ticker,
        );
      } else {
        const newId = await this.sessionService.saveSession(
          config,
          snapshot,
          this.sessionName() || undefined,
        );
        if (newId) {
          this.activeSessionId.set(newId);
          this.store.setSavedSession({ id: newId, schemaVersion: DATA_LAB_WORKSPACE_SCHEMA_VERSION });
        }
      }
      await this.refreshSessionList();
    } finally {
      this.savingSession.set(false);
    }
  }

  async saveAsNewSession(): Promise<void> {
    this.activeSessionId.set(null);
    this.store.setSavedSession(null);
    await this.saveSession();
  }

  async loadSession(id: string): Promise<void> {
    const session = await this.sessionService.getSession(id);
    if (!session) return;
    this.store.patchDraft({
      ticker: session.config.ticker,
      window: {
        startMsUtc: parseYmdMsUtc(session.config.fromDate) ?? this.store.draft().window.startMsUtc,
        endMsUtc: parseYmdMsUtc(session.config.toDate) ?? this.store.draft().window.endMsUtc,
      },
      session: session.config.session,
      forwardFill: session.config.forwardFill,
      adjusted: session.config.adjusted,
    });
    this.store.commitScope();
    this.store.setIndicators(
      session.config.entries.map((entry) => ({
        id: dataLabIndicatorInstanceId(entry.name, entry.params),
        canonicalKey: entry.name,
        params: entry.params,
      })),
    );
    this.activeSessionId.set(session.id);
    this.sessionName.set(session.name);
    this.store.setSavedSession({ id: session.id, schemaVersion: DATA_LAB_WORKSPACE_SCHEMA_VERSION });
    // Hand the snapshot (if any) to Explore — restoring it renders cached
    // bars with no HTTP call; the chart is marked stale meanwhile (PRD §16).
    if (session.chartSnapshot) {
      this.store.setRestoredChartSnapshot(session.chartSnapshot);
    }
    this.sessionsDrawerOpen.set(false);
  }

  async deleteSession(id: string, event: Event): Promise<void> {
    event.stopPropagation();
    await this.sessionService.deleteSession(id);
    if (this.activeSessionId() === id) {
      this.activeSessionId.set(null);
      this.sessionName.set('');
      this.store.setSavedSession(null);
    }
    await this.refreshSessionList();
  }

  startRenaming(session: DataLabSessionSummary, event: Event): void {
    event.stopPropagation();
    this.renamingSessionId.set(session.id);
    this.renameValue.set(session.name);
  }

  onRenameInput(event: Event): void {
    this.renameValue.set((event.target as HTMLInputElement).value);
  }

  async confirmRename(id: string): Promise<void> {
    if (this.renameValue().trim()) {
      await this.sessionService.renameSession(id, this.renameValue().trim());
      await this.refreshSessionList();
    }
    this.renamingSessionId.set(null);
  }

  cancelRename(): void {
    this.renamingSessionId.set(null);
  }

  onSessionNameInput(event: Event): void {
    this.sessionName.set((event.target as HTMLInputElement).value);
  }

  detachSession(): void {
    this.activeSessionId.set(null);
    this.sessionName.set('');
    this.store.setSavedSession(null);
  }

  formatDate(timestamp: string): string {
    return new Date(timestamp).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }
}
