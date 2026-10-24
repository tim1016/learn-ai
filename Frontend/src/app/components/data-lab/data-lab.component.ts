import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  signal,
} from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { NavigationEnd } from '@angular/router';
import { filter } from 'rxjs';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { DateRangePickerComponent, ProviderBoundsService, type QuickRangeKey, type QuickRangeOffer } from '../../shared/date-picker';
import { OptionArchiveCardComponent } from '../../shared/option-archive/option-archive-card.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { TimestampDisplayPipe } from '../../shared/timestamp';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';
import { DEFAULT_ADJUSTMENT_MODE } from '../../shared/ticker-catalog';
import type { PriceAdjustmentMode } from '../../shared/data-lake';
import { RunDockComponent } from '../../shared/run-dock/run-dock.component';
import { menuLabelFor } from '../../shell/app-menu';
import { CurrentUrlService } from '../../shell/current-url.service';
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
} from '../../shared/run-dock/run-dock-source';
import { RunSessionService } from '../../services/run-session.service';
import {
  DataLabSessionService,
  DataLabSessionSummary,
} from '../../services/data-lab-session.service';
import {
  ChartRangePreset,
  DataLabRangePresetsService,
} from '../../services/data-lab-range-presets.service';
import { instrumentCommands, legacyInstrumentTarget, MARKETS_ROUTE } from '../../markets/instrument-address';
import { resolveLegacyDataLabUrl } from './data-lab-ingress';
import {
  BAR_TIMEFRAMES,
  barTimeframeFor,
  isoOrEmpty,
  parseChartTimeframe,
  parseYmdMsUtc,
  PRESET_DEFAULT_TIMEFRAMES,
  presetQuickRanges,
  PRESET_LABELS,
} from './data-lab-scope';
import { dataLabIndicatorInstanceId, seedDefaultIndicators } from './data-lab-workspace-store';
import { utcDayEndMs, utcMsToIsoDate } from './data-lab-request-mapper';
import {
  DataLabDraftScope,
  DataLabWorkspaceStore,
  DATA_LAB_WORKSPACE_SCHEMA_VERSION,
} from './data-lab-workspace-store';

/**
 * Data Lab route shell.
 *
 * Owns the compact scope bar (with the calendar-resolved quick-range chips),
 * the route tabs (Build dataset, Validate, Returns study), the saved-setups
 * drawer, and one RunDockComponent. All workspace state lives in the
 * route-provided {@link DataLabWorkspaceStore}; child routes inject the same
 * instance. The chart left for the instrument place (`/markets/:symbol/chart`);
 * an address that asked for it is sent there.
 */
