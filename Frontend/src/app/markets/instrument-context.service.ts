import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, type Params } from '@angular/router';

import { DataLabRangePresetsService, type ChartRangePreset } from '../services/data-lab-range-presets.service';
import {
  PRESET_DEFAULT_TIMEFRAMES,
  PRESET_LABELS,
  barTimeframeByValue,
  parseYmdMsUtc,
  presetQuickRanges,
} from '../components/data-lab/data-lab-scope';
import { utcDayEndMs, utcMsToIsoDate } from '../components/data-lab/data-lab-request-mapper';
import { DataLabWorkspaceStore } from '../components/data-lab/data-lab-workspace-store';
import type { QuickRangeKey, QuickRangeOffer } from '../shared/date-picker';
import { isUsEquitySymbol } from '../shared/symbol-catalog/us-equity-symbol';
import {
  CONTEXT_QUERY_KEYS,
  readInstrumentContext,
  sameContext,
  type ContextRead,
  type InstrumentContext,
  type InstrumentSession,
  type InstrumentWindow,
} from './instrument-context';

/** The quick range an instrument opens on when its address names no window. */
const DEFAULT_RANGE_KEY = '1Y';

/** What the controls show while the committed context is not usable: no window, and the timeframe and session the address names or defaults. */
export interface ShownContext {
  readonly window: InstrumentWindow | null;
  readonly timeframe: string;
  readonly session: InstrumentSession;
}

/**
 * An instrument place's committed context: the one reader of its address and
 * the one writer of it.
 *
 * Provided by the place's shell, so every tab under it shares one instance
 * and a tab change keeps what was committed. The address is the truth: a
 * picker commits by navigating, and the committed context follows the address,
 * so Back, Forward and a reload land on what was committed. Nothing here
 * loads data; the Chart tab loads when the committed context it reads
 * changes.
 *
 * Commit rules (PRD #3490 R05):
 *   - a change that equals what is committed does nothing;
 *   - a half-typed, reversed or unreal window never reaches `commit`: the date
 *     picker hands an empty date for it, and `commitWindow` refuses that;
 *   - an address without a window opens on the default range Python resolves,
 *     shown and committed like any other, and written to the address only once
 *     something is committed over it.
 */
@Injectable()
export class InstrumentContextService {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly store = inject(DataLabWorkspaceStore);
  private readonly rangePresets = inject(DataLabRangePresetsService);

  private readonly params = toSignal(this.route.paramMap, { initialValue: this.route.snapshot.paramMap });
  private readonly query = toSignal(this.route.queryParamMap, { initialValue: this.route.snapshot.queryParamMap });

  /** The instrument, upper-cased as the lake names it. Empty when the address has none. */
  readonly symbol = computed(() => (this.params().get('symbol') ?? '').trim().toUpperCase());

  /** What the address says about the context. */
  readonly read = computed<ContextRead>(() => readInstrumentContext(this.query()));

  /** The context keys the address carries, for a link that keeps the context across a tab. */
  readonly carriedQuery = computed<Readonly<Record<string, string>>>(() =>
    Object.fromEntries(
      CONTEXT_QUERY_KEYS.flatMap((key) => {
        const value = this.query().get(key);
        return value === null ? [] : [[key, value]];
      }),
    ),
  );

  // ── Quick ranges: Python resolves every date ────────────────
  private readonly presets = signal<readonly ChartRangePreset[]>([]);
  /** Why the quick ranges could not be read, when they could not. */
  readonly presetsProblem = signal<string | null>(null);
  /** Whether Python's ranges have been read once, whatever they held. */
  private readonly presetsRead = signal(false);
  readonly presetOffers = computed<readonly QuickRangeOffer[]>(() => presetQuickRanges(this.presets()));
  private presetTurn = 0;

  /** The window an address with none opens on: the 1Y range, resolved by Python. */
  private readonly defaultWindow = computed<InstrumentWindow | null>(() => {
    const preset = this.presets().find((candidate) => candidate.key === DEFAULT_RANGE_KEY);
    return preset === undefined
      ? null
      : { from: utcMsToIsoDate(preset.start_ms_utc), to: utcMsToIsoDate(preset.end_ms_utc) };
  });

  /**
   * Where an address with no window stands while it has none to open on:
   * still reading Python's ranges, or unable to. `null` when it has a window
   * or needs none.
   */
  readonly defaultWindowPending = computed<{ readonly kind: 'resolving' } | { readonly kind: 'unavailable'; readonly reason: string } | null>(() => {
    if (this.read().kind !== 'window-missing' || this.defaultWindow() !== null) return null;
    const problem = this.presetsProblem();
    if (problem !== null) return { kind: 'unavailable', reason: problem };
    return this.presetsRead()
      ? { kind: 'unavailable', reason: `The ${DEFAULT_RANGE_KEY} range was not among the ranges Python resolved.` }
      : { kind: 'resolving' };
  });

  /** What the controls show: what the address commits, else what it names and the default window. */
  readonly shown = computed<ShownContext>(() => {
    const read = this.read();
    if (read.kind === 'ready') return read.context;
    const window = read.kind === 'window-missing' ? this.defaultWindow() : null;
    return { window, timeframe: read.timeframe, session: read.session };
  });

  /**
   * The committed context, or `null` while there is none to load: the address
   * is unusable, or it names no window and the default is not yet resolved.
   * Two reads that commit the same request are the same value, so a repeated
   * emission wakes nobody.
   */
  readonly context = computed<InstrumentContext | null>(
    () => {
      const { window, timeframe, session } = this.shown();
      return window === null ? null : { window, timeframe, session };
    },
    { equal: (a, b) => a === b || (a !== null && b !== null && sameContext(a, b)) },
  );

