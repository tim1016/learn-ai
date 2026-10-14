import { HttpErrorResponse } from '@angular/common/http';
import { signal, type Provider } from '@angular/core';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { DataLakeService, type BackfillDefaults, type DataLakeRead, type DataRunSpec } from '../../shared/data-lake';
import { BackfillJobRunner, type BackfillJobRun, type BackfillRunHooks, type BackfillRunState, type BackfillTerminalEvent } from '../../shared/data-lake/backfill-job-runner';
import { fakePickerWorld, pickSymbol } from '../../shared/symbol-picker/testing/fake-picker-world';
import { GoldenSearchPlanFormComponent } from './golden-search-plan-form.component';
import { REFUSAL_INPUTS, refusalTarget } from './golden-search-plan-problems';
import { GoldenSearchService, type CommandOutcome } from './golden-search.service';
import { etMidnightMs } from '../../shared/date/et-midnight';
import type { CreateStudyRequest, DefaultsLayout, GoldenSearchDefaults, GoldenSearchPreflight, ProtocolRequest, StrategyCapability, StudyCommandRequest, StudyDetail } from './golden-search.types';
import { defaults, emaCapability, frequencyProtocol, INCUMBENT_PARAMS, preflight as preflightFixture, protocol as protocolFixture, STUDY_POOL, studyDetail, unavailableCapability } from './testing/fixtures';

/** The defaults laid out for a four-month final test: the development range ends where the final test starts. */
function fourMonthDefaults(): GoldenSearchDefaults {
  return defaults({
    development_start_ms: etMidnightMs('2023-12-01'),
    development_end_ms: etMidnightMs('2025-12-01'),
    final_start_ms: etMidnightMs('2025-12-01'),
    final_end_ms: etMidnightMs('2026-04-01'),
  });
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((done) => (resolve = done));
  return { promise, resolve };
}

interface FakeService {
  defaults: ReturnType<typeof vi.fn<(strategyKey: string, symbol: string, layout?: DefaultsLayout) => Promise<ReturnType<typeof defaults>>>>;
  preflight: ReturnType<typeof vi.fn<(protocol: ProtocolRequest) => Promise<GoldenSearchPreflight>>>;
  createStudy: ReturnType<typeof vi.fn<(request: CreateStudyRequest) => Promise<CommandOutcome>>>;
  command: ReturnType<typeof vi.fn<(studyId: string, request: StudyCommandRequest) => Promise<CommandOutcome>>>;
}

function fakeService(): FakeService {
  return {
    defaults: vi.fn(async (_strategyKey: string, _symbol: string, _layout?: DefaultsLayout) => defaults()),
    preflight: vi.fn(async (_protocol: ProtocolRequest) => preflightFixture()),
    createStudy: vi.fn(async (_request: CreateStudyRequest) => ({ study: studyDetail('locked'), jobId: null })),
    command: vi.fn(async (_id: string, _request: StudyCommandRequest) => ({ study: studyDetail('locked', { id: 'study-0002-bbbb', parent_study_id: 'study-0001-aaaa' }), jobId: null })),
  };
}

async function renderForm(
  service: FakeService,
  options: { capabilities?: StrategyCapability[]; reviseFrom?: StudyDetail | null; debounceMs?: number; pool?: Parameters<typeof fakePickerWorld>[0]; providers?: Provider[] } = {},
) {
  const view = await render(GoldenSearchPlanFormComponent, {
    inputs: { capabilities: options.capabilities ?? [emaCapability(), unavailableCapability()], reviseFrom: options.reviseFrom ?? null, preflightDebounceMs: options.debounceMs ?? 0 },
    providers: [...fakePickerWorld(options.pool ?? STUDY_POOL).providers, { provide: GoldenSearchService, useValue: service }, ...(options.providers ?? [])],
  });
  const locked = vi.fn();
  view.fixture.componentInstance.locked.subscribe(locked);
  return { view, locked };
}

async function pickSpy(service: FakeService, view: Awaited<ReturnType<typeof renderForm>>['view']): Promise<void> {
  pickSymbol(view.fixture, 'SPY');
  await waitFor(() => expect(screen.getByRole('table', { name: /knobs, in search order/i })).not.toBeNull());
  await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());
}

/** The dates the server lays out inside two years ending on the fake lake's last day, with what it left out. */
const TWO_YEAR_NOTE =
  'Development starts on 2024-10-19. The 21 sessions from 2024-09-19 to 2024-10-18 are left out: the months before the final test have to split into 6 months of training and whole tests of 2 months.';

function twoYearDefaults(): GoldenSearchDefaults {
  return defaults({
    development_start_ms: etMidnightMs('2024-10-19'),
    development_end_ms: etMidnightMs('2026-06-19'),
    final_start_ms: etMidnightMs('2026-06-19'),
    final_end_ms: etMidnightMs('2026-09-19'),
    range_note: TWO_YEAR_NOTE,
  });
}

/** What the picker's 2Y hands over in the fake world: two years ending on the lake's last day, 2026-09-18. */
const TWO_YEARS = { range_start_ms: etMidnightMs('2024-09-19'), range_end_ms: etMidnightMs('2026-09-19') };

function quickRange(key: string): HTMLElement {
  return within(screen.getByRole('group', { name: 'Quick ranges' })).getByText(key, { exact: true });
}

/** The lake stops on 2026-03-20; the default plan's final test runs through 2026-03-31. */
const LAGGING_POOL = [{ ...STUDY_POOL[0], lastHeld: '2026-03-20' }];
const MISSING_SESSIONS =
  'missing data: SPY has no minute bars for 7 of 564 trading sessions in 2023-12-28..2026-03-31 — missing 2026-03-23..2026-03-31 (read from the polygon_split_adjusted lake); backfill SPY in polygon_split_adjusted mode and launch again';
/** A backfill's window is calendar-anchored: noon UTC of each day. */
const MISSING_WINDOW = { start_trading_date_ms: Date.UTC(2026, 2, 23, 12), end_trading_date_ms: Date.UTC(2026, 2, 31, 12) };

/** The plan check's refusal for those sessions: the backfill that fetches them, and the last day the lake serves instead. */
function missingSessions(remedy = true): GoldenSearchPreflight {
  const backfill = remedy ? { symbol: 'SPY', price_adjustment_mode: 'polygon_split_adjusted' as const, ...MISSING_WINDOW } : null;
  return preflightFixture({ refusals: [{ code: 'DATA_MISSING', field: 'final_end_ms', message: MISSING_SESSIONS, nearest_valid_ms: [etMidnightMs('2026-03-21')], backfill }] });
}

const BACKFILL_DEFAULTS: DataLakeRead<BackfillDefaults> = {
  kind: 'ok',
  value: { market: 'usa', lean_image_digest: 'sha256:lean', max_trading_range_days: 1830, max_symbol_length: 16, provider_history_start_ms: 0 },
};

