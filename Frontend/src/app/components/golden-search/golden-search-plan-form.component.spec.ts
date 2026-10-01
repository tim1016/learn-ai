import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { fakePickerWorld, pickSymbol } from '../../shared/symbol-picker/testing/fake-picker-world';
import { GoldenSearchPlanFormComponent } from './golden-search-plan-form.component';
import { GoldenSearchService, type CommandOutcome } from './golden-search.service';
import { etMidnightMs } from '../../shared/date/et-midnight';
import type { CreateStudyRequest, DefaultsMonths, GoldenSearchDefaults, GoldenSearchPreflight, ProtocolRequest, StrategyCapability, StudyCommandRequest, StudyDetail } from './golden-search.types';
import { defaults, emaCapability, preflight as preflightFixture, studyDetail, unavailableCapability } from './testing/fixtures';

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
  await waitFor(() => expect(screen.getByText(/the server accepts this plan/i)).not.toBeNull());
}

function lockButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /lock/i }) as HTMLButtonElement;
}

describe('GoldenSearchPlanFormComponent', () => {
  it('offers only declared strategies and names why the others cannot be studied yet', async () => {
    await renderForm(fakeService());

    const picker = screen.getByRole('combobox', { name: 'Strategy' }) as HTMLSelectElement;
    expect(Array.from(picker.options).map((o) => o.value)).toEqual(['ema_crossover_signal']);
    const list = screen.getByRole('list', { name: /cannot study yet/i });
    expect(list.textContent).toContain('SMA Crossover');
    expect(list.textContent).toContain('No Golden Search declaration has shipped');
  });

  it('loads the defaults for the strategy and instrument, then shows every knob, fixed control and constraint', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);

    await pickSpy(service, view);

    expect(service.defaults).toHaveBeenCalledWith('ema_crossover_signal', 'SPY');
    const table = screen.getByRole('table', { name: /knobs, in search order/i });
    for (const label of ['Crossover gap', 'RSI lower gate', 'Fast EMA length', 'Hold time', 'Crossover gap (bps)']) expect(table.textContent).toContain(label);
    expect(screen.getByRole('list', { name: 'Fixed controls' }).textContent).toContain('RSI length');
    expect(screen.getByRole('list', { name: 'Constraints' }).textContent).toContain('fast EMA must be shorter');
    expect(screen.getByText(/registry validated settings/i)).not.toBeNull();
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

    const ceiling = screen.getByLabelText(/maximum drawdown/i);
    fireEvent.input(ceiling, { target: { value: '15' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(2));
    fireEvent.input(ceiling, { target: { value: '25' } });
    await waitFor(() => expect(service.preflight).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByText(/the server accepts this plan/i)).not.toBeNull());

    slow.resolve(preflightFixture({ refusals: [{ code: 'DRAWDOWN_CEILING_INVALID', field: 'policy', message: 'stale answer' }] }));
    await view.fixture.whenStable();

    expect(screen.queryByText(/stale answer/)).toBeNull();
    expect(lockButton().disabled).toBe(false);
    expect(service.preflight.mock.lastCall?.[0].policy.max_drawdown_ceiling).toBe(0.25);
  });

  it('lists every refusal with its code and keeps Lock disabled', async () => {
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
    expect(refusals.textContent).toContain('Workload Limit');
    expect(refusals.textContent).toContain('6,200 runs exceed the cap');
    expect(refusals.textContent).toContain('no whole fold');
    expect(lockButton().disabled).toBe(true);
  });

  it('does not preflight a value it cannot read, and says which one to fix', async () => {
    const service = fakeService();
    const { view } = await renderForm(service);
    await pickSpy(service, view);

    fireEvent.input(screen.getByLabelText(/backtest cap/i), { target: { value: '' } });
    await view.fixture.whenStable();

    expect(screen.getByRole('list', { name: /values to fix/i }).textContent).toContain('Backtest cap');
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
    fireEvent.change(screen.getByLabelText('Search or keep fixed: Crossover gap (bps)'), { target: { value: 'search' } });
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

    await waitFor(() => expect(screen.getByText(/the server accepts this plan/i)).not.toBeNull());
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
    await waitFor(() => expect(screen.getByText(/the server accepts this plan/i)).not.toBeNull());

    view.fixture.componentRef.setInput('reviseFrom', null);

    await waitFor(() => expect(service.defaults).toHaveBeenCalledWith('ema_crossover_signal', 'SPY'));
    await waitFor(() => expect(service.preflight.mock.lastCall?.[0].incumbent.source).toBe('registry'));
    expect(screen.getByText('New study')).not.toBeNull();
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
    await waitFor(() => expect(screen.getByText(/the server accepts this plan/i)).not.toBeNull());

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
    fireEvent.input(screen.getByLabelText(/backtest cap/i), { target: { value: '100' } });
    await screen.findByRole('list', { name: /cannot be locked/i });

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
