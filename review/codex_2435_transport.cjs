/* Isolated review checks: executes unchanged pure helpers from the fixed clone.
 * No application bootstrap, HTTP client, database, or browser is loaded.
 * Run with Node's permission system; only this clone and TypeScript may be read.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require(process.env.REVIEW_TYPESCRIPT);
const root = path.resolve(__dirname, '..');
const checks = [];

function load(relative, names, dependencies = {}) {
  const filename = path.join(root, relative);
  let source = fs.readFileSync(filename, 'utf8');
  if (names) {
    // Select complete function declarations by AST, never rewrite their bodies.
    const parsed = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
    source = names.map((name) => {
      const declaration = parsed.statements.find((item) =>
        ts.isFunctionDeclaration(item) && item.name?.text === name);
      assert.ok(declaration, `Missing production function ${name}`);
      const text = declaration.getText(parsed);
      return text.startsWith('export ') ? text : `export ${text}`;
    }).join('\n');
  }
  const code = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const exports = {};
  const context = vm.createContext({ exports, Intl, Date, ...dependencies }, {
    codeGeneration: { strings: false, wasm: false },
  });
  // There is no require, process, fetch, filesystem or network in the VM.
  new vm.Script(code, { filename }).runInContext(context, { timeout: 1000 });
  return exports;
}

function check(name, callback) {
  callback();
  checks.push({ name, outcome: 'passed' });
}

const timestamps = load('Frontend/src/app/shared/timestamp/timestamp-display.ts');
check('nullable and nonfinite evidence times remain unavailable', () => {
  for (const value of [null, undefined, NaN, Infinity]) {
    assert.equal(timestamps.formatTimestampDisplay(value), '—');
  }
});
check('ET display follows spring-forward and fall-back instants', () => {
  const format = (ms) => timestamps.formatTimestampDisplay(ms, { mode: 'et' });
  assert.equal(format(Date.UTC(2026, 2, 8, 6, 59, 59)), '2026-03-08 01:59:59 ET');
  assert.equal(format(Date.UTC(2026, 2, 8, 7)), '2026-03-08 03:00:00 ET');
  assert.equal(format(Date.UTC(2026, 10, 1, 5, 30)), '2026-11-01 01:30:00 ET');
  assert.equal(format(Date.UTC(2026, 10, 1, 6, 30)), '2026-11-01 01:30:00 ET');
  assert.equal(timestamps.formatTimestampIsoInZone(Date.UTC(2026, 10, 1, 5, 30), 'America/New_York'),
    '2026-11-01T01:30:00-04:00');
  assert.equal(timestamps.formatTimestampIsoInZone(Date.UTC(2026, 10, 1, 6, 30), 'America/New_York'),
    '2026-11-01T01:30:00-05:00');
});
check('UTC calendar markers and viewer-local instants have distinct renderings', () => {
  const ms = Date.UTC(2026, 8, 24);
  assert.equal(timestamps.formatTimestampDisplay(ms, { mode: 'date-utc' }), '2026-09-24');
  assert.equal(timestamps.formatTimestampDisplay(ms, { mode: 'local', localTimeZone: 'America/Los_Angeles' }),
    '2026-09-23 17:00:00');
});
check('supported millisecond timestamps retain integer precision through JSON', () => {
  for (const ms of [0, 1772953199123, 253402300799999]) {
    assert.equal(Number.isSafeInteger(ms), true);
    assert.equal(JSON.parse(JSON.stringify({ ms })).ms, ms);
  }
});

const candles = load('Frontend/src/app/components/broker/v2-panel/lib/chart-bar-mapping.ts');
check('chart-only adapter changes milliseconds to seconds and preserves OHLC values', () => {
  const source = { start_ms: 1772953199123, open: '123.4567', high: '123.9876', low: '123.0001', close: '123.8765' };
  const candle = candles.toCandle(source);
  assert.equal(candle.time, 1772953199);
  for (const field of ['open', 'high', 'low', 'close']) assert.equal(candle[field], Number(source[field]));
  assert.equal(source.start_ms, 1772953199123);
});

const returns = load('Frontend/src/app/components/data-lab/returns-distribution/returns-distribution.service.ts', ['toStudy']);
check('returns adapter preserves null statistics, day returns, coverage and bin identity', () => {
  const dto = {
    meta: { adjustment: 'raw', warnings: ['synthetic incomplete day'], capture: null },
    coverage: { requested_sessions: 2, returned_sessions: 1, missing_sessions: 1, excluded_sessions: 0,
      first_session_open_ms_utc: 1773063000000, last_session_open_ms_utc: null },
    kinds: [{ kind: 'session', bins: [{ lower_pct: null, upper_pct: -1, count: 1, is_edge: true }],
      normal_expected_counts: [0.125], stats: { n_days: 1, mean_pct: -0.375, std_pct: null,
        annualized_vol_pct: null, skewness: null, excess_kurtosis: null, var_95_pct: -0.375, cvar_95_pct: -0.375,
        best_day: { session_open_ms_utc: 1773063000000, value_pct: -0.375 },
        worst_day: { session_open_ms_utc: 1773063000000, value_pct: -0.375 } } }],
    days: [{ session_open_ms_utc: 1773063000000, close_to_close_pct: null, session_pct: -0.375,
      overnight_pct: null, pre_market_pct: null, morning_pct: 0, afternoon_pct: -0.375, after_hours_pct: null,
      volume: 123456, bin_indices: { close_to_close: null, session: 0, overnight: null } }],
  };
  const result = returns.toStudy(dto);
  assert.equal(result.coverage.lastSessionOpenMsUtc, null);
  assert.equal(result.coverage.missingSessions, 1);
  assert.equal(result.days[0].sessionOpenMsUtc, 1773063000000);
  assert.equal(result.days[0].sessionPct, -0.375);
  assert.equal(result.days[0].closeToClosePct, null);
  assert.equal(result.days[0].morningPct, 0);
  assert.equal(result.days[0].binIndices.session, 0);
  assert.equal(result.days[0].binIndices.close_to_close, null);
  assert.equal(result.kinds[0].stats.stdPct, null);
  assert.equal(result.kinds[0].stats.annualizedVolPct, null);
  assert.equal(result.kinds[0].stats.meanPct, -0.375);
  assert.equal(result.kinds[0].normalExpectedCounts[0], 0.125);
  assert.equal(result.kinds[0].bins[0].lowerPct, null);
});

const reports = load('Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.ts', ['toEngineTrade']);
check('persisted trade values are not recomputed from displayed prices', () => {
  const source = { entryTimestamp: 1773063000123, exitTimestamp: 1773063060456,
    entryPrice: 100, exitPrice: 102, quantity: 3, pnL: -1.5, pnlPts: -0.5, pnlPct: -0.005, signalReason: 'synthetic' };
  const result = reports.toEngineTrade(source, 0);
  assert.equal(result.entry_time, source.entryTimestamp);
  assert.equal(result.exit_time, source.exitTimestamp);
  assert.equal(result.pnl_pts, -0.5);
  assert.equal(result.pnl_pct, -0.005);
  assert.equal(result.result, 'LOSS');
});
const history = load('Frontend/src/app/services/backtest-runs.types.ts', ['toRunHistoryRow', 'runWindowDate'], timestamps);
check('run history preserves unknown commission and ET trading dates', () => {
  const result = history.toRunHistoryRow({ id: 1, totalPnL: -0.12345, commissionPerOrder: null,
    brokeragePolicy: null, startDate: Date.UTC(2026, 0, 5, 5), endDate: Date.UTC(2026, 6, 6, 4) });
  assert.equal(result.totalPnl, -0.12345);
  assert.equal(result.commissionPerOrder, null);
  assert.equal(result.brokeragePolicy, null);
  assert.equal(history.runWindowDate(result.startDate), '2026-01-05');
  assert.equal(history.runWindowDate(result.endDate), '2026-07-06');
});

process.stdout.write(`${JSON.stringify({ review: 2435, checks }, null, 2)}\n`);