/** The lake's backfill job, which a spec reports on and ends by hand. */
function fakeBackfill(defaultsRead: DataLakeRead<BackfillDefaults> = BACKFILL_DEFAULTS) {
  const terminal = deferred<BackfillTerminalEvent>();
  const state = signal<BackfillRunState>({ kind: 'running', started: true, progress: null });
  let hooks: BackfillRunHooks | undefined;
  // Stopping to watch a job ends this form's wait for it, as the real runner's detach does.
  const detach = vi.fn(() => terminal.resolve({ kind: 'detached' }));
  const cancel = vi.fn(async () => undefined);
  const start = vi.fn(async (_spec: DataRunSpec, given?: BackfillRunHooks): Promise<BackfillJobRun> => {
    hooks = given;
    return { jobId: 'job-1', state: state.asReadonly(), terminal: terminal.promise, detach, cancel };
  });
  return {
    providers: [{ provide: BackfillJobRunner, useValue: { start } }, { provide: DataLakeService, useValue: { backfillDefaults: async () => defaultsRead } }] satisfies Provider[],
    start,
    detach,
    cancel,
    progress: (current: number, total: number) => state.set({ kind: 'running', started: true, progress: { current, total, unit: 'days', message: null } }),
    /** One day of the run that fetched nothing, with the provider's reason. */
    dayFailed: (detail: string) =>
      hooks?.onDomainEvent({ type: 'data_lake.backfill_day', trading_date_ms: MISSING_WINDOW.start_trading_date_ms, day_index: 1, total_days: 7, failures: [{ artifact_kind: 'time_series_bars', symbol: 'SPY', reason: 'provider_no_data', detail }] }),
    end: (event: BackfillTerminalEvent) => terminal.resolve(event),
  };
}

function backfillButton(): HTMLButtonElement {
  return within(screen.getByRole('list', { name: /cannot be locked/i })).getByRole('button', { name: 'Backfill SPY' }) as HTMLButtonElement;
}

/** The dates the plan ends up with, as the line under the bar tells them. */
function planDates(container: Element): string[] {
  return [...container.querySelectorAll('.spans > span')].map((span) => (span.textContent ?? '').replace(/\s+/g, ' ').trim());
}

function lockButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /lock/i }) as HTMLButtonElement;
}

function runButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Run research' }) as HTMLButtonElement;
}

