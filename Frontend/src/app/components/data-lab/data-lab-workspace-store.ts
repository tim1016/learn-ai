import { signal } from '@angular/core';

import {
  ChartSeriesColorToken,
  isChartSeriesColorToken,
} from '../../shared/trading-chart/chart-series-color-tokens';
import { browserTimeZone, type ExportColumnSelection } from './export-csv-options';

/* DataLabWorkspaceStore.
 *
 * Component-scoped state an instrument's place shares across its tabs (the
 * Chart's recipe, the committed scope, the Build dataset choices). The store is
 * NOT provided in any injector root — the place constructs it via
 * `createDataLabWorkspaceStore()`, so state dies with the place instead of
 * leaking across the app.
 *
 * Server sessions and runs remain durable truth; the store keeps references
 * and draft state only. All timestamps are int64 ms UTC at every boundary. */

export const DATA_LAB_WORKSPACE_SCHEMA_VERSION = 2;

/** Half-open numeric UTC window: [startMsUtc, endMsUtc). */
export interface DataLabWindowMsUtc {
  startMsUtc: number;
  endMsUtc: number;
}

/** A staged indicator instance. `id` is parameter-aware identity:
 *  `${canonicalKey}|${sorted param k=v}` — same name + same params means the
 *  same instance; changing params means a new identity. */
export interface DataLabIndicatorInstance {
  readonly id: string;
  readonly canonicalKey: string;
  readonly params: Readonly<Record<string, number>>;
}

/** Draft bar-policy + session scope fields (commit via commitScope()). */
export interface DataLabDraftScope {
  ticker: string;
  window: DataLabWindowMsUtc;
  timeframe: string;
  timespan: string;
  multiplier: number;
  session: string;
  forwardFill: boolean;
  adjusted: boolean;
}

/** Companion-file settings — flags mirrored into the generate-zip payload. */
export interface DataLabCompanionSettings {
  optionsCompanionEnabled: boolean;
  includeQualityReport: boolean;
  includePreviousClose: boolean;
  includeSplits: boolean;
  includeDividends: boolean;
  includeTickerOverview: boolean;
  includeNews: boolean;
  includeFinancials: boolean;
  includeStockTrades: boolean;
  includeStockQuotes: boolean;
}

/** Reference to the saved session the workspace was restored from. */
export interface DataLabSavedSessionRef {
  readonly id: string;
  readonly schemaVersion: number;
}

/** Stable parameter-aware instance identity. Params are sorted so
 *  `{a:1,b:2}` and `{b:2,a:1}` are the same instance. Pure. */
export function dataLabIndicatorInstanceId(
  canonicalKey: string,
  params: Readonly<Record<string, number>>,
): string {
  const sorted = Object.keys(params)
    .sort()
    .map(k => `${k}=${params[k]}`)
    .join(',');
  return `${canonicalKey}|${sorted}`;
}

export interface DataLabScopeCommitResult {
  ok: boolean;
  error: string | null;
}

// ── Restore result ────────────────────────────────────────────
export interface RestoreResult {
  warnings: string[];
}

const COMPANION_KEYS: readonly (keyof DataLabCompanionSettings)[] = [
  'optionsCompanionEnabled',
  'includeQualityReport',
  'includePreviousClose',
  'includeSplits',
  'includeDividends',
  'includeTickerOverview',
  'includeNews',
  'includeFinancials',
  'includeStockTrades',
  'includeStockQuotes',
];

const DEFAULT_COMPANIONS: DataLabCompanionSettings = {
  optionsCompanionEnabled: false,
  includeQualityReport: false,
  includePreviousClose: false,
  includeSplits: false,
  includeDividends: false,
  includeTickerOverview: false,
  includeNews: false,
  includeFinancials: false,
  includeStockTrades: false,
  includeStockQuotes: false,
};

function omitKey<T extends Record<string, unknown>>(map: T, key: string): T {
  return Object.fromEntries(
    Object.entries(map).filter(([k]) => k !== key),
  ) as T;
}

function isFiniteInt(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && Number.isFinite(v);
}

