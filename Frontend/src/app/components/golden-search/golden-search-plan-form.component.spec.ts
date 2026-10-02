import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { fakePickerWorld, pickSymbol } from '../../shared/symbol-picker/testing/fake-picker-world';
import { GoldenSearchPlanFormComponent } from './golden-search-plan-form.component';
import { REFUSAL_INPUTS, refusalTarget } from './golden-search-plan-problems';
import { GoldenSearchService, type CommandOutcome } from './golden-search.service';
import { etMidnightMs } from '../../shared/date/et-midnight';
import type { CreateStudyRequest, DefaultsMonths, GoldenSearchDefaults, GoldenSearchPreflight, ProtocolRequest, StrategyCapability, StudyCommandRequest, StudyDetail } from './golden-search.types';
import { defaults, emaCapability, frequencyProtocol, INCUMBENT_PARAMS, preflight as preflightFixture, studyDetail, unavailableCapability } from './testing/fixtures';

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
  defaults: ReturnType<typeof vi.fn<(strategyKey: string, symbol: string, months?: DefaultsMonths) => Promise<ReturnType<typeof defaults>>>>;
  preflight: ReturnType<typeof vi.fn<(protocol: ProtocolRequest) => Promise<GoldenSearchPreflight>>>;
  createStudy: ReturnType<typeof vi.fn<(request: CreateStudyRequest) => Promise<CommandOutcome>>>;
  command: ReturnType<typeof vi.fn<(studyId: string, request: StudyCommandRequest) => Promise<CommandOutcome>>>;
}

function fakeService(): FakeService {
  return {
    defaults: vi.fn(async (_strategyKey: string, _symbol: string, _months?: DefaultsMonths) => defaults()),
    preflight: vi.fn(async (_protocol: ProtocolRequest) => preflightFixture()),
    createStudy: vi.fn(async (_request: CreateStudyRequest) => ({ study: studyDetail('locked'), jobId: null })),
    command: vi.fn(async (_id: string, _request: StudyCommandRequest) => ({ study: studyDetail('locked', { id: 'study-0002-bbbb', parent_study_id: 'study-0001-aaaa' }), jobId: null })),
  };
}

async function renderForm(service: FakeService, options: { capabilities?: StrategyCapability[]; reviseFrom?: StudyDetail | null; debounceMs?: number } = {}) {
  const view = await render(GoldenSearchPlanFormComponent, {
    inputs: { capabilities: options.capabilities ?? [emaCapability(), unavailableCapability()], reviseFrom: options.reviseFrom ?? null, preflightDebounceMs: options.debounceMs ?? 0 },
    providers: [...fakePickerWorld().providers, { provide: GoldenSearchService, useValue: service }],
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

function lockButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /lock/i }) as HTMLButtonElement;
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

  it('every field the server can refuse links to an input the form renders', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const knobs = defaults().knobs;
    const fields = [...Object.keys(REFUSAL_INPUTS).filter((field) => field !== 'expected_trades_per_year'), ...knobs.flatMap((knob) => [`knobs.${knob.name}`, `seed.${knob.name}`, ...(knob.mode === 'search' ? [`knobs.${knob.name}.step`] : [])])];

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
    expect(screen.getByRole('button', { name: 'Lock plan' })).not.toBeNull();
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
    const describing = (sent: object | undefined): string[] => Object.keys(sent ?? {}).filter((key) => ['final_months', 'final_sessions_cut', 'incumbent_label', 'incumbent_sentence', 'exposure'].includes(key));
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

    expect(screen.getByText('Laying out the dates for these months…')).not.toBeNull();
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

  it('a date typed by hand while dates are being laid out wins: the late layout is dropped and the month count cleared', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);
    const pending = deferred<GoldenSearchDefaults>();
    service.defaults.mockImplementationOnce(() => pending.promise);

    fireEvent.input(screen.getByLabelText('Final test (months)'), { target: { value: '4' } });
    await waitFor(() => expect(service.defaults).toHaveBeenCalledTimes(2));
    fireEvent.input(screen.getByLabelText('Final test from (development ends the day before)'), { target: { value: '2025-10-01' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    pending.resolve(fourMonthDefaults());
    await view.fixture.whenStable();

    expect((screen.getByLabelText('Final test (months)') as HTMLInputElement).value).toBe('');
    expect(screen.queryByText('Laying out the dates for these months…')).toBeNull();
    expect(service.preflight).toHaveBeenCalledTimes(2);
    const sent = service.preflight.mock.lastCall?.[0];
    expect(sent?.final_start_ms).toBe(etMidnightMs('2025-10-01'));
    expect(sent?.development_end_ms).toBe(etMidnightMs('2025-10-01'));
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

    expect(await screen.findByText(/the dates could not be laid out for these months/i)).not.toBeNull();
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
