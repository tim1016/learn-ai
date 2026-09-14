import { describe, expect, it } from 'vitest';

import {
  DATA_LAB_WORKSPACE_SCHEMA_VERSION,
  DataLabWorkspaceStore,
  createDataLabWorkspaceStore,
  dataLabIndicatorInstanceId,
} from './data-lab-workspace-store';

const WINDOW = { startMsUtc: Date.UTC(2026, 0, 2), endMsUtc: Date.UTC(2026, 5, 30) };

function committedStore(): DataLabWorkspaceStore {
  const store = createDataLabWorkspaceStore();
  store.patchDraft({ ticker: 'aapl', window: WINDOW, timeframe: 'hour', multiplier: 2 });
  const result = store.commitScope();
  if (!result.ok) throw new Error(`commitScope failed: ${result.error}`);
  return store;
}

describe('dataLabIndicatorInstanceId', () => {
  it('is parameter-aware and order-independent', () => {
    expect(dataLabIndicatorInstanceId('ema', { length: 10 })).toBe('ema|length=10');
    expect(dataLabIndicatorInstanceId('macd', { fast: 12, slow: 26 })).toBe(
      'macd|fast=12,slow=26',
    );
    expect(dataLabIndicatorInstanceId('macd', { slow: 26, fast: 12 })).toBe(
      'macd|fast=12,slow=26',
    );
  });
});

describe('DataLabWorkspaceStore scope', () => {
  it('commitScope validates and moves draft into committed state', () => {
    const store = committedStore();
    expect(store.committedTicker()).toBe('AAPL');
    expect(store.committedWindow()).toEqual(WINDOW);
    expect(store.committedScope()?.timeframe).toBe('hour');
  });

  it('rejects a blank ticker and an inverted window without mutating state', () => {
    const store = createDataLabWorkspaceStore();
    store.patchDraft({ ticker: '   ' });
    expect(store.commitScope().ok).toBe(false);
    store.patchDraft({ ticker: 'aapl', window: { startMsUtc: 200, endMsUtc: 100 } });
    expect(store.commitScope().ok).toBe(false);
    expect(store.committedTicker()).toBe('');
    expect(store.committedWindow()).toBeNull();
  });

  it('commits a single-day window — equality names one trading date, not an empty range', () => {
    // The interim date-anchor semantics (known-gaps §13) make the pair two
    // inclusive UTC-midnight trading-date anchors; a 1D quick range resolves
    // to the same session on both ends.
    const store = createDataLabWorkspaceStore();
    const day = Date.UTC(2026, 8, 11);
    store.patchDraft({ ticker: 'SPY', window: { startMsUtc: day, endMsUtc: day } });
    expect(store.commitScope().ok).toBe(true);
    expect(store.committedWindow()).toEqual({ startMsUtc: day, endMsUtc: day });
  });

  it('committing marks the chart stale', () => {
    const store = committedStore();
    expect(store.chartStale()).toBe(true);
  });
});

describe('DataLabWorkspaceStore chart staleness', () => {
  it('markChartStale/consumeStale round-trip keyed by request signature', () => {
    const store = createDataLabWorkspaceStore();
    expect(store.chartStale()).toBe(false);
    store.markChartStale();
    expect(store.consumeStale('sig-1')).toBe(false); // no signature recorded yet
    expect(store.recordChartRequest('sig-1')).toBe(true);
    // Recording a request no longer clears staleness — only settling does.
    expect(store.chartStale()).toBe(true);
    store.settleChartRequest();
    expect(store.chartStale()).toBe(false);
    store.markChartStale();
    expect(store.consumeStale('sig-2')).toBe(false); // wrong signature
    expect(store.chartStale()).toBe(true);
    expect(store.consumeStale('sig-1')).toBe(true);
    expect(store.chartStale()).toBe(false);
  });

  it('recordChartRequest keeps the stale flag set while a request is pending', () => {
    // An in-flight (or failed) fetch must leave old bars visibly out of
    // date — only settleChartRequest (the explicit success/failure
    // callback) clears the flag.
    const store = committedStore();
    expect(store.chartStale()).toBe(true);
    expect(store.recordChartRequest('sig-a')).toBe(true);
    expect(store.chartStale()).toBe(true);
    store.settleChartRequest();
    expect(store.chartStale()).toBe(false);
  });
});