function isParams(v: unknown): v is Record<string, number> {
  if (typeof v !== 'object' || v === null) return false;
  for (const value of Object.values(v)) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return false;
  }
  return true;
}

/** Pure workspace store for an instrument place. Construct through
 *  `createDataLabWorkspaceStore()` in the place's providers. */
export class DataLabWorkspaceStore {
  // ── Committed scope (what the chart/news follow) ────────────
  private readonly _committedTicker = signal('');
  private readonly _committedWindow = signal<DataLabWindowMsUtc | null>(null);
  private readonly _committedScope = signal<DataLabDraftScope | null>(null);

  // ── Draft scope ─────────────────────────────────────────────
  private readonly _draft = signal<DataLabDraftScope>(this.initialDraft());

  // ── Recipe ──────────────────────────────────────────────────
  private readonly _indicators = signal<readonly DataLabIndicatorInstance[]>([]);
  private readonly _colorOverrides = signal<Readonly<Record<string, ChartSeriesColorToken>>>({});

  // ── Companions ──────────────────────────────────────────────
  private readonly _companions = signal<DataLabCompanionSettings>({ ...DEFAULT_COMPANIONS });

  // ── dataset.csv choices (owner decision 2026-09-19) ─────────
  // Kept while the owner moves between Data Lab tabs; not part of the
  // saved-session envelope.
  private readonly _exportColumns = signal<ExportColumnSelection>(null);
  private readonly _exportTimeZone = signal<string | null>(browserTimeZone());

  // ── Session / chart / receipts / runs ───────────────────────
  private readonly _savedSession = signal<DataLabSavedSessionRef | null>(null);
  private readonly _lastChartRequestSignature = signal<string | null>(null);
  private readonly _chartStale = signal(false);
  private readonly _datasetPlanReceipt = signal<Readonly<Record<string, unknown>> | null>(null);
  /** Serialized request payload the current receipt describes. A receipt whose
   *  signature no longer matches the live recipe is stale and must not be
   *  presented as current plan data. */
  private readonly _datasetPlanSignature = signal<string | null>(null);
  /** Latest chart payload the Chart tab produced — a reference "Save setup"
   *  merges in, and the answer the tab shows again when it is reopened with the
   *  same context. Never triggers a chart render by itself. */
  private readonly _latestChartSnapshot = signal<unknown | null>(null);

  // ── Read-only state surface ─────────────────────────────────
  readonly committedTicker = this._committedTicker.asReadonly();
  readonly committedWindow = this._committedWindow.asReadonly();
  readonly committedScope = this._committedScope.asReadonly();
  readonly draft = this._draft.asReadonly();
  readonly indicators = this._indicators.asReadonly();
  readonly colorTokenOverrides = this._colorOverrides.asReadonly();
  readonly companions = this._companions.asReadonly();
  readonly exportColumns = this._exportColumns.asReadonly();
  readonly exportTimeZone = this._exportTimeZone.asReadonly();
  readonly lastChartRequestSignature = this._lastChartRequestSignature.asReadonly();
  readonly chartStale = this._chartStale.asReadonly();
  readonly datasetPlanReceipt = this._datasetPlanReceipt.asReadonly();
  readonly datasetPlanReceiptSignature = this._datasetPlanSignature.asReadonly();
  readonly latestChartSnapshot = this._latestChartSnapshot.asReadonly();
  /** The saved setup the workspace was opened from or saved as, if any. */
  readonly savedSession = this._savedSession.asReadonly();