describe('GoldenSearchPlanFormComponent', () => {
  it('shows editable expected trade frequency and locks the selected annual policy with the server preview', async () => {
    const service = fakeService();
    const { policy, exam_min_trades, expected_trades_per_year } = frequencyProtocol();
    service.defaults.mockResolvedValue(defaults({ policy, exam_min_trades, expected_trades_per_year }));
    service.preflight.mockImplementation(async (protocol) => preflightFixture({
      activity: {
        expected_trades_per_year: protocol.expected_trades_per_year ?? 50,
        windows: [{ key: 'development', label: 'Development', start_ms: protocol.development_start_ms, end_ms: protocol.development_end_ms, trading_sessions: 501, minimum_trades: protocol.expected_trades_per_year === 100 ? 200 : 100, years: [] }],
      },
    }));
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const frequency = screen.getByRole('spinbutton', { name: 'Expected trade frequency' });
    expect((frequency as HTMLInputElement).value).toBe('50');
    expect(screen.getByText('At least 100 trades')).not.toBeNull();
    expect(screen.queryByLabelText('Min completed trades')).toBeNull();
    expect(screen.queryByLabelText('Min final-test trades')).toBeNull();

    fireEvent.input(frequency, { target: { value: '100' } });
    await waitFor(() => expect(screen.getByText('At least 200 trades')).not.toBeNull());
    await waitFor(() => expect(lockButton().disabled).toBe(false));
    fireEvent.click(lockButton());
    await waitFor(() => expect(service.createStudy).toHaveBeenCalledTimes(1));
    expect(service.createStudy.mock.lastCall?.[0].protocol.expected_trades_per_year).toBe(100);
  });

  it('offers only declared strategies and names why the others cannot be studied yet', async () => {
    await renderForm(fakeService());

    const picker = screen.getByRole('combobox', { name: 'Strategy' }) as HTMLSelectElement;
    const options = Array.from(picker.options);
    expect(options.filter((o) => !o.disabled).map((o) => o.value)).toEqual(['ema_crossover_signal']);
    const unavailable = options.filter((o) => o.disabled).map((o) => o.textContent ?? '');
    expect(unavailable).toHaveLength(1);
    expect(unavailable[0]).toContain('SMA Crossover');
    expect(unavailable[0]).toContain('No Golden Search declaration has shipped');
  });

  it('clears obsolete floor errors when a revised plan adopts expected trade frequency', async () => {
    const service = fakeService();
    await renderForm(service, { reviseFrom: studyDetail('awaiting_validation') });
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());

    fireEvent.input(screen.getByLabelText('Min completed trades'), { target: { value: '0' } });
    fireEvent.input(screen.getByLabelText('Min final-test trades'), { target: { value: '1.5' } });
    await waitFor(() => expect(lockButton().disabled).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Use expected trade frequency (50 per year)' }));

    await waitFor(() => expect(lockButton().disabled).toBe(false));
    expect((screen.getByRole('spinbutton', { name: 'Expected trade frequency' }) as HTMLInputElement).value).toBe('50');
    expect(screen.queryByLabelText('Min completed trades')).toBeNull();
    expect(screen.queryByLabelText('Min final-test trades')).toBeNull();
    expect(service.preflight.mock.lastCall?.[0]).toMatchObject({ expected_trades_per_year: 50, policy: { min_trades: null }, exam_min_trades: null });
  });

  it('loads the defaults for the strategy and instrument, then shows every knob, fixed control and constraint', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);

    await pickSpy(service, view);

    expect(service.defaults).toHaveBeenCalledWith('ema_crossover_signal', 'SPY');
    const table = screen.getByRole('table', { name: /knobs, in search order/i });
    for (const label of ['Crossover gap', 'RSI lower gate', 'Fast EMA length', 'Hold time', 'Crossover gap (bps)']) expect(table.textContent).toContain(label);
    expect(screen.getByRole('list', { name: 'Fixed controls' }).textContent).toContain('RSI length');
    expect(screen.getByRole('list', { name: 'Constraints' }).textContent).toContain('Fast EMA length < Slow EMA length');
    expect(screen.getByText(/registry validated settings/i)).not.toBeNull();
    expect(screen.getByText('Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars')).not.toBeNull();
  });

  it('starts every knob at importance 5; a higher score moves the knob to the top, keeps focus, and locks with the plan', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const order = (): string[] => screen.getAllByRole('rowheader').map((header) => header.textContent?.trim().split('\n')[0] ?? '');
    expect(screen.getAllByRole('combobox', { name: / importance$/ }).map((select) => (select as HTMLSelectElement).value)).toEqual(Array(7).fill('5'));
    expect(order()[0]).toMatch(/^Crossover gap/);

    const hold = screen.getByRole('combobox', { name: 'Hold time importance' });
    fireEvent.change(hold, { target: { value: '9' } });
    await waitFor(() => expect(order()[0]).toMatch(/^Hold time/));
    await waitFor(() => expect(document.activeElement?.id).toBe(hold.id));

    await waitFor(() => expect(lockButton().disabled).toBe(false));
    fireEvent.click(lockButton());
    await waitFor(() => expect(service.createStudy).toHaveBeenCalledTimes(1));
    const knobs = service.createStudy.mock.lastCall?.[0].protocol.knobs ?? [];
    expect(knobs.map((knob) => [knob.name, knob.importance]).slice(0, 2)).toEqual([['hold_bars', 9], ['gap', 5]]);
  });

  it('ranks a revised legacy plan at importance 5 and says the order changed', async () => {
    const service = fakeService();
    const legacyKnobs = [...protocolFixture().knobs].reverse().map(({ importance: _importance, ...knob }) => knob);
    await renderForm(service, { reviseFrom: studyDetail('awaiting_validation', { protocol: protocolFixture({ knobs: legacyKnobs }) }) });
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());

    expect(screen.getByRole('note').textContent).toContain('this revision ranks them by importance, starting every knob at 5');
    expect(screen.getAllByRole('rowheader')[0].textContent).toContain('Crossover gap');
    expect(service.preflight.mock.lastCall?.[0].knobs.every((knob) => knob.importance === 5)).toBe(true);
  });

  it('Run research locks the plan with the intent to run on to Compare; Lock only does not', async () => {
    const service = fakeService();
    const { view, locked } = await renderForm(service);
    await pickSpy(service, view);
    await waitFor(() => expect(runButton().disabled).toBe(false));
    expect(runButton().getAttribute('aria-describedby')).toBe('gs-run-research-scope');
    expect(screen.getByText(/runs Search and Test over time on the server, and stops at Compare/)).not.toBeNull();

    fireEvent.click(runButton());
    await waitFor(() => expect(locked).toHaveBeenCalledTimes(1));
    expect(service.createStudy.mock.lastCall?.[0]).toMatchObject({ run_research: true });

    fireEvent.click(lockButton());
    await waitFor(() => expect(service.createStudy).toHaveBeenCalledTimes(2));
    expect(service.createStudy.mock.lastCall?.[0]).toMatchObject({ run_research: false });
  });

  it('Run research on a revision runs the new study under a key derived from the revision', async () => {
    const service = fakeService();
    const revised = studyDetail('locked', { id: 'study-0002-bbbb', revision: 0, parent_study_id: 'study-0001-aaaa' });
    service.command.mockResolvedValueOnce({ study: revised, jobId: null }).mockResolvedValueOnce({ study: studyDetail('search_running', { id: 'study-0002-bbbb' }), jobId: 'job-9' });
    await renderForm(service, { reviseFrom: studyDetail('awaiting_validation') });
    await waitFor(() => expect(runButton().disabled).toBe(false));

    fireEvent.click(runButton());
    await waitFor(() => expect(service.command).toHaveBeenCalledTimes(2));
    const [[, revise], [target, run]] = service.command.mock.calls;
    expect(revise.command).toBe('revise');
    expect(target).toBe('study-0002-bbbb');
    expect(run).toMatchObject({ command: 'run_research', expected_revision: 0, idempotency_key: `${revise.idempotency_key}:run` });
  });

  it('shows where the plan sits in the research process', async () => {
    await renderForm(fakeService());

    const process = screen.getByRole('list', { name: 'Research process' });
    expect(within(process).getAllByRole('listitem').map((item) => item.textContent?.trim())).toEqual(['1 Plan', '2 Research', '3 Compare', '4 Final test', '5 Decision', '6 Deploy']);
    expect(process.querySelector('[aria-current="step"]')?.textContent).toContain('Plan');
  });

  it('debounces a burst of edits into one preflight of the latest plan', async () => {
    const service = fakeService();
    const { view } = await renderForm(service, { debounceMs: 40 });
    await pickSpy(service, view);

    const low = screen.getByLabelText('Fast EMA length low');
    fireEvent.input(low, { target: { value: '4' } });
    fireEvent.input(low, { target: { value: '6' } });
    expect(lockButton().disabled).toBe(true);

    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    await new Promise((done) => setTimeout(done, 80));
    expect(service.preflight).toHaveBeenCalledTimes(2);
    expect(service.preflight.mock.lastCall?.[0].knobs.find((k) => k.name === 'fast_period')?.low).toBe(6);
  });

  it('ignores a preflight answer that arrives after a newer edit', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const slow = deferred<GoldenSearchPreflight>();
    service.preflight.mockImplementationOnce(() => slow.promise);

    const ceiling = screen.getByLabelText(/max drawdown/i);
    fireEvent.input(ceiling, { target: { value: '15' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    fireEvent.input(ceiling, { target: { value: '25' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());

    slow.resolve(preflightFixture({ refusals: [{ code: 'DRAWDOWN_CEILING_INVALID', field: 'policy', message: 'stale answer' }] }));
    await view.fixture.whenStable();

    expect(screen.queryByText(/stale answer/)).toBeNull();
    expect(lockButton().disabled).toBe(false);
    expect(service.preflight.mock.lastCall?.[0].policy.max_drawdown_ceiling).toBe(0.25);
  });

  it('lists every refusal in plain words and keeps Lock disabled', async () => {
    const service = fakeService();
    service.preflight.mockImplementation(async () =>
      preflightFixture({
        refusals: [
          { code: 'WORKLOAD_LIMIT', field: 'budget_cap', message: 'At most 6,200 runs exceed the cap of 5,000.' },
          { code: 'FOLDS_INVALID', field: 'training_months', message: 'The development range holds no whole fold.' },
        ],
        estimate: null,
      }),
    );
    const { view } = await renderForm(service);
    pickSymbol(view.fixture, 'SPY');

    const refusals = await screen.findByRole('list', { name: /cannot be locked/i });
    expect(screen.getByText('2 problems to fix before locking')).not.toBeNull();
    expect(refusals.textContent).not.toContain('WORKLOAD_LIMIT');
    expect(refusals.textContent).toContain('6,200 runs exceed the cap');
    expect(refusals.textContent).toContain('no whole fold');
    expect(lockButton().disabled).toBe(true);
  });

  it('does not preflight a value it cannot read, and says which one to fix', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    fireEvent.input(screen.getByLabelText(/run cap/i), { target: { value: '' } });
    await view.fixture.whenStable();

    expect(screen.getByRole('list', { name: /values to fix/i }).textContent).toContain('Run cap: Enter a number.');
    expect(screen.getByText(/some values cannot be read yet/i)).not.toBeNull();
    expect(lockButton().disabled).toBe(true);
    await new Promise((done) => setTimeout(done, 20));
    expect(service.preflight).toHaveBeenCalledTimes(1);
  });

  it('sends a smallest step for every searched knob, and a knob turned to Search starts at its declared step', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    expect(screen.getByLabelText('Fast EMA length smallest step')).not.toBeNull();
    fireEvent.click(screen.getByRole('switch', { name: 'Vary Crossover gap (bps)' }));
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('radio', { name: /grid search/i }));
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(3));

    const sent = service.preflight.mock.lastCall?.[0];
    expect(sent?.method).toBe('grid');
    expect(sent?.knobs.filter((k) => k.mode === 'search').map((k) => [k.name, k.step])).toEqual([
      ['gap', 0.05],
      ['rsi_min', 1],
      ['rsi_max', 1],
      ['fast_period', 1],
      ['slow_period', 1],
      ['hold_bars', 1],
      ['gap_bps', 0.5],
    ]);
    expect(sent?.knobs.some((k) => 'grid_step' in k)).toBe(false);
  });

  it('says when a searched knob starts outside its range, a knob the seed omits starting at its declared default, without refusing the plan', async () => {
    const service = fakeService();
    service.defaults.mockResolvedValueOnce(defaults({ seed: { ...INCUMBENT_PARAMS, fast_period: 2 } }));
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const knobRow = (name: string): string => screen.getByRole('rowheader', { name: new RegExp(name, 'i') }).closest('tr')?.textContent ?? '';
    const note = /is outside this range\. It stays the answer only if nothing in the range beats it\./;

    expect(knobRow('Fast EMA length')).toMatch(/Starting value 2 is outside this range/);
    expect(knobRow('Slow EMA length')).not.toMatch(note);

    fireEvent.input(screen.getByLabelText('Fast EMA length low'), { target: { value: '2' } });
    fireEvent.input(screen.getByLabelText('Slow EMA length low'), { target: { value: '12' } });
    await waitFor(() => expect(knobRow('Fast EMA length')).not.toMatch(note));
    expect(knobRow('Slow EMA length')).toMatch(/Starting value 10 is outside this range/);
    await waitFor(() => expect(lockButton().disabled).toBe(false));
  });

  it('holding a knob with Vary off sends it held at its starting value, and its pair audit waits until it varies again', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    fireEvent.click(screen.getByRole('switch', { name: 'Vary Fast EMA length' }));
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));

    expect((screen.getByLabelText('Fast EMA length held value') as HTMLInputElement).value).toBe('5');
    const sent = service.preflight.mock.lastCall?.[0];
    expect(sent?.knobs.find((k) => k.name === 'fast_period')).toMatchObject({ mode: 'fixed', fixed_value: 5 });
    expect(sent?.pair_audits).toEqual([['rsi_min', 'rsi_max']]);
    expect(screen.getByText(/off while Fast EMA length is held/)).not.toBeNull();

    fireEvent.click(screen.getByRole('switch', { name: 'Vary Fast EMA length' }));
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(3));
    expect(service.preflight.mock.lastCall?.[0].pair_audits).toEqual([['fast_period', 'slow_period'], ['rsi_min', 'rsi_max']]);
  });

  it('a range whose ends meet holds the knob at that value instead of being refused', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    fireEvent.input(screen.getByLabelText('Fast EMA length low'), { target: { value: '5' } });
    fireEvent.input(screen.getByLabelText('Fast EMA length high'), { target: { value: '5' } });
    await waitFor(() => expect(service.preflight.mock.lastCall?.[0].knobs.find((k) => k.name === 'fast_period')?.mode).toBe('fixed'));

    expect(service.preflight.mock.lastCall?.[0].knobs.find((k) => k.name === 'fast_period')?.fixed_value).toBe(5);
    expect(screen.getByText('Same low and high: held at 5.')).not.toBeNull();
    expect(screen.queryByLabelText('Fast EMA length smallest step')).toBeNull();
    await waitFor(() => expect(lockButton().disabled).toBe(false));
  });

  it("shows each knob's golden value and the server's count of its settings", async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const table = screen.getByRole('table', { name: /knobs, in search order/i });
    const columns = Array.from(table.querySelectorAll('thead th')).map((header) => header.textContent?.trim());
    const cell = (knob: string, column: string): string => {
      const row = screen.getByRole('rowheader', { name: new RegExp(knob) }).closest('tr');
      return row?.children[columns.indexOf(column)]?.textContent?.trim() ?? '';
    };

    expect(cell('Crossover gap price', 'Golden')).toBe('0.2');
    expect(cell('Crossover gap price', 'Values')).toBe('13');
    expect(cell('Slow EMA length', 'Golden')).toBe('10');
    expect(cell('Slow EMA length', 'Values')).toBe('23');
  });

  // A plan shows either its expected trade frequency or its two fixed floors, so the hidden fields are never refused.
  it.each([
    { plan: 'a fixed-floor plan', policy: undefined, hidden: ['expected_trades_per_year'] },
    { plan: 'a frequency plan', policy: frequencyProtocol(), hidden: ['policy.min_trades', 'exam_min_trades'] },
  ])('every input the form links a refusal to is rendered, for $plan', async ({ policy, hidden }) => {
    const service = fakeService();
    if (policy) service.defaults.mockResolvedValue(defaults({ policy: policy.policy, exam_min_trades: policy.exam_min_trades, expected_trades_per_year: policy.expected_trades_per_year }));
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const knobs = defaults().knobs;
    const fields = [...Object.keys(REFUSAL_INPUTS).filter((field) => !hidden.includes(field)), ...knobs.flatMap((knob) => [`knobs.${knob.name}`, `seed.${knob.name}`, `knobs.${knob.name}.importance`, ...(knob.mode === 'search' ? [`knobs.${knob.name}.step`] : [])])];

    const missing = fields.filter((field) => {
      const target = refusalTarget(field, knobs);
      return target === null || view.container.querySelector(`[id="${target}"]`) === null;
    });

    expect(missing).toEqual([]);
  });

  it('says when the final test ends early because the lake has not reached its last sessions', async () => {
    const service = fakeService();
    service.defaults.mockResolvedValueOnce(defaults({ final_sessions_cut: 3, final_end_ms: etMidnightMs('2026-03-28') }));
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    expect(screen.getByRole('note').textContent).toMatch(/3 scheduled sessions short of 3 whole months/);
  });

  it('a qualified benchmark is named as the benchmark, with the search starting from the registry settings', async () => {
    const service = fakeService();
    service.defaults.mockResolvedValueOnce(defaults({ incumbent: { source: 'qualification', qualification_id: 'q-1', params: { ...INCUMBENT_PARAMS, hold_bars: 6 } }, incumbent_label: 'Golden configuration q-1' }));
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    expect(screen.getByText(/Benchmark · Golden configuration q-1/)).not.toBeNull();
    expect(screen.getByText(/Searches start from the registry settings/)).not.toBeNull();
    expect(screen.getByRole('rowheader', { name: /Hold time/ }).closest('tr')?.textContent).toContain('starts at 5');
  });

  it('a registry benchmark with an edited held value is the benchmark, without the qualification sentence', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    expect(screen.getByText(/Start and benchmark · Registry validated settings/)).not.toBeNull();

    fireEvent.click(screen.getByRole('switch', { name: 'Vary Fast EMA length' }));
    fireEvent.input(screen.getByLabelText('Fast EMA length held value'), { target: { value: '6' } });

    await waitFor(() => expect(screen.getByText(/Benchmark · Registry validated settings/)).not.toBeNull());
    expect(screen.queryByText(/Searches start from the registry settings/)).toBeNull();
  });

  it("shows a knob's refusal on its row, and the footer's link takes the trader to the field", async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    service.preflight.mockResolvedValueOnce(preflightFixture({ refusals: [{ code: 'RANGE_OUTSIDE_DOMAIN', field: 'knobs.slow_period', message: 'Slow EMA length: 8–41 leaves the legal domain 3–40 decision bars.' }] }));

    fireEvent.input(screen.getByLabelText('Slow EMA length high'), { target: { value: '41' } });
    const refusals = await screen.findByRole('list', { name: /cannot be locked/i });

    const row = screen.getByRole('rowheader', { name: /Slow EMA length/ }).closest('tr')?.textContent ?? '';
    expect(row).toContain('8–41 leaves the legal domain 3–40 decision bars.');
    expect(screen.getByLabelText('Slow EMA length high').getAttribute('aria-invalid')).toBe('true');
    fireEvent.click(within(refusals).getByRole('button', { name: /leaves the legal domain/ }));
    expect(document.activeElement).toBe(screen.getByLabelText('Slow EMA length low'));
    expect(lockButton().disabled).toBe(true);
  });

  it('a footer link to a folded field unfolds its section first', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    service.preflight.mockResolvedValueOnce(preflightFixture({ refusals: [{ code: 'WORKLOAD_LIMIT', field: 'budget_cap', message: 'At most 6,200 runs exceed the cap of 5,000.' }] }));

    fireEvent.input(screen.getByLabelText(/max drawdown/i), { target: { value: '25' } });
    const refusals = await screen.findByRole('list', { name: /cannot be locked/i });
    const cap = screen.getByLabelText(/run cap/i);
    expect(cap.closest('details')?.open).toBe(false);

    fireEvent.click(within(refusals).getByRole('button', { name: /exceed the cap/ }));

    expect(cap.closest('details')?.open).toBe(true);
    expect(document.activeElement).toBe(cap);
  });

  it('Reset to defaults restores the knob table the plan started with', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    fireEvent.click(screen.getByRole('switch', { name: 'Vary Fast EMA length' }));
    fireEvent.input(screen.getByLabelText('Hold time high'), { target: { value: '20' } });
    await waitFor(() => expect(service.preflight.mock.lastCall?.[0].knobs.find((k) => k.name === 'hold_bars')?.high).toBe(20));

    fireEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }));

    await waitFor(() => expect(service.preflight.mock.lastCall?.[0].knobs).toEqual(defaults().knobs));
    expect((screen.getByRole('switch', { name: 'Vary Fast EMA length' }) as HTMLInputElement).checked).toBe(true);
  });

  it('locks the checked plan and reports its study; a retry after no answer reuses the idempotency key', async () => {
    const service = fakeService();
    service.createStudy.mockRejectedValueOnce(new Error('network down'));
    const { view, locked } = await renderForm(service);
    await pickSpy(service, view);

    fireEvent.click(lockButton());
    await screen.findByText(/not confirmed as locked/i);
    fireEvent.click(lockButton());
    await waitFor(() => expect(locked).toHaveBeenCalledWith('study-0001-aaaa'));

    const [first, second] = service.createStudy.mock.calls.map(([request]) => request);
    expect(first.protocol).toEqual(service.preflight.mock.lastCall?.[0]);
    expect(first.idempotency_key).toBeTruthy();
    expect(second.idempotency_key).toBe(first.idempotency_key);
  });

  it('revises a frozen plan into a new study without reloading defaults', async () => {
    const service = fakeService();
    const source = studyDetail('awaiting_validation');
    const { locked } = await renderForm(service, { reviseFrom: source });

    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());
    expect(service.defaults).not.toHaveBeenCalled();
    expect(screen.queryByRole('combobox', { name: 'Strategy' })).toBeNull();
    fireEvent.input(screen.getByLabelText('Hold time high'), { target: { value: '10' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(lockButton().disabled).toBe(false));

    fireEvent.click(screen.getByRole('button', { name: /lock as a new study/i }));

    await waitFor(() => expect(locked).toHaveBeenCalledWith('study-0002-bbbb'));
    const [studyId, request] = service.command.mock.calls[0];
    expect(studyId).toBe(source.id);
    expect(request.command).toBe('revise');
    expect(request.expected_revision).toBe(source.revision);
    expect(request.command === 'revise' ? request.payload.protocol.knobs.find((k) => k.name === 'hold_bars')?.high : null).toBe(10);
    expect(service.createStudy).not.toHaveBeenCalled();
  });

  it('leaving revise mode starts a fresh plan from the current defaults, not the old study frozen plan', async () => {
    const service = fakeService();
    const frozen = studyDetail('awaiting_validation');
    const source = { ...frozen, protocol: { ...frozen.protocol, incumbent: { source: 'qualification' as const, qualification_id: 'q-old-0001', params: { gap: 0.3, symbol: 'SPY' } } } };
    const { view } = await renderForm(service, { reviseFrom: source });
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());

    view.fixture.componentRef.setInput('reviseFrom', null);

    await waitFor(() => expect(service.defaults).toHaveBeenCalledWith('ema_crossover_signal', 'SPY'));
    await waitFor(() => expect(service.preflight.mock.lastCall?.[0].incumbent.source).toBe('registry'));
    expect(screen.getByRole('combobox', { name: 'Strategy' })).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Lock only' })).not.toBeNull();
  });

  it('changing the final-test months has the server lay the dates out again, keeping every other edit', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    fireEvent.input(screen.getByLabelText('Fast EMA length low'), { target: { value: '4' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    service.defaults.mockResolvedValueOnce(fourMonthDefaults());

    fireEvent.input(screen.getByLabelText('Final test (months)'), { target: { value: '4' } });

    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(3));
    expect(service.defaults.mock.lastCall).toEqual(['ema_crossover_signal', 'SPY', { final_months: 4, training_months: 6, test_months: 2 }]);
    const sent = service.preflight.mock.lastCall?.[0];
    expect(sent?.final_start_ms).toBe(etMidnightMs('2025-12-01'));
    expect(sent?.development_end_ms).toBe(etMidnightMs('2025-12-01'));
    expect(sent?.final_end_ms).toBe(etMidnightMs('2026-04-01'));
    expect(sent?.knobs.find((k) => k.name === 'fast_period')?.low).toBe(4);
    await waitFor(() => expect(lockButton().disabled).toBe(false));
  });

  it('starts the final test at the length the server laid out, and never sends the fields that only describe the plan', async () => {
    const service = fakeService();
    service.defaults.mockResolvedValueOnce(defaults({ final_months: 6 }));
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    expect((screen.getByLabelText('Final test (months)') as HTMLInputElement).value).toBe('6');
    const describing = (sent: object | undefined): string[] => Object.keys(sent ?? {}).filter((key) => ['final_months', 'final_sessions_cut', 'range_note', 'incumbent_label', 'incumbent_sentence', 'exposure'].includes(key));
    expect(describing(service.preflight.mock.lastCall?.[0])).toEqual([]);

    fireEvent.input(screen.getByLabelText('Test window (months)'), { target: { value: '3' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    expect(service.defaults.mock.lastCall?.[2]).toEqual({ final_months: 6, training_months: 6, test_months: 3 });
    expect(describing(service.preflight.mock.lastCall?.[0])).toEqual([]);

    await waitFor(() => expect(lockButton().disabled).toBe(false));
    fireEvent.click(lockButton());
    await waitFor(() => expect(service.createStudy).toHaveBeenCalledTimes(1));
    expect(describing(service.createStudy.mock.calls[0][0].protocol)).toEqual([]);
  });

  it('holds the plan back while the dates are laid out, and ignores dates laid out for older month counts', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const older = deferred<GoldenSearchDefaults>();
    const newer = deferred<GoldenSearchDefaults>();
    service.defaults.mockImplementationOnce(() => older.promise).mockImplementationOnce(() => newer.promise);

    fireEvent.input(screen.getByLabelText('Training window (months)'), { target: { value: '9' } });
    await waitFor(() => expect(service.defaults).toHaveBeenCalledTimes(2));
    fireEvent.input(screen.getByLabelText('Training window (months)'), { target: { value: '12' } });
    await waitFor(() => expect(service.defaults).toHaveBeenCalledTimes(3));

    expect(screen.getByText('Laying out the dates…')).not.toBeNull();
    expect(lockButton().disabled).toBe(true);
    newer.resolve(fourMonthDefaults());
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    older.resolve(defaults({ final_start_ms: etMidnightMs('2020-01-01') }));
    await view.fixture.whenStable();

    expect(service.defaults.mock.calls[2][2]).toEqual({ final_months: 3, training_months: 12, test_months: 2 });
    expect(service.preflight.mock.lastCall?.[0].final_start_ms).toBe(etMidnightMs('2025-12-01'));
    expect(service.preflight.mock.lastCall?.[0].training_months).toBe(12);
    expect(service.preflight).toHaveBeenCalledTimes(2);
  });

  it('shows the first and last day of the plan it opened with, and offers only the ranges a study can use (#2886)', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    expect((screen.getByRole('textbox', { name: 'Start date' }) as HTMLInputElement).value).toBe('2024-01-01');
    expect((screen.getByRole('textbox', { name: 'End date' }) as HTMLInputElement).value).toBe('2026-03-31');
    expect(within(screen.getByRole('group', { name: 'Quick ranges' })).getAllByRole('button').map((button) => button.textContent?.trim())).toEqual(['6M', '1Y', '2Y', 'Max']);
    expect(planDates(view.container)).toEqual(['Development 2024-01-01 to 2025-12-31', 'Final test 2026-01-01 to 2026-03-31']);
    // No range has been picked, so the server was asked for its own layout.
    expect(service.defaults.mock.lastCall).toEqual(['ema_crossover_signal', 'SPY']);
  });

  it('a picked range is laid out by the server inside it, and the page says what the plan left out (#2886)', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    service.defaults.mockResolvedValueOnce(twoYearDefaults());

    fireEvent.click(quickRange('2Y'));

    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    expect(service.defaults.mock.lastCall).toEqual(['ema_crossover_signal', 'SPY', { final_months: 3, training_months: 6, test_months: 2, ...TWO_YEARS }]);
    const sent = service.preflight.mock.lastCall?.[0];
    expect([sent?.development_start_ms, sent?.final_start_ms, sent?.final_end_ms]).toEqual([etMidnightMs('2024-10-19'), etMidnightMs('2026-06-19'), etMidnightMs('2026-09-19')]);
    expect(screen.getByRole('note').textContent).toBe(TWO_YEAR_NOTE);
    // The picker keeps the range that was picked; the bar shows what the plan uses of it.
    expect((screen.getByRole('textbox', { name: 'Start date' }) as HTMLInputElement).value).toBe('2024-09-19');
    expect(quickRange('2Y').getAttribute('aria-pressed')).toBe('true');
    expect(planDates(view.container)).toEqual(['Development 2024-10-19 to 2026-06-18', 'Final test 2026-06-19 to 2026-09-18']);
    await waitFor(() => expect(lockButton().disabled).toBe(false));
  });

  it('a month count changed after a range is picked is laid out inside that range (#2886)', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    service.defaults.mockResolvedValueOnce(twoYearDefaults());
    fireEvent.click(quickRange('2Y'));
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));

    fireEvent.input(screen.getByLabelText('Test window (months)'), { target: { value: '1' } });

    await waitFor(() => expect(service.defaults).toHaveBeenCalledTimes(3));
    expect(service.defaults.mock.lastCall?.[2]).toEqual({ final_months: 3, training_months: 6, test_months: 1, ...TWO_YEARS });
  });

  it('holds the plan on a half-typed day: the range is not laid out, and the footer names the range (#2886)', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const start = screen.getByRole('textbox', { name: 'Start date' }) as HTMLInputElement;

    fireEvent.change(start, { target: { value: '2024-09' } });
    await view.fixture.whenStable();

    expect(start.value).toBe('2024-09');
    expect(screen.getByText('Study range: Enter a first and a last day.')).not.toBeNull();
    expect(lockButton().disabled).toBe(true);
    expect(service.defaults).toHaveBeenCalledTimes(1);
    expect(service.preflight).toHaveBeenCalledTimes(1);

    service.defaults.mockResolvedValueOnce(defaults({ development_start_ms: etMidnightMs('2024-10-01') }));
    fireEvent.change(start, { target: { value: '2024-09-19' } });

    await waitFor(() => expect(service.defaults.mock.lastCall?.[2]).toEqual({ final_months: 3, training_months: 6, test_months: 2, range_start_ms: etMidnightMs('2024-09-19'), range_end_ms: etMidnightMs('2026-04-01') }));
    await waitFor(() => expect(lockButton().disabled).toBe(false));
  });

  it("shows the server's refusal of a date under the range, with the day the range can start on one click away (#2886)", async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const message = 'The slowest setting needs 32 decision bars of history before 2024-01-01, and the lake does not hold them for SPY. The earliest start the lake can serve with whole folds is 2024-11-01.';
    service.preflight.mockResolvedValueOnce(
      preflightFixture({ refusals: [{ code: 'RUN_UP_HISTORY_MISSING', field: 'development_start_ms', message, nearest_valid_ms: [etMidnightMs('2024-11-01')] }] }),
    );
    fireEvent.input(screen.getByLabelText('Fast EMA length low'), { target: { value: '4' } });

    const range = view.container.querySelector<HTMLElement>('[id="gs-plan-range"]');
    await waitFor(() => expect(within(range as HTMLElement).getByRole('alert').textContent).toBe(message));
    fireEvent.click(within(screen.getByRole('list', { name: /cannot be locked/i })).getByRole('button', { name: message }));
    expect(document.activeElement).toBe(range);

    service.defaults.mockResolvedValueOnce(defaults({ development_start_ms: etMidnightMs('2024-11-01') }));
    fireEvent.click(screen.getByRole('button', { name: 'Start the range on 2024-11-01' }));

    // The offered day becomes the range's first and its last day stays; the server lays the plan out inside it.
    await waitFor(() =>
      expect(service.defaults.mock.lastCall?.[2]).toEqual({ final_months: 3, training_months: 6, test_months: 2, range_start_ms: etMidnightMs('2024-11-01'), range_end_ms: etMidnightMs('2026-04-01') }),
    );
    await waitFor(() => expect(planDates(view.container)[0]).toBe('Development 2024-11-01 to 2025-12-31'));
    expect(screen.queryByRole('button', { name: /^Start the range on/ })).toBeNull();
    expect(document.activeElement).toBe(range);
  });

  it("leaves a default plan that ends after the lake's last day to the server's check: its own last day is not an entry to fix (#2886)", async () => {
    const service = fakeService();
    // The lake stops in January; the default plan's final test runs through 2026-03-31.
    const { view } = await renderForm(service, { pool: [{ ...STUDY_POOL[0], lastHeld: '2026-01-16' }] });

    pickSymbol(view.fixture, 'SPY');

    // The plan reaches the server, which names the sessions the lake lacks and the backfill for them.
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(1));
    expect((screen.getByRole('textbox', { name: 'End date' }) as HTMLInputElement).value).toBe('2026-03-31');
    expect(screen.queryByText(/^Study range:/)).toBeNull();
  });

  it("offers no first day while the plan's own last day is past the lake: the range would be laid out again from another last day (#2886)", async () => {
    const service = fakeService();
    const message = 'The slowest setting needs 32 decision bars of history before 2024-01-01, and the lake does not hold them for SPY. The earliest start the lake can serve with whole folds is 2024-03-01.';
    service.preflight.mockResolvedValueOnce(
      preflightFixture({ refusals: [{ code: 'RUN_UP_HISTORY_MISSING', field: 'development_start_ms', message, nearest_valid_ms: [etMidnightMs('2024-03-01')] }] }),
    );
    // The lake stops in January; the default plan's final test runs through 2026-03-31.
    const { view } = await renderForm(service, { pool: [{ ...STUDY_POOL[0], lastHeld: '2026-01-16' }] });
    pickSymbol(view.fixture, 'SPY');

    await waitFor(() => expect(within(view.container.querySelector<HTMLElement>('[id="gs-plan-range"]') as HTMLElement).getByRole('alert').textContent).toBe(message));
    expect(screen.queryByRole('button', { name: /^Start the range on/ })).toBeNull();
  });

  it("lays a quick range out inside the lake when the default plan ran past it, and keeps the plan's own last day when only the first is typed (#2886)", async () => {
    const service = fakeService();
    // The lake stops on 2026-01-16; the default plan's final test runs through 2026-03-31.
    const { view } = await renderForm(service, { pool: [{ ...STUDY_POOL[0], lastHeld: '2026-01-16' }] });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(1));

    // The plan's own last day is the server's to judge, so a first day typed beside it is laid out as asked.
    service.defaults.mockResolvedValueOnce(defaults({ development_start_ms: etMidnightMs('2024-08-01') }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: '2024-07-15' } });
    await waitFor(() => expect(service.defaults.mock.lastCall?.[2]).toMatchObject({ range_start_ms: etMidnightMs('2024-07-15'), range_end_ms: etMidnightMs('2026-04-01') }));

    // A quick range is the lake's: it ends on the last day held.
    service.defaults.mockResolvedValueOnce(defaults({ development_start_ms: etMidnightMs('2025-02-17'), development_end_ms: etMidnightMs('2025-10-17'), final_start_ms: etMidnightMs('2025-10-17'), final_end_ms: etMidnightMs('2026-01-17') }));
    fireEvent.click(quickRange('1Y'));

    await waitFor(() => expect(service.defaults.mock.lastCall?.[2]).toMatchObject({ range_start_ms: etMidnightMs('2025-01-17'), range_end_ms: etMidnightMs('2026-01-17') }));
    await waitFor(() => expect(planDates(view.container)[1]).toBe('Final test 2025-10-17 to 2026-01-16'));
    expect(screen.queryByText(/^Study range:/)).toBeNull();
  });

  it('says so when the data plane answers a range with dates that are not that range (#2886)', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    // A data plane older than this page ignores the range and answers with its default dates.
    service.defaults.mockResolvedValueOnce(defaults());

    fireEvent.click(quickRange('2Y'));

    const range = view.container.querySelector<HTMLElement>('[id="gs-plan-range"]') as HTMLElement;
    await waitFor(() => expect(within(range).getByRole('alert').textContent).toMatch(/it is older than this page\. Restart the data plane, then pick the range again\./));
    expect(planDates(view.container)).toEqual([]);
    expect(lockButton().disabled).toBe(true);
    expect(service.preflight).toHaveBeenCalledTimes(1);
  });

  it('a range picked while dates are being laid out for a month count wins: the late layout is dropped', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const pending = deferred<GoldenSearchDefaults>();
    service.defaults.mockImplementationOnce(() => pending.promise);

    fireEvent.input(screen.getByLabelText('Final test (months)'), { target: { value: '4' } });
    await waitFor(() => expect(service.defaults).toHaveBeenCalledTimes(2));
    service.defaults.mockResolvedValueOnce(twoYearDefaults());
    fireEvent.click(quickRange('2Y'));
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    pending.resolve(fourMonthDefaults());
    await view.fixture.whenStable();

    expect(service.defaults.mock.lastCall?.[2]).toEqual({ final_months: 4, training_months: 6, test_months: 2, ...TWO_YEARS });
    expect(screen.queryByText('Laying out the dates…')).toBeNull();
    expect(service.preflight).toHaveBeenCalledTimes(2);
    expect(service.preflight.mock.lastCall?.[0].final_start_ms).toBe(etMidnightMs('2026-06-19'));
  });

  it('a range the server cannot lay out shows its reason and keeps the plan from being locked (#2886)', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const message = 'The range is too short: 6 months of training, one test of 2 months and a final test of 3 months need development to start by 2025-10-19, and the range starts on 2026-03-19.';
    service.defaults.mockRejectedValueOnce(new HttpErrorResponse({ status: 400, error: { detail: { code: 'INTERVALS_INVALID', field: 'range_start_ms', message } } }));

    fireEvent.click(quickRange('6M'));

    // The server's words are under the range it refused; the plan's dates, which are the range before's, are not shown.
    const range = view.container.querySelector<HTMLElement>('[id="gs-plan-range"]') as HTMLElement;
    await waitFor(() => expect(within(range).getByRole('alert').textContent).toBe(message));
    expect(planDates(view.container)).toEqual([]);
    expect(lockButton().disabled).toBe(true);
    expect(service.preflight).toHaveBeenCalledTimes(1);
  });

  it('defaults the server refuses show its reason', async () => {
    const service = fakeService();
    service.defaults.mockRejectedValueOnce(new HttpErrorResponse({ status: 400, error: { detail: { code: 'NO_COVERAGE', message: 'SPY has no bars held before 2024-01-01.' } } }));
    const { view } = await renderForm(service);

    pickSymbol(view.fixture, 'SPY');

    expect(await screen.findByText('SPY has no bars held before 2024-01-01.')).not.toBeNull();
  });

  it('a failed layout says so and keeps the plan from being checked or locked', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    service.defaults.mockRejectedValueOnce(new Error('down'));

    fireEvent.input(screen.getByLabelText('Test window (months)'), { target: { value: '3' } });

    expect((await screen.findAllByText(/the dates could not be laid out/i)).length).toBeGreaterThan(0);
    // The bar would describe the plan of the months before.
    expect(planDates(view.container)).toEqual([]);
    expect(lockButton().disabled).toBe(true);
    expect(service.preflight).toHaveBeenCalledTimes(1);
  });

  it('a revised plan keeps its frozen dates: no month count is assumed, and a fold change is checked as it is', async () => {
    const service = fakeService();
    await renderForm(service, { reviseFrom: studyDetail('awaiting_validation') });
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());

    expect((screen.getByLabelText('Final test (months)') as HTMLInputElement).value).toBe('');
    fireEvent.input(screen.getByLabelText('Training window (months)'), { target: { value: '9' } });

    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    expect(service.defaults).not.toHaveBeenCalled();
    expect(service.preflight.mock.lastCall?.[0].training_months).toBe(9);
  });

  it("tells a revised plan whose fold change the server refused how to have its dates laid out again (#2886)", async () => {
    const service = fakeService();
    await renderForm(service, { reviseFrom: studyDetail('awaiting_validation') });
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());
    const message = '27 months do not divide into whole folds of 2 months after 9 months of training; the final test can start on 2026-02-01 or 2026-04-01';
    service.preflight.mockResolvedValueOnce(preflightFixture({ refusals: [{ code: 'FOLDS_INVALID', field: 'development_end_ms', message, nearest_valid_ms: [etMidnightMs('2026-02-01')] }] }));

    fireEvent.input(screen.getByLabelText('Training window (months)'), { target: { value: '9' } });

    // No field takes the day the server names, so the range says what does.
    expect(await screen.findByText(/Pick a range, or enter the final test's length in months, and the dates are laid out again\./)).not.toBeNull();
    expect(screen.queryByRole('button', { name: /^Start the range on/ })).toBeNull();
  });

  it("a revised plan asks for the final test's length before it lays a picked range out (#2886)", async () => {
    const service = fakeService();
    await renderForm(service, { reviseFrom: studyDetail('awaiting_validation') });
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());

    fireEvent.click(quickRange('2Y'));

    expect(await screen.findByText("Final test (months): Enter the final test's length in months; the dates are laid out from it.")).not.toBeNull();
    expect(service.defaults).not.toHaveBeenCalled();
    service.defaults.mockResolvedValueOnce(twoYearDefaults());
    fireEvent.input(screen.getByLabelText('Final test (months)'), { target: { value: '3' } });

    await waitFor(() => expect(service.defaults.mock.lastCall?.[2]).toEqual({ final_months: 3, training_months: 6, test_months: 2, ...TWO_YEARS }));
    await waitFor(() => expect(service.preflight.mock.lastCall?.[0].development_start_ms).toBe(etMidnightMs('2024-10-19')));
  });

  it('runs the backfill a refusal for missing sessions names, and the refusal goes once the lake holds them', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValueOnce(missingSessions());
    const lake = fakeBackfill();
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    // The backfill is the remedy the refusal names: no other day is offered before it has been tried.
    expect(screen.queryByRole('button', { name: /^End the range on/ })).toBeNull();

    fireEvent.click(backfillButton());

    await waitFor(() => expect(lake.start).toHaveBeenCalledTimes(1));
    expect(lake.start.mock.lastCall?.[0]).toMatchObject({ symbols: ['SPY'], price_adjustment_mode: 'polygon_split_adjusted', ...MISSING_WINDOW, data_types: ['trade'], lean_image_digest: 'sha256:lean' });
    lake.progress(3, 7);
    expect(await screen.findByText(/The backfill is running: 3 of 7 days\./)).not.toBeNull();
    expect(backfillButton().disabled).toBe(true);
    expect(service.preflight).toHaveBeenCalledTimes(1);

    lake.end({ kind: 'completed' });

    // The lake changed under the plan, so it is checked again; this time nothing is missing.
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());
    expect(service.preflight).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('button', { name: 'Backfill SPY' })).toBeNull();
    expect(screen.queryByText(/The backfill/)).toBeNull();
  });

  it('says why sessions are still missing after the backfill, and the range then offers the last day the lake serves', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValue(missingSessions());
    const lake = fakeBackfill();
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    fireEvent.click(backfillButton());
    await waitFor(() => expect(lake.start).toHaveBeenCalledTimes(1));

    lake.dayFailed('Polygon returned no bars for SPY on 2026-03-23.');
    lake.end({ kind: 'completed' });

    const said = await screen.findByText(/The backfill finished, and the lake still does not hold every session this plan reads\./);
    expect(said.textContent).toContain('Polygon returned no bars for SPY on 2026-03-23.');
    expect(screen.getByText('Pick a study range the lake holds, or run the backfill again.')).not.toBeNull();
    expect(backfillButton().disabled).toBe(false);

    service.defaults.mockResolvedValueOnce(defaults({ final_start_ms: etMidnightMs('2025-12-21'), development_end_ms: etMidnightMs('2025-12-21'), final_end_ms: etMidnightMs('2026-03-21') }));
    service.preflight.mockResolvedValue(preflightFixture());
    fireEvent.click(screen.getByRole('button', { name: 'End the range on 2026-03-20' }));

    // The offered day becomes the range's last and its first day stays; the server lays the plan out inside it.
    await waitFor(() =>
      expect(service.defaults.mock.lastCall?.[2]).toEqual({ final_months: 3, training_months: 6, test_months: 2, range_start_ms: etMidnightMs('2024-01-01'), range_end_ms: etMidnightMs('2026-03-21') }),
    );
    await waitFor(() => expect(screen.getByText(/plan is ready to lock/i)).not.toBeNull());
    expect(screen.queryByText(/The backfill/)).toBeNull();
  });

  it('says why a backfill did not run to its end, and lets it be tried again', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValue(missingSessions());
    const lake = fakeBackfill();
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    fireEvent.click(backfillButton());
    await waitFor(() => expect(lake.start).toHaveBeenCalledTimes(1));

    lake.end({ kind: 'failed', code: 'provider_entitlement_error', message: 'The provider refused the request.' });

    expect(await screen.findByText(/The backfill did not finish: The provider refused the request\./)).not.toBeNull();
    expect(backfillButton().disabled).toBe(false);
  });

  it('keeps what one backfill found to the sessions it ran over: a refusal for other sessions starts from its own backfill', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValue(missingSessions());
    const lake = fakeBackfill();
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    fireEvent.click(backfillButton());
    await waitFor(() => expect(lake.start).toHaveBeenCalledTimes(1));
    lake.dayFailed('Polygon returned no bars for SPY on 2026-03-23.');
    lake.end({ kind: 'completed' });
    await screen.findByText(/The backfill finished/);

    // The plan changes onto a hole the backfill was never asked for.
    const hole = 'missing data: SPY has no minute bars for 3 of 564 trading sessions in 2023-12-28..2026-03-20 — missing 2025-01-13..2025-01-15';
    const elsewhere = { symbol: 'SPY', price_adjustment_mode: 'polygon_split_adjusted' as const, start_trading_date_ms: Date.UTC(2025, 0, 13, 12), end_trading_date_ms: Date.UTC(2025, 0, 15, 12) };
    service.preflight.mockResolvedValue(
      preflightFixture({ refusals: [{ code: 'DATA_MISSING', field: 'development_start_ms', message: hole, nearest_valid_ms: [etMidnightMs('2025-03-01')], backfill: elsewhere }] }),
    );
    fireEvent.input(screen.getByLabelText('Fast EMA length low'), { target: { value: '4' } });

    await waitFor(() => expect(within(screen.getByRole('list', { name: /cannot be locked/i })).getByRole('button', { name: hole })).not.toBeNull());
    expect(screen.queryByText(/The backfill finished/)).toBeNull();
    expect(screen.queryByRole('button', { name: /^Start the range on/ })).toBeNull();
    expect(backfillButton().disabled).toBe(false);
  });

  it('cancels a running backfill: the footer lets go of it at once, the server is told to stop, and the plan is checked again', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValue(missingSessions());
    const lake = fakeBackfill();
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    fireEvent.click(backfillButton());
    const cancel = (await screen.findByRole('button', { name: 'Cancel backfill' })) as HTMLButtonElement;
    await waitFor(() => expect(cancel.disabled).toBe(false));

    fireEvent.click(cancel);

    // A job whose end never arrives cannot hold the footer: nothing ended this one but the cancel.
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    expect(lake.cancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/The backfill/)).toBeNull();
    // A cancelled backfill was not tried: the range offers nothing in its place yet.
    expect(screen.queryByRole('button', { name: /^End the range on/ })).toBeNull();
  });

  it('says so when the server could not be told to stop a cancelled backfill', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValue(missingSessions());
    const lake = fakeBackfill();
    lake.cancel.mockRejectedValueOnce(new HttpErrorResponse({ status: 503, statusText: 'Service Unavailable' }));
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    fireEvent.click(backfillButton());
    const cancel = (await screen.findByRole('button', { name: 'Cancel backfill' })) as HTMLButtonElement;
    await waitFor(() => expect(cancel.disabled).toBe(false));

    fireEvent.click(cancel);

    // The plan is checked again all the same, and the footer still says what the cancel could not do.
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    expect(screen.getByText(/The backfill did not finish: it was cancelled here, but the server could not be told to stop it, so it may still be running\./)).not.toBeNull();
  });

  it('stops watching a running backfill when the form goes, and checks nothing again', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValue(missingSessions());
    const lake = fakeBackfill();
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));
    fireEvent.click(backfillButton());
    await waitFor(() => expect(lake.start).toHaveBeenCalledTimes(1));
    await screen.findByRole('button', { name: 'Cancel backfill' });

    view.fixture.destroy();
    await Promise.resolve();
    await Promise.resolve();

    expect(lake.detach).toHaveBeenCalledTimes(1);
    // The job keeps running on the server: leaving the page does not cancel it.
    expect(lake.cancel).not.toHaveBeenCalled();
    expect(service.preflight).toHaveBeenCalledTimes(1);
  });

  it('says why when the backfill cannot be started', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValue(missingSessions());
    const lake = fakeBackfill({ kind: 'unavailable', message: 'The data lake service is not reachable.' });
    const { view } = await renderForm(service, { pool: LAGGING_POOL, providers: lake.providers });
    pickSymbol(view.fixture, 'SPY');
    await waitFor(() => expect(backfillButton().disabled).toBe(false));

    fireEvent.click(backfillButton());

    expect(await screen.findByText(/The backfill did not finish: The data lake service is not reachable\./)).not.toBeNull();
    expect(lake.start).not.toHaveBeenCalled();
  });

  it('offers the last day the lake serves at once when no backfill can fetch the missing sessions', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValueOnce(missingSessions(false));
    const { view } = await renderForm(service, { pool: LAGGING_POOL });

    pickSymbol(view.fixture, 'SPY');

    expect(await screen.findByRole('button', { name: 'End the range on 2026-03-20' })).not.toBeNull();
    expect(screen.queryByRole('button', { name: 'Backfill SPY' })).toBeNull();
  });

  it('offers a revised plan no last day: its frozen dates carry no final-test length to lay the range out from', async () => {
    const service = fakeService();
    service.preflight.mockResolvedValueOnce(missingSessions(false));

    await renderForm(service, { reviseFrom: studyDetail('awaiting_validation') });

    expect(await screen.findByText(/Pick a range, or enter the final test's length in months, and the dates are laid out again\./)).not.toBeNull();
    expect(screen.queryByRole('button', { name: /^End the range on/ })).toBeNull();
  });

  it('passes axe with the defaults loaded and a refusal shown', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    service.preflight.mockResolvedValueOnce(preflightFixture({ refusals: [{ code: 'WORKLOAD_LIMIT', field: 'budget_cap', message: 'Over the cap.' }] }));
    fireEvent.input(screen.getByLabelText(/run cap/i), { target: { value: '100' } });
    await screen.findByRole('list', { name: /cannot be locked/i });

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
