/* Offline source-execution review. No Angular/browser integration is claimed.
 * Actual JobsService, BackfillJobRunner, EnsureCoverageService, gate controller,
 * and panel store are transpiled in memory. Framework, HTTP, and EventSource
 * are explicit doubles; no production source or dependency file is modified.
 * Run with Node permissions denying network, child processes and all writes.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const ts = require('/Users/inkant/learn-ai/Frontend/node_modules/typescript');
const root = path.resolve(__dirname, '..');
const app = path.join(root, 'Frontend/src/app');
const providers = new Map();
const cache = new Map();
const outcomes = [];
class DataLakeService {}
class TickerCatalogService {}
class HttpClient {}
class HttpErrorResponse extends Error {}
class DestroyRef {}
function signal(value) {
  const getter = () => value;
  getter.set = (next) => { value = next; };
  getter.update = (f) => { value = f(value); };
  getter.asReadonly = () => getter;
  return getter;
}
const angular = {
  signal, computed: (f) => f, Injectable: () => (type) => type, DestroyRef,
  inject: (token) => {
    assert(providers.has(token), `Unprovided token: ${token.name}`);
    return providers.get(token);
  },
};
class FakeEventSource {
  static instances = [];
  constructor(url) { this.url = url; this.closed = false; FakeEventSource.instances.push(this); }
  close() { this.closed = true; }
  emit(frame, id = '1000-1') {
    assert(!this.closed);
    this.onmessage({ data: JSON.stringify(frame), lastEventId: id });
  }
}
function evaluate(filename, source) {
  const output = ts.transpileModule(source, {
    fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, experimentalDecorators: true },
  }).outputText;
  const module = { exports: {} };
  const sandbox = {
    module, exports: module.exports, require: (name) => imports(name, filename),
    Date, Intl, Map, Set, Promise, Error, Number, Math, JSON, String, Object,
    crypto: webcrypto, EventSource: FakeEventSource, setTimeout, clearTimeout,
  };
  vm.runInNewContext(output, sandbox, { filename });
  return module.exports;
}
function load(relative) {
  const filename = path.resolve(app, relative);
  if (!cache.has(filename)) cache.set(filename, evaluate(filename, fs.readFileSync(filename, 'utf8')));
  return cache.get(filename);
}
function extractFunction(relative, name) {
  const filename = path.join(app, relative);
  const source = fs.readFileSync(filename, 'utf8');
  const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
  const node = ast.statements.find((item) => ts.isFunctionDeclaration(item) && item.name?.text === name);
  assert(node, `Missing source function ${name}`);
  return evaluate(filename, node.getText(ast))[name];
}
function imports(name, parent) {
  if (name === '@angular/core') return angular;
  if (name === '@angular/common/http') return { HttpClient, HttpErrorResponse };
  if (name === 'rxjs') return { firstValueFrom: (value) => Promise.resolve(value) };
  const resolved = path.resolve(path.dirname(parent), name);
  if (resolved === path.join(app, 'shared/data-lake') || resolved === path.join(app, 'shared/data-lake/index')) {
    return {
      DataLakeService,
      ...load('shared/data-lake/backfill-day-event.ts'),
      ...load('shared/data-lake/trading-range.ts'),
      classifyDataLakeError: (error) => ({ kind: 'unavailable', message: error.message }),
    };
  }
  if (resolved === path.join(app, 'shared/ticker-catalog')) {
    return { TickerCatalogService, isRunnableSpan: extractFunction('shared/ticker-catalog/ticker-catalog.service.ts', 'isRunnableSpan') };
  }
  if (resolved === path.join(app, 'shared/pipes/receipt-label.pipe')) return { formatReceiptLabel: (value) => value };
  assert(resolved.startsWith(app + path.sep), `Unexpected import: ${name}`);
  return load(path.relative(app, resolved + '.ts'));
}
const { JobsService } = load('services/jobs.service.ts');
const { BackfillJobRunner } = load('shared/data-lake/backfill-job-runner.ts');
const { EnsureCoverageService, fitBackfillWindow } = load('shared/symbol-catalog/ensure-coverage.service.ts');
const { CoverageGateController } = load('shared/symbol-catalog/coverage-gate.controller.ts');
const { DataLakeBackfillStore } = load('components/data-lake-observatory/lib/data-lake-backfill.store.ts');
const range = load('shared/data-lake/trading-range.ts');
function deferred() { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; }
async function flush() { for (let n = 0; n < 24; n++) await Promise.resolve(); }
const span = (symbol) => ({ symbol, artifact_count: 1, last_trading_date_ms: Date.UTC(2026, 8, 24, 12) });
const summary = (...symbols) => ({ kind: 'ok', value: { symbols: symbols.map(span) } });
function fixture() {
  providers.clear();
  FakeEventSource.instances = [];
  const posted = [], cancelled = [], reads = [], held = new Map();
  const defaults = { kind: 'ok', value: { lean_image_digest: 'sha256:synthetic', max_trading_range_days: 1830, provider_history_start_ms: Date.UTC(2021, 8, 25, 12) } };
  const http = {
    get: async () => [],
    post: async (url, payload) => { posted.push({ url, payload }); return { id: `job-${posted.length}`, status: 'queued' }; },
    delete: async (url) => { cancelled.push(url); },
  };
  providers.set(HttpClient, http);
  const jobs = new JobsService();
  providers.set(JobsService, jobs);
  const runner = new BackfillJobRunner();
  providers.set(BackfillJobRunner, runner);
  const lake = {
    backfillDefaults: async () => defaults,
    storageSummary: async (...args) => { reads.push(args); return summary(); },
  };
  providers.set(DataLakeService, lake);
  providers.set(TickerCatalogService, { viewFor: (mode) => ({ pool: () => held.get(mode) ?? [], reload: () => {} }) });
  const service = new EnsureCoverageService();
  providers.set(EnsureCoverageService, service);
  const destroyed = [];
  providers.set(DestroyRef, { onDestroy: (callback) => destroyed.push(callback) });
  const emit = (index, event) => FakeEventSource.instances[index].emit(event);
  const complete = (index = 0) => emit(index, { type: 'job.completed' });
  return { service, runner, jobs, http, lake, defaults, posted, cancelled, reads, held, destroyed, emit, complete };
}
async function check(name, body) {
  await body();
  outcomes.push({ name, result: 'PASS' });
}
function day(index, failures = []) {
  return { type: 'data_lake.backfill_day', trading_date_ms: Date.UTC(2026, 8, 20 + index, 12), day_index: index, total_days: 3, days_remaining: 3 - index, fetched_count: failures.length ? 0 : 1, reused_count: 0, failures };
}
const fatal = { artifact_kind: 'time_series_bars', symbol: 'SYN', reason: 'provider_auth_error', detail: 'Synthetic refused credential', data_type: 'trade', trading_date_ms: Date.UTC(2026, 8, 21, 12), attempt_count: 1 };
const perDayFailure = { ...fatal, reason: 'provider_api_error', detail: 'Synthetic exhausted retry after provider 502' };

(async () => {
  await check('held pick needs no submission', async () => {
    const f = fixture(); f.held.set('raw', [{ symbol: 'SYN' }]);
    assert.equal(await f.service.ensure('SYN', 'raw').done, true); assert.equal(f.posted.length, 0);
  });
  await check('completion alone and another symbol cannot satisfy fresh coverage', async () => {
    const f = fixture(); const gate = f.service.ensure('SYN', 'raw'); await flush();
    f.held.set('raw', [{ symbol: 'SYN' }]); f.lake.storageSummary = async (...args) => { f.reads.push(args); return summary('OTHER'); };
    f.complete(); assert.equal(await gate.done, false); assert.equal(gate.state().reason, 'backfill_empty');
    assert.deepEqual(f.reads, [['usa', 'raw', 'trade']]);
  });
  await check('fresh positive span admits partial history as documented membership', async () => {
    const f = fixture(); f.lake.storageSummary = async () => summary('SYN');
    const gate = f.service.ensure('SYN', 'raw'); await flush(); f.emit(0, day(1, [perDayFailure])); f.complete();
    assert.equal(await gate.done, true); assert.equal(gate.state(), null);
    assert.equal(f.posted[0].payload.spec.symbols[0], 'SYN');
    assert.equal(f.posted[0].payload.spec.price_adjustment_mode, 'raw');
    assert.equal(f.posted[0].payload.spec.data_types.join(','), 'trade');
  });
  await check('same-symbol callers share a job and releasing one preserves the other', async () => {
    const f = fixture(); f.lake.storageSummary = async () => summary('SYN');
    const a = f.service.ensure('SYN', 'raw'), b = f.service.ensure('SYN', 'raw'); await flush();
    assert.equal(f.posted.length, 1); await a.cancel(); assert.equal(await a.done, false); assert.equal(f.cancelled.length, 0);
    f.complete(); assert.equal(await b.done, true);
  });
  await check('adjustment modes and symbols are distinct capture identities', async () => {
    const f = fixture(); f.lake.storageSummary = async (_, mode) => mode === 'raw' ? summary() : summary('SYN');
    const raw = f.service.ensure('SYN', 'raw'), adjusted = f.service.ensure('SYN', 'polygon_split_adjusted');
    const other = f.service.ensure('OTHER', 'raw'); await flush(); assert.equal(f.posted.length, 3);
    f.complete(0); f.complete(1); f.emit(2, { type: 'job.failed', code: 'synthetic_failure', message: 'Refused' });
    assert.equal(await raw.done, false); assert.equal(await adjusted.done, true); assert.equal(await other.done, false);
  });
  await check('last-caller cancellation disarms late completion', async () => {
    const f = fixture(); const gate = f.service.ensure('SYN', 'raw'); await flush(); await gate.cancel();
    assert.equal(await gate.done, false); assert.equal(gate.state(), null); assert.equal(f.cancelled[0], '/api/jobs/job-1');
    f.complete(); await flush(); assert.equal(f.reads.length, 0);
  });
  await check('cancellation during submission cancels the subsequently accepted job', async () => {
    const f = fixture(); const submitted = deferred(); f.http.post = () => submitted.promise;
    const gate = f.service.ensure('SYN', 'raw'); await flush(); await gate.cancel();
    submitted.resolve({ id: 'late-job' }); await flush();
    assert.equal(await gate.done, false); assert.equal(f.cancelled[0], '/api/jobs/late-job');
  });
  await check('old defaults resolution cannot supersede replacement gate', async () => {
    const f = fixture(); const oldDefaults = deferred(); f.lake.backfillDefaults = () => oldDefaults.promise;
    const old = f.service.ensure('SYN', 'raw'); await old.cancel();
    f.lake.backfillDefaults = async () => f.defaults; f.lake.storageSummary = async () => summary('SYN');
    const current = f.service.ensure('SYN', 'raw'); await flush(); oldDefaults.resolve(f.defaults); await flush();
    assert.equal(f.posted.length, 1); f.complete(); assert.equal(await old.done, false); assert.equal(await current.done, true);
  });
  await check('unreadable coverage refuses instead of treating absence as success', async () => {
    const f = fixture(); f.lake.storageSummary = async () => ({ kind: 'unavailable', message: 'Synthetic offline' });
    const gate = f.service.ensure('SYN', 'raw'); await flush(); f.complete();
    assert.equal(await gate.done, false); assert.equal(gate.state().reason, 'coverage_unknown');
  });
  await check('empty completion retains the latest typed root failure', async () => {
    const f = fixture(); const gate = f.service.ensure('SYN', 'raw'); await flush();
    f.emit(0, day(1, [{ ...fatal, reason: 'provider_no_data' }])); f.emit(0, day(2, [fatal, { ...fatal, reason: 'run_aborted' }]));
    f.complete(); assert.equal(await gate.done, false); assert.equal(gate.state().reason, 'provider_auth_error');
  });
  await check('controller supersession and current-mode check block stale commits', async () => {
    const f = fixture(); f.lake.storageSummary = async () => summary('FIRST', 'SECOND');
    let mode = 'raw'; const commits = []; const controller = new CoverageGateController(() => mode);
    controller.admit({ symbol: 'FIRST', mode, lakeDark: () => null, commit: (s) => commits.push(s) }); await flush();
    controller.admit({ symbol: 'SECOND', mode, lakeDark: () => null, commit: (s) => commits.push(s) }); await flush();
    f.complete(0); mode = 'polygon_split_adjusted'; f.complete(1); await flush(); assert.deepEqual(commits, []);
  });
  await check('retry re-reads lake visibility; unsupported adjustment cannot submit', async () => {
    const f = fixture(); let dark = 'Synthetic unknown'; const controller = new CoverageGateController(() => 'raw');
    controller.admit({ symbol: 'SYN', mode: 'raw', lakeDark: () => dark, commit: () => {} }); controller.retry(); await flush();
    assert.equal(f.posted.length, 0); dark = null; controller.retry(); await flush(); assert.equal(f.posted.length, 1);
    controller.abandon(); const unsupported = new CoverageGateController(() => 'lean_adjusted');
    unsupported.admit({ symbol: 'SYN', mode: 'lean_adjusted', lakeDark: () => null, commit: () => assert.fail('Unsupported committed') });
    assert.equal(unsupported.state().reason, 'view_not_backfillable'); assert.equal(f.posted.length, 1);
  });
  await check('all weekday windows fit cap and provider floor', async () => {
    for (let d = 20; d <= 26; d++) {
      const window = fitBackfillWindow(`2026-09-${d}`, 1830, '2021-09-25');
      assert(window.start >= '2021-09-25'); assert.equal(range.tradingRangeRejection(window.start, window.end, 1830), null);
    }
  });
  await check('C1 reproduced: same-tab panel reattach drops earlier receipt failures', async () => {
    const f = fixture(); const original = new DataLakeBackfillStore();
    await original.start({ symbols: ['SYN'], price_adjustment_mode: 'raw' });
    f.emit(0, { type: 'job.started' }); f.emit(0, day(1, [perDayFailure]));
    f.emit(0, { type: 'job.progress', current: 1, total: 3, unit: 'days' });
    assert.equal(original.failures().length, 1); f.destroyed[0]();
    f.emit(0, day(2));
    const returned = new DataLakeBackfillStore(); returned.reattach('job-1');
    assert.equal(FakeEventSource.instances.length, 1); assert.equal(returned.reattached(), true);
    assert.equal(returned.days().length, 0); assert.equal(returned.progress(), null);
    f.emit(0, day(3)); f.complete();
    assert.equal(returned.phase(), 'completed'); assert.equal(returned.failures().length, 0);
    assert.equal(returned.days().map((d) => d.day_index).join(','), '3'); assert.equal(returned.fetchedCount(), 1);
    outcomes.push({ evidence: 'C1', streamsOpened: 1, reattached: true, phase: returned.phase(), dayIndexes: [3], failuresVisible: 0, actualEarlierFailure: perDayFailure.reason });
  });
  await check('replayed frames restore receipts and duplicate-day folding is idempotent', async () => {
    const f = fixture(); const job = await f.jobs.startJob('data_lake_backfill', { spec: {} });
    const store = new DataLakeBackfillStore(); store.reattach(job);
    f.emit(0, day(1, [perDayFailure])); f.emit(0, day(2)); f.emit(0, day(2)); f.emit(0, day(3)); f.complete();
    assert.equal(store.days().length, 3); assert.equal(store.failures().length, 1); assert.equal(store.fetchedCount(), 2);
  });
  process.stdout.write(JSON.stringify({ sourceBaseline: '10b5f31b529c8507bd19bb24015f3d85fa9aba43', checksPassed: outcomes.filter((item) => item.result === 'PASS').length, outcomes }, null, 2) + '\n');
})().catch((error) => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