  // ── Scope ───────────────────────────────────────────────────
  /** Validate and atomically commit the draft scope. Returns an error string
   *  (draft unchanged) when the ticker is blank or the window is unset or
   *  inverted. The uninitialized draft window `{0, 0}` is rejected
   *  explicitly — under the interim date-anchor semantics (known-gaps §13:
   *  start = UTC midnight of the from-date, end = the to-date's final UTC
   *  instant) an equal pair names one trading day, but epoch zeros name
   *  nothing and would fetch 1970. A committed window is otherwise ordered
   *  `startMsUtc <= endMsUtc`; the canonical half-open resolution
   *  will tighten this further. */
  commitScope(): DataLabScopeCommitResult {
    const draft = this._draft();
    const ticker = draft.ticker.trim().toUpperCase();
    if (!ticker) return { ok: false, error: 'Ticker is required' };
    if (
      !isFiniteInt(draft.window.startMsUtc) ||
      !isFiniteInt(draft.window.endMsUtc) ||
      (draft.window.startMsUtc === 0 && draft.window.endMsUtc === 0)
    ) {
      return { ok: false, error: 'Pick a date range before applying the scope' };
    }
    if (draft.window.startMsUtc > draft.window.endMsUtc) {
      return { ok: false, error: 'Window must satisfy startMsUtc <= endMsUtc' };
    }
    const next: DataLabDraftScope = { ...draft, ticker };
    this._draft.set(next);
    this._committedTicker.set(ticker);
    this._committedWindow.set({ ...draft.window });
    this._committedScope.set(next);
    this._chartStale.set(true);
    return { ok: true, error: null };
  }

  /** Patch part of the draft scope (shallow merge). */
  patchDraft(patch: Partial<DataLabDraftScope>): void {
    this._draft.update(d => ({ ...d, ...patch }));
  }

  // ── Chart staleness ─────────────────────────────────────────
  markChartStale(): void {
    this._chartStale.set(true);
  }

  /** Record the signature of the chart request actually issued. The stale
   *  flag STAYS set while the request is pending — an in-flight failure must
   *  leave the old bars visibly out of date, not looking current. Call
   *  {@link settleChartRequest} from the explicit success/failure callback.
   *  Returns true when the stored signature changes. */
  recordChartRequest(signature: string): boolean {
    const changed = this._lastChartRequestSignature() !== signature;
    this._lastChartRequestSignature.set(signature);
    return changed;
  }

  /** Settle a pending chart request (success or failure): only here does the
   *  stale flag clear, so a failed fetch never leaves old bars reading as
   *  current. */
  settleChartRequest(): void {
    this._chartStale.set(false);
  }

  // ── Recipe (setRecipe ops) ──────────────────────────────────
  /** Add an indicator instance. Adding an existing identity is a no-op that
   *  returns the existing instance id. */
  addIndicator(canonicalKey: string, params: Readonly<Record<string, number>>): string {
    const id = dataLabIndicatorInstanceId(canonicalKey, params);
    if (this._indicators().some(i => i.id === id)) return id;
    this._indicators.update(list => [
      ...list,
      { id, canonicalKey, params: { ...params } },
    ]);
    this.markChartStale();
    return id;
  }

  removeIndicator(id: string): void {
    if (!this._indicators().some(i => i.id === id)) return;
    this._indicators.update(list => list.filter(i => i.id !== id));
    this._colorOverrides.update(map => {
      if (!(id in map)) return map;
      return omitKey(map, id);
    });
    this.markChartStale();
  }

  /** Update an instance's params. Preserves list position and migrates the
   *  color override to the new parameter-aware identity. Returns the new id,
   *  or null when `id` is unknown — or when the new params collide with a
   *  DIFFERENT instance's identity: identity is parameter-aware, so merging
   *  two instances silently would duplicate ids. Refusing (keeping the
   *  previous state) is the least surprising option; remove the other
   *  instance first if the merge is intended. */
  updateIndicator(id: string, params: Readonly<Record<string, number>>): string | null {
    const list = this._indicators();
    const index = list.findIndex(i => i.id === id);
    if (index === -1) return null;
    const instance = list[index];
    const nextId = dataLabIndicatorInstanceId(instance.canonicalKey, params);
    if (list.some(i => i.id === nextId && i.id !== id)) return null;
    const next: DataLabIndicatorInstance = { id: nextId, canonicalKey: instance.canonicalKey, params: { ...params } };
    this._indicators.update(current => {
      const copy = [...current];
      copy[index] = next;
      return copy;
    });
    if (nextId !== id) {
      this._colorOverrides.update(map => {
        const token = map[id];
        if (!token) return map;
        return { ...omitKey(map, id), [nextId]: token };
      });
    }
    this.markChartStale();
    return nextId;
  }