  constructor() {
    void this.loadPresets();
    // The workspace store holds what the chart, the dataset builder and the
    // saved scopes read. It follows the address, never the other way round.
    effect(() => {
      const context = this.context();
      const symbol = this.symbol();
      if (context === null || !isUsEquitySymbol(symbol)) return;
      untracked(() => this.followAddress(symbol, context));
    });
  }

  private async loadPresets(): Promise<void> {
    try {
      this.presets.set(await this.rangePresets.presets('rth'));
      this.presetsProblem.set(null);
      this.presetsRead.set(true);
    } catch (error: unknown) {
      this.presetsProblem.set(`The quick ranges could not be read: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  /** Commits the address's context into the workspace store, once per distinct request. */
  private followAddress(symbol: string, context: InstrumentContext): void {
    const start = parseYmdMsUtc(context.window.from);
    const end = parseYmdMsUtc(context.window.to);
    const timeframe = barTimeframeByValue(context.timeframe);
    if (start === null || end === null || timeframe === undefined) return;
    const startMsUtc = start;
    const endMsUtc = utcDayEndMs(end);
    const held = this.store.committedScope();
    if (
      held !== null &&
      held.ticker === symbol &&
      held.window.startMsUtc === startMsUtc &&
      held.window.endMsUtc === endMsUtc &&
      held.timespan === timeframe.timespan &&
      held.multiplier === timeframe.multiplier &&
      held.session === context.session
    ) {
      return;
    }
    this.store.patchDraft({
      ticker: symbol,
      window: { startMsUtc, endMsUtc },
      timeframe: timeframe.timespan === 'day' && timeframe.multiplier === 1 ? 'day' : timeframe.value,
      timespan: timeframe.timespan,
      multiplier: timeframe.multiplier,
      session: context.session,
    });
    this.store.commitScope();
  }

  // ── Committing ──────────────────────────────────────────────

  /**
   * Commits a window the shared date picker handed over. A date the picker
   * refused arrives empty, and so does a half-typed one: neither commits, and
   * neither starts a request. Returns whether the address changed.
   */
  commitWindow(dates: { readonly from: string; readonly to: string }): boolean {
    if (dates.from === '' || dates.to === '') return false;
    return this.commit({ window: { from: dates.from, to: dates.to } });
  }

  /** Commits a bar timeframe the place offers. Returns whether the address changed. */
  commitTimeframe(value: string): boolean {
    return barTimeframeByValue(value) === undefined ? false : this.commit({ timeframe: value });
  }

  /** Commits a session. Returns whether the address changed. */
  commitSession(session: string): boolean {
    return session === 'regular' || session === 'extended' ? this.commit({ session }) : false;
  }

  /**
   * Commits the window of the quick range a person clicked: Python's own
   * resolution, read again so a tab left open across a trading day moves with
   * it. A short range also takes the intraday timeframe that keeps it from
   * being one candle.
   */
  async commitPreset(key: QuickRangeKey): Promise<boolean> {
    const turn = ++this.presetTurn;
    const named = (preset: ChartRangePreset) => PRESET_LABELS[preset.key] === key;
    let target = this.presets().find(named);
    try {
      const fresh = await this.rangePresets.presets('rth');
      this.presets.set(fresh);
      this.presetsProblem.set(null);
      target = fresh.find(named) ?? target;
    } catch (error: unknown) {
      // The cached range is still Python's resolution, and a stale-but-correct
      // window beats refusing a click; the bar says the ranges are stale.
      this.presetsProblem.set(`The quick ranges could not be refreshed: ${error instanceof Error ? error.message : String(error)}`);
    }
    // A later click answered first: this answer is for an earlier one.
    if (target === undefined || turn !== this.presetTurn) return false;
    const suggested = PRESET_DEFAULT_TIMEFRAMES[target.key];
    return this.commit({
      window: { from: utcMsToIsoDate(target.start_ms_utc), to: utcMsToIsoDate(target.end_ms_utc) },
      ...(suggested === undefined ? {} : { timeframe: suggested }),
    });
  }

  /**
   * Writes a change to the address. Everything committed so far is written
   * with it, so the window the place opened on is pinned the moment anything
   * is committed over it. A change equal to what is committed does nothing.
   */
  private commit(change: Partial<InstrumentContext>): boolean {
    const held = this.context();
    if (held !== null && sameContext(held, { ...held, ...change })) return false;
    const shown = this.shown();
    const window = change.window ?? shown.window;
    this.writeQuery({
      timeframe: change.timeframe ?? shown.timeframe,
      session: change.session ?? shown.session,
      ...(window === null ? {} : { from: window.from, to: window.to }),
    });
    return true;
  }

  /** Moves to another instrument on the same tab, keeping the context and everything else in the address. */
  switchSymbol(symbol: string): void {
    const tree = this.router.parseUrl(this.router.url);
    const segments = tree.root.children['primary']?.segments.map((segment) => segment.path) ?? [];
    const next = symbol.toUpperCase();
    if (segments.length < 2 || segments[1].toUpperCase() === next) return;
    segments[1] = next;
    void this.router.navigate(['/', ...segments], { queryParams: tree.queryParams, fragment: tree.fragment ?? undefined });
  }

  private writeQuery(patch: Params): void {
    const tree = this.router.parseUrl(this.router.url);
    tree.queryParams = { ...tree.queryParams, ...patch };
    void this.router.navigateByUrl(tree);
  }
}