@Component({
  selector: 'app-data-lab',
  imports: [
    OptionArchiveCardComponent,
    RouterLink,
    RouterLinkActive,
    RouterOutlet,
    PageHeaderComponent,
    AssetIdentityComponent,
    RunDockComponent,
    SymbolPickerComponent,
    DateRangePickerComponent,
    TimestampDisplayPipe,
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
  private readonly rangePresetsService = inject(DataLabRangePresetsService);
  readonly store = inject(DataLabWorkspaceStore);

  /** The page is titled by the menu entry that leads to it (PRD #3490 R01):
   *  Build dataset, Return Distribution or Validate. */
  private readonly menuNode = inject(CurrentUrlService).menuNode;
  protected readonly pageTitle = computed(
    () => this.menuNode()?.item.title ?? menuLabelFor('/data-lab/returns'),
  );

  readonly barTimeframes = BAR_TIMEFRAMES;

  // ── Legacy URL ingress ─────────────────────────────────────
  /** Warnings surfaced by the ingress adapter, shown in a dismissible banner. */
  readonly ingressWarnings = signal<string[]>([]);

  constructor() {
    // Seed the product's thirteen-indicator default recipe into a fresh
    // workspace. Seeding never fetches anything.
    seedDefaultIndicators(this.store);
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
    void this.loadRangePresets();
  }

  /** True once the ingress has applied a legacy URL's scope params. The
   *  redirected canonical URL KEEPS the surviving scope params (ticker/
   *  from/to) by design, so the shape check alone can't distinguish
   *  "not yet processed" from "already processed" — without this guard the
   *  NavigationEnd re-check would re-navigate to the same URL forever. */
  private ingressApplied = false;

  /** If the current URL carries legacy Data Lab query state, resolve it,
   *  populate and commit the workspace from it, and name what it ignored in
   *  the migration banner.
   *
   *  The legacy addresses (`/data-lab`, `/data-quality`) redirect straight to
   *  the child their `mode` names, query intact (`redirectLegacyDataLab`), so
   *  this normally runs on a URL that is already canonical and navigates
   *  nowhere. Only a canonical child whose `mode` names another child is moved,
   *  and one whose `mode` names the chart goes to the instrument it names.
   *  The trigger keys on the legacy `mode` marker, the legacy parent path, or
   *  a canonical child URL carrying legacy scope keys — that last shape is
   *  what a `/data-quality?ticker=…` bookmark looks like once redirected.
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
    if (resolution.route === 'instrument') {
      const target = legacyInstrumentTarget(resolution.params, 'chart');
      void this.router.navigate(target.commands, { queryParams: target.queryParams, replaceUrl: true });
      return;
    }
    this.applyLegacyScope(resolution.params);
    // Set unconditionally: a valid legacy URL must clear warnings left by an
    // earlier invalid one, not layer over them.
    this.ingressWarnings.set(resolution.warnings);
    if (resolution.route !== path) {
      this.router.navigate([resolution.route], {
        queryParams: resolution.params,
        replaceUrl: true,
      });
    }
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
        endMsUtc: to !== null ? utcDayEndMs(to) : draft.window.endMsUtc,
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

  /** Where the chart lives now: the committed instrument's Chart tab with the committed window, else the Markets list. */
  protected readonly chartLink = computed(() => {
    const ticker = this.store.committedTicker();
    const window = this.store.committedWindow();
    if (!ticker) return { commands: [MARKETS_ROUTE], queryParams: {} };
    const scope = this.store.committedScope();
    if (window === null || scope === null) return { commands: instrumentCommands(ticker, 'chart'), queryParams: {} };
    const timeframe = barTimeframeFor(scope.timespan, scope.multiplier);
    return {
      commands: instrumentCommands(ticker, 'chart'),
      queryParams: {
        from: utcMsToIsoDate(window.startMsUtc),
        to: utcMsToIsoDate(window.endMsUtc),
        session: scope.session === 'extended' ? 'extended' : 'regular',
        ...(timeframe === undefined ? {} : { timeframe: timeframe.value }),
      },
    };
  });

  /**
   * The tree this page's chart reads — the picker's coverage badge and gate
   * must follow it, so a session restored with `adjusted: false` admits
   * symbols against the raw tree the run will actually query.
   */
  readonly pickerAdjustmentMode = computed<PriceAdjustmentMode>(() =>
    this.store.draft().adjusted ? DEFAULT_ADJUSTMENT_MODE : 'raw',
  );
  /**
   * The draft's window on its own. What follows it starts over when the
   * window is replaced, and not when another scope field changes: read
   * straight off the draft, a session or ticker change would wipe a date
   * the operator had just typed.
   */
  private readonly draftWindow = computed(() => this.store.draft().window);
  /**
   * The dates the picker holds for the draft window; empty while the draft
   * has no window, which is ms 0, not 1970. The picker empties a date it
   * cannot use, and a replaced window (a saved setup, a preset) puts both
   * dates back in the fields, even one that is the same day as before.
   */
  readonly draftFromIso = linkedSignal({ source: this.draftWindow, computation: (held) => isoOrEmpty(held.startMsUtc) });
  readonly draftToIso = linkedSignal({ source: this.draftWindow, computation: (held) => isoOrEmpty(held.endMsUtc) });
  /**
   * Data Lab charts the session still forming and takes any calendar day, so
   * its dates run from the provider's oldest day with no last day, and need
   * not be sessions.
   */
  protected readonly dateBounds = inject(ProviderBoundsService).openEnded;
  /**
   * A date field holds no usable date; the picker says why beside it. It
   * starts over whenever the draft window is replaced (a saved setup, a
   * preset), because the fields then show the new window's dates.
   */
  protected readonly datesUnusable = linkedSignal({ source: this.draftWindow, computation: () => false });
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

  onTickerPicked(ticker: string): void {
    this.store.patchDraft({ ticker });
  }

  /** The picker's dates into the draft window: string dates become int64 ms here and nowhere else. */
  onDatesChanged(dates: { readonly from: string; readonly to: string }): void {
    this.datesUnusable.set(dates.from === '' || dates.to === '');
    const held = this.store.draft().window;
    if (dates.from === isoOrEmpty(held.startMsUtc) && dates.to === isoOrEmpty(held.endMsUtc)) return;
    const start = parseYmdMsUtc(dates.from);
    const end = parseYmdMsUtc(dates.to);
    if (start === null || end === null) return;
    this.store.patchDraft({ window: { startMsUtc: start, endMsUtc: utcDayEndMs(end) } });
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
    // A tap can arrive in the task that refused a date, before the button is drawn disabled.
    if (this.datesUnusable()) return;
    const result = this.store.commitScope();
    this.scopeCommitError.set(result.ok ? null : result.error);
  }

  // ── Calendar-resolved quick ranges ─────────────────────────
  readonly rangePresets = signal<readonly ChartRangePreset[]>([]);

  private async loadRangePresets(): Promise<void> {
    // A failed or empty load leaves the quick ranges hidden and the typed
    // dates as the path of record. Logged, not silent — a backend
    // or network regression must be distinguishable from presets not
    // existing, and an unhandled rejection must not leak from a mount-time
    // nicety.
    try {
      this.rangePresets.set(await this.rangePresetsService.presets('rth'));
    } catch (err) {
      console.warn('[DataLab] range presets failed to load; quick ranges hidden', err);
      this.rangePresets.set([]);
    }
  }

  /** The server's presets as the shared picker's quick ranges: Python's resolved dates under the shared labels. */
  protected readonly presetOffers = computed<readonly QuickRangeOffer[]>(() => presetQuickRanges(this.rangePresets()));

  /** A quick range was clicked: apply the server preset behind its label. */
  onPresetPicked(key: QuickRangeKey): void {
    const preset = this.rangePresets().find((candidate) => PRESET_LABELS[candidate.key] === key);
    if (preset !== undefined) void this.applyRangePreset(preset);
  }

  /** Apply a server-resolved preset: window verbatim from Python, commit.
   *  The list is re-resolved first — the service cache is
   *  five-minute TTL, so a Data Lab left open across an ET trading-date
   *  boundary would otherwise keep applying the previous day's windows.
   *  Short windows also get a sensible intraday default timeframe —
   *  a 1D range on a daily bar chart is one candle, which is technically
   *  allowed and practically useless. A display default, not math: the chart
   *  endpoint's own recommendation machinery stays authoritative. */
  async applyRangePreset(preset: ChartRangePreset): Promise<void> {
    let target = preset;
    try {
      const fresh = await this.rangePresetsService.presets('rth');
      target = fresh.find(p => p.key === preset.key) ?? preset;
      this.rangePresets.set(fresh);
    } catch (err) {
      // Offline or transient failure: the clicked preset's resolved window
      // still applies — a stale-but-correct range beats no action.
      console.warn(`[DataLab] re-resolving preset ${preset.key} failed; applying cached window`, err);
    }
    const patch: Partial<DataLabDraftScope> = {
      window: { startMsUtc: target.start_ms_utc, endMsUtc: target.end_ms_utc },
    };
    const suggested = PRESET_DEFAULT_TIMEFRAMES[target.key];
    if (suggested) {
      const parsed = parseChartTimeframe(suggested);
      if (parsed) {
        patch.timespan = parsed.timespan;
        patch.multiplier = parsed.multiplier;
        patch.timeframe = suggested;
      }
    }
    this.store.patchDraft(patch);
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
  /** Why the last saved-setups call failed; cleared when the next user action starts. */
  readonly sessionError = signal<string | null>(null);

  /** Runs one saved-setups call. A failure lands in the drawer instead of
   *  leaving as an unhandled rejection from a click handler. */
  private async attemptSessionCall<T>(
    call: () => Promise<T>,
  ): Promise<{ readonly ok: true; readonly value: T } | { readonly ok: false }> {
    try {
      return { ok: true, value: await call() };
    } catch (err: unknown) {
      this.sessionError.set(err instanceof Error ? err.message : 'The saved setups request failed.');
      return { ok: false };
    }
  }

  async refreshSessionList(): Promise<void> {
    const listed = await this.attemptSessionCall(() => this.sessionService.listSessions());
    if (listed.ok) this.savedSessions.set(listed.value);
  }

  toggleSessionsDrawer(): void {
    this.sessionsDrawerOpen.update((v) => !v);
    if (this.sessionsDrawerOpen()) {
      this.sessionError.set(null);
      void this.refreshSessionList();
    }
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
    this.sessionError.set(null);
    try {
      // Persist the latest chart snapshot ONLY when it still matches the
      // committed configuration: `chartStale` is set by any scope/recipe
      // edit after the last settled fetch, so a stale chart means the
      // snapshot depicts a config the saved setup no longer has. Clear it
      // rather than persisting an unrelated render.
      const snapshot = (this.store.chartStale() ? null : this.store.latestChartSnapshot()) as never;
      const activeId = this.activeSessionId();
      await this.attemptSessionCall(async () => {
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
          this.activeSessionId.set(newId);
          this.store.setSavedSession({ id: newId, schemaVersion: DATA_LAB_WORKSPACE_SCHEMA_VERSION });
        }
      });
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
    this.sessionError.set(null);
    const loaded = await this.attemptSessionCall(() => this.sessionService.getSession(id));
    if (!loaded.ok) return;
    const session = loaded.value;
    if (!session) {
      this.sessionError.set('Session not found');
      await this.refreshSessionList();
      return;
    }
    const fromMs = parseYmdMsUtc(session.config.fromDate);
    const toMs = parseYmdMsUtc(session.config.toDate);
    this.store.patchDraft({
      ticker: session.config.ticker,
      window: {
        startMsUtc: fromMs ?? this.store.draft().window.startMsUtc,
        endMsUtc: toMs !== null ? utcDayEndMs(toMs) : this.store.draft().window.endMsUtc,
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
    // Stage the snapshot (if any) for the chart to render from cache with no
    // HTTP call; the chart is marked stale meanwhile.
    if (session.chartSnapshot) {
      this.store.setRestoredChartSnapshot(session.chartSnapshot);
    }
    this.sessionsDrawerOpen.set(false);
  }

  async deleteSession(id: string, event: Event): Promise<void> {
    event.stopPropagation();
    this.sessionError.set(null);
    const deleted = await this.attemptSessionCall(() => this.sessionService.deleteSession(id));
    if (deleted.ok && this.activeSessionId() === id) {
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
      this.sessionError.set(null);
      await this.attemptSessionCall(() => this.sessionService.renameSession(id, this.renameValue().trim()));
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
}