  // ── Color overrides (theme-only tokens) ─────────────────────
  /** Approve a series-color token for an instance. Rejects unknown instances
   *  and non-token values (hex, RGB, arbitrary CSS strings). */
  setColorToken(instanceId: string, token: ChartSeriesColorToken): boolean {
    if (!isChartSeriesColorToken(token)) return false;
    if (!this._indicators().some(i => i.id === instanceId)) return false;
    this._colorOverrides.update(map => ({ ...map, [instanceId]: token }));
    return true;
  }

  // ── Companions / receipts / refs ────────────────────────────
  patchCompanions(patch: Partial<DataLabCompanionSettings>): void {
    this._companions.update(c => ({ ...c, ...patch }));
  }

  /** `null` exports every planned column, including later ones. */
  setExportColumns(selection: ExportColumnSelection): void {
    this._exportColumns.set(selection === null ? null : [...selection]);
  }

  /** IANA zone of the readable time column; `null` omits the column. */
  setExportTimeZone(zone: string | null): void {
    this._exportTimeZone.set(zone);
  }

  setSavedSession(ref: DataLabSavedSessionRef | null): void {
    this._savedSession.set(ref ? { ...ref } : null);
  }

  setDatasetPlanReceipt(
    receipt: Readonly<Record<string, unknown>> | null,
    planSignature: string | null = null,
  ): void {
    this._datasetPlanReceipt.set(receipt ? { ...receipt } : null);
    this._datasetPlanSignature.set(receipt ? planSignature : null);
  }

  /** Record the latest fetched chart payload (for Save setup, and the answer the Chart tab shows again for the same request). */
  setLatestChartSnapshot(snapshot: unknown | null): void {
    this._latestChartSnapshot.set(snapshot);
  }