describe('DataLabWorkspaceStore recipe', () => {
  it('addIndicator is idempotent for identical parameter-aware identity', () => {
    const store = createDataLabWorkspaceStore();
    const a = store.addIndicator('ema', { length: 10 });
    const b = store.addIndicator('ema', { length: 10 });
    expect(b).toBe(a);
    expect(store.indicators()).toHaveLength(1);
  });

  it('updateIndicator preserves list position and migrates the color override', () => {
    const store = createDataLabWorkspaceStore();
    const emaId = store.addIndicator('ema', { length: 10 });
    const rsiId = store.addIndicator('rsi', { length: 14 });
    expect(store.setColorToken(emaId, 'series-amber')).toBe(true);

    const newId = store.updateIndicator(emaId, { length: 50 });
    expect(newId).not.toBeNull();
    expect(newId).not.toBe(emaId);
    expect(newId).toBe('ema|length=50');
    // position preserved: ema instance is still first
    expect(store.indicators()[0]?.id).toBe(newId);
    expect(store.indicators()[1]?.id).toBe(rsiId);
    // override migrated to the new identity
    expect(store.colorTokenOverrides()[newId as string]).toBe('series-amber');
    expect(store.colorTokenOverrides()[emaId]).toBeUndefined();

    // param editing marks the chart stale
    expect(store.chartStale()).toBe(true);
  });

  it('updateIndicator returns null for unknown ids', () => {
    const store = createDataLabWorkspaceStore();
    expect(store.updateIndicator('nope|a=1', { a: 2 })).toBeNull();
  });

  it('updateIndicator refuses params colliding with another instance identity', () => {
    // Identity is parameter-aware: editing ema(10) into ema(50) while an
    // ema(50) instance exists would duplicate ids — refuse and keep the
    // previous state instead.
    const store = createDataLabWorkspaceStore();
    const id10 = store.addIndicator('ema', { length: 10 });
    const id50 = store.addIndicator('ema', { length: 50 });
    expect(store.updateIndicator(id10, { length: 50 })).toBeNull();
    expect(store.indicators().map(i => i.id).sort()).toEqual([id10, id50].sort());
    // A no-op edit onto the instance's own identity is fine.
    expect(store.updateIndicator(id10, { length: 10 })).toBe(id10);
  });

  it('removeIndicator drops the instance and its color override', () => {
    const store = createDataLabWorkspaceStore();
    const id = store.addIndicator('ema', { length: 10 });
    expect(store.setColorToken(id, 'series-teal')).toBe(true);
    store.removeIndicator(id);
    expect(store.indicators()).toHaveLength(0);
    expect(store.colorTokenOverrides()[id]).toBeUndefined();
  });

  it('setColorToken rejects non-token values and unknown instances', () => {
    const store = createDataLabWorkspaceStore();
    const id = store.addIndicator('ema', { length: 10 });
    expect(store.setColorToken(id, '#4d8dff' as never)).toBe(false);
    expect(store.setColorToken(id, 'var(--chart-series-blue)' as never)).toBe(false);
    expect(store.setColorToken('unknown|x=1', 'series-blue')).toBe(false);
    expect(store.colorTokenOverrides()).toEqual({});
    expect(store.setColorToken(id, 'series-blue')).toBe(true);
  });
});