  // ── Restore (saved-setup JSON, schema v2) ───────────────────
  /** Restore from a v2 envelope. Bounded validator: unknown keys and
   *  invalid entries are dropped with a reported warning; valid state is
   *  applied. A schema-version mismatch refuses the restore entirely. */
  restore(value: unknown): RestoreResult {
    const warnings: string[] = [];
    if (typeof value !== 'object' || value === null) {
      return { warnings: ['Workspace payload is not an object; nothing restored'] };
    }
    const raw = value as Record<string, unknown>;
    if (raw['schemaVersion'] !== DATA_LAB_WORKSPACE_SCHEMA_VERSION) {
      return {
        warnings: [
          `Unsupported workspace schemaVersion ${String(raw['schemaVersion'])}; expected ${DATA_LAB_WORKSPACE_SCHEMA_VERSION}`,
        ],
      };
    }
    const knownKeys = new Set([
      'schemaVersion', 'ticker', 'windowMsUtc', 'scope', 'indicators',
      'colorTokenOverrides', 'companions', 'savedSession',
    ]);
    for (const key of Object.keys(raw)) {
      if (!knownKeys.has(key)) warnings.push(`Dropped unknown key "${key}"`);
    }

    // Ticker + window
    // Same normalization as commitScope(): a restored ticker enters the
    // committed state trimmed and uppercased, so a restored payload can't
    // smuggle in a differently-cased duplicate scope.
    let ticker = '';
    if (typeof raw['ticker'] === 'string') {
      const normalized = raw['ticker'].trim().toUpperCase();
      if (normalized) ticker = normalized;
      else warnings.push('Dropped invalid ticker');
    } else if ('ticker' in raw) {
      warnings.push('Dropped invalid ticker');
    }

    let window: DataLabWindowMsUtc | null = null;
    const rawWindow = raw['windowMsUtc'];
    if (typeof rawWindow === 'object' && rawWindow !== null) {
      const w = rawWindow as Record<string, unknown>;
      if (isFiniteInt(w['startMsUtc']) && isFiniteInt(w['endMsUtc'])) {
        // Same window rule as commitScope(): only an ordered pair with at
        // least one nonzero endpoint commits — accepting anything else would
        // smuggle an invalid committed scope into the workspace.
        if (
          w['startMsUtc'] <= w['endMsUtc'] &&
          !(w['startMsUtc'] === 0 && w['endMsUtc'] === 0)
        ) {
          window = { startMsUtc: w['startMsUtc'], endMsUtc: w['endMsUtc'] };
        } else {
          warnings.push('Dropped invalid windowMsUtc');
        }
      } else if ('windowMsUtc' in raw) {
        warnings.push('Dropped invalid windowMsUtc');
      }
    } else if ('windowMsUtc' in raw) {
      warnings.push('Dropped invalid windowMsUtc');
    }

    // Scope fields (draft bar policy)
    const rawScope = raw['scope'];
    const draftPatch: Partial<DataLabDraftScope> = {};
    if (typeof rawScope === 'object' && rawScope !== null) {
      const s = rawScope as Record<string, unknown>;
      const timeframe = s['timeframe'];
      if (typeof timeframe === 'string') draftPatch['timeframe'] = timeframe;
      const timespan = s['timespan'];
      if (typeof timespan === 'string') draftPatch['timespan'] = timespan;
      const multiplier = s['multiplier'];
      if (typeof multiplier === 'number' && Number.isFinite(multiplier)) {
        draftPatch['multiplier'] = multiplier;
      }
      const session = s['session'];
      if (typeof session === 'string') draftPatch['session'] = session;
      const forwardFill = s['forwardFill'];
      if (typeof forwardFill === 'boolean') draftPatch['forwardFill'] = forwardFill;
      const adjusted = s['adjusted'];
      if (typeof adjusted === 'boolean') draftPatch['adjusted'] = adjusted;
    } else if ('scope' in raw) {
      warnings.push('Dropped invalid scope');
    }

    // Indicators — bounded recipe validation
    const instances: DataLabIndicatorInstance[] = [];
    const seenIds = new Set<string>();
    const rawIndicators = raw['indicators'];
    if (Array.isArray(rawIndicators)) {
      for (const entry of rawIndicators) {
        if (typeof entry !== 'object' || entry === null) {
          warnings.push('Dropped invalid indicator entry (not an object)');
          continue;
        }
        const e = entry as Record<string, unknown>;
        if (typeof e['canonicalKey'] !== 'string' || !e['canonicalKey']) {
          warnings.push('Dropped indicator entry missing canonicalKey');
          continue;
        }
        if (!isParams(e['params'])) {
          warnings.push(`Dropped indicator entry "${String(e['canonicalKey'])}" with invalid params`);
          continue;
        }
        const id = dataLabIndicatorInstanceId(e['canonicalKey'], e['params']);
        if (seenIds.has(id)) {
          warnings.push(`Dropped duplicate indicator entry "${id}"`);
          continue;
        }
        seenIds.add(id);
        instances.push({ id, canonicalKey: e['canonicalKey'], params: { ...e['params'] } });
      }
    } else if ('indicators' in raw) {
      warnings.push('Dropped invalid indicators list');
    }

    // Color overrides — only valid tokens for surviving instances
    const overrides: Record<string, ChartSeriesColorToken> = {};
    const rawOverrides = raw['colorTokenOverrides'];
    if (typeof rawOverrides === 'object' && rawOverrides !== null) {
      const instanceIds = new Set(instances.map(i => i.id));
      for (const [instanceId, token] of Object.entries(rawOverrides as Record<string, unknown>)) {
        if (!isChartSeriesColorToken(token)) {
          warnings.push(`Dropped invalid color token "${String(token)}"`);
          continue;
        }
        if (!instanceIds.has(instanceId)) {
          warnings.push(`Dropped color override for unknown instance "${instanceId}"`);
          continue;
        }
        overrides[instanceId] = token;
      }
    } else if ('colorTokenOverrides' in raw) {
      warnings.push('Dropped invalid colorTokenOverrides');
    }

    // Companions — booleans only
    const companions: DataLabCompanionSettings = { ...DEFAULT_COMPANIONS };
    const rawCompanions = raw['companions'];
    if (typeof rawCompanions === 'object' && rawCompanions !== null) {
      const c = rawCompanions as Record<string, unknown>;
      for (const key of COMPANION_KEYS) {
        if (typeof c[key] === 'boolean') companions[key] = c[key] as boolean;
      }
    } else if ('companions' in raw) {
      warnings.push('Dropped invalid companions');
    }

    // Saved session ref
    let savedSession: DataLabSavedSessionRef | null = null;
    const rawSession = raw['savedSession'];
    if (typeof rawSession === 'object' && rawSession !== null) {
      const s = rawSession as Record<string, unknown>;
      const id = s['id'];
      const schemaVersion = s['schemaVersion'];
      if (typeof id === 'string' && isFiniteInt(schemaVersion)) {
        savedSession = { id, schemaVersion };
      } else {
        warnings.push('Dropped invalid savedSession');
      }
    } else if (rawSession !== null && rawSession !== undefined) {
      warnings.push('Dropped invalid savedSession');
    }

    // Apply. The committed ticker/window/scope triple is derived from ONE
    // validated scope object and written together — a partial write would
    // leave e.g. committedScope null while committedTicker is set, which
    // readers treat as "never committed". A payload without a usable
    // ticker+window restores the draft/recipe but leaves the committed
    // scope untouched.
    this._draft.update(d => ({ ...d, ...draftPatch, ticker, window: window ?? d.window }));
    if (ticker && window) {
      const scope: DataLabDraftScope = {
        ...this._draft(),
        ticker,
        window: { ...window },
      };
      this._committedTicker.set(scope.ticker);
      this._committedWindow.set({ ...scope.window });
      this._committedScope.set(scope);
    }
    this._indicators.set(instances);
    this._colorOverrides.set(overrides);
    this._companions.set(companions);
    this._savedSession.set(savedSession);
    this._chartStale.set(false);
    return { warnings };
  }