describe('DataLabWorkspaceStore serialization', () => {
  it('serializes to a v2 envelope and restores it losslessly', () => {
    const store = committedStore();
    const emaId = store.addIndicator('ema', { length: 10 });
    expect(store.setColorToken(emaId, 'series-violet')).toBe(true);
    store.patchCompanions({ includeNews: true, optionsCompanionEnabled: true });
    store.setSavedSession({ id: 'sess-1', schemaVersion: 2 });

    const snapshot = store.serialize();
    expect(snapshot.schemaVersion).toBe(DATA_LAB_WORKSPACE_SCHEMA_VERSION);
    expect(snapshot.ticker).toBe('AAPL');
    expect(snapshot.windowMsUtc).toEqual(WINDOW);
    expect(snapshot.indicators).toEqual([{ id: 'ema|length=10', canonicalKey: 'ema', params: { length: 10 } }]);
    expect(snapshot.colorTokenOverrides['ema|length=10']).toBe('series-violet');
    expect(snapshot.companions.includeNews).toBe(true);
    expect(snapshot.savedSession).toEqual({ id: 'sess-1', schemaVersion: 2 });

    const restored = createDataLabWorkspaceStore();
    const result = restored.restore(snapshot);
    expect(result.warnings).toEqual([]);
    expect(restored.serialize()).toEqual(snapshot);
  });

  it('drops unknown keys and invalid entries with warnings', () => {
    const store = createDataLabWorkspaceStore();
    const result = store.restore({
      schemaVersion: 2,
      ticker: 'msft',
      windowMsUtc: WINDOW,
      indicators: [
        { canonicalKey: 'ema', params: { length: 10 } },
        { canonicalKey: 'ema', params: { length: 10 } },        // duplicate
        { params: { length: 3 } },                              // missing key
        { canonicalKey: 'rsi', params: { length: 'x' } },        // bad param
        'garbage',
      ],
      colorTokenOverrides: {
        'ema|length=10': 'series-teal',
        'weird|instance': '#ff0000' as never,                    // invalid token
        'ghost|a=1': 'series-blue',                              // unknown instance
      },
      companions: { includeNews: true, includeHacking: true },
      futureShape: { anything: true },
    });
    expect(result.warnings.some(w => w.includes('futureShape'))).toBe(true);
    expect(result.warnings.some(w => w.includes('duplicate'))).toBe(true);
    expect(result.warnings.some(w => w.includes('missing canonicalKey'))).toBe(true);
    expect(result.warnings.some(w => w.includes('invalid params'))).toBe(true);
    expect(result.warnings.some(w => w.includes('invalid color token'))).toBe(true);
    expect(result.warnings.some(w => w.includes('unknown instance'))).toBe(true);
    expect(store.committedTicker()).toBe('MSFT');
    expect(store.indicators()).toEqual([{ id: 'ema|length=10', canonicalKey: 'ema', params: { length: 10 } }]);
    expect(store.colorTokenOverrides()['ema|length=10']).toBe('series-teal');
    // unknown companion flag dropped, known flag kept
    expect(store.companions().includeNews).toBe(true);
    expect('includeHacking' in store.companions()).toBe(false);
  });

  it('refuses a foreign schemaVersion without touching state', () => {
    const store = committedStore();
    const before = store.serialize();
    const result = store.restore({ schemaVersion: 1, ticker: 'zzz' });
    expect(result.warnings[0]).toContain('schemaVersion');
    expect(store.serialize()).toEqual(before);
  });

  it('refuses an inverted window with a warning', () => {
    const store = createDataLabWorkspaceStore();
    const result = store.restore({
      schemaVersion: 2,
      ticker: 'AAPL',
      windowMsUtc: { startMsUtc: 200, endMsUtc: 100 },
    });
    expect(result.warnings.some(w => w.includes('invalid windowMsUtc'))).toBe(true);
    expect(store.committedWindow()).toBeNull();
    expect(store.committedTicker()).toBe('');
    expect(store.committedScope()).toBeNull();
  });

  it('restores a single-day window as a committed scope', () => {
    const store = createDataLabWorkspaceStore();
    const day = Date.UTC(2026, 8, 11);
    const result = store.restore({
      schemaVersion: 2,
      ticker: 'SPY',
      windowMsUtc: { startMsUtc: day, endMsUtc: day },
    });
    expect(result.warnings).toEqual([]);
    expect(store.committedWindow()).toEqual({ startMsUtc: day, endMsUtc: day });
  });

  it('restore populates the committed scope atomically from one validated object', () => {
    const store = createDataLabWorkspaceStore();
    const result = store.restore({
      schemaVersion: 2,
      ticker: 'AAPL',
      windowMsUtc: WINDOW,
      scope: { timeframe: 'hour', timespan: 'hour', multiplier: 2, session: 'extended', forwardFill: false, adjusted: false },
    });
    expect(result.warnings).toEqual([]);
    expect(store.committedTicker()).toBe('AAPL');
    expect(store.committedWindow()).toEqual(WINDOW);
    expect(store.committedScope()).toEqual({
      ticker: 'AAPL',
      window: WINDOW,
      timeframe: 'hour',
      timespan: 'hour',
      multiplier: 2,
      session: 'extended',
      forwardFill: false,
      adjusted: false,
    });
  });

  it('restore without a usable ticker+window leaves the committed scope null', () => {
    const store = createDataLabWorkspaceStore();
    const result = store.restore({
      schemaVersion: 2,
      ticker: 'AAPL',
      scope: { session: 'extended' },
    });
    expect(result.warnings).toEqual([]);
    expect(store.committedTicker()).toBe('');
    expect(store.committedWindow()).toBeNull();
    expect(store.committedScope()).toBeNull();
    // The draft still received the restored fields.
    expect(store.draft().ticker).toBe('AAPL');
    expect(store.draft().session).toBe('extended');
  });

  it('refuses a non-object payload', () => {
    const store = createDataLabWorkspaceStore();
    const result = store.restore(null);
    expect(result.warnings).toHaveLength(1);
    expect(store.committedTicker()).toBe('');
  });
});