  private initialDraft(): DataLabDraftScope {
    return {
      ticker: '',
      window: { startMsUtc: 0, endMsUtc: 0 },
      timeframe: 'day',
      timespan: 'day',
      multiplier: 1,
      session: 'regular',
      forwardFill: true,
      adjusted: true,
    };
  }
}

/** Place-owned construction — the store is deliberately not `providedIn`:
 *  it must live and die with the instrument place that provides it. */
export function createDataLabWorkspaceStore(): DataLabWorkspaceStore {
  return new DataLabWorkspaceStore();
}

/** The product's thirteen-indicator default recipe (unchanged
 *  until a separate product decision). A place seeds these into a fresh
 *  workspace through {@link seedDefaultIndicators}. */
export const DEFAULT_DATA_LAB_INDICATORS: readonly {
  canonicalKey: string;
  params: Record<string, number>;
}[] = [
  { canonicalKey: 'ema', params: { length: 5 } },
  { canonicalKey: 'ema', params: { length: 10 } },
  { canonicalKey: 'ema', params: { length: 20 } },
  { canonicalKey: 'ema', params: { length: 30 } },
  { canonicalKey: 'ema', params: { length: 40 } },
  { canonicalKey: 'ema', params: { length: 50 } },
  { canonicalKey: 'ema', params: { length: 100 } },
  { canonicalKey: 'ema', params: { length: 200 } },
  { canonicalKey: 'bbands', params: { length: 20, std: 2.0 } },
  { canonicalKey: 'supertrend', params: { length: 10, multiplier: 3.0 } },
  { canonicalKey: 'macd', params: { fast: 12, slow: 26, signal: 9 } },
  { canonicalKey: 'rsi', params: { length: 14 } },
  { canonicalKey: 'adx', params: { length: 14 } },
];

/** Seed the default recipe into a fresh workspace; a workspace that already
 *  holds a recipe is left alone. Seeding never fetches anything. */
export function seedDefaultIndicators(store: DataLabWorkspaceStore): void {
  if (store.indicators().length > 0) return;
  for (const entry of DEFAULT_DATA_LAB_INDICATORS) {
    store.addIndicator(entry.canonicalKey, entry.params);
  }
  store.markChartStale();
}
