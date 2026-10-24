import { Router, provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { JobState } from '../../services/jobs.service';
import { provideFakeDateWorld } from '../../shared/date-picker/testing/fake-trading-calendar';
import { OptionsSearchDispatchError, OptionsGoldenSearchService } from './options-golden-search.service';
import type { HistoryGuidanceRequest, PlanRequest } from './options-golden-search.types';
import { OptionsSearchPlanComponent } from './options-search-plan.component';
import { declarations, historyGuidance, preflight, study } from './testing';

const SETUP = 'Saved two-leg setup';

function service() {
  return {
    declarations: vi.fn(async (_name: string) => declarations()),
    historyGuidance: vi.fn(async (_request: HistoryGuidanceRequest) => historyGuidance()),
    preflight: vi.fn(async (_request: PlanRequest) => preflight()),
    captureHistory: vi.fn(async (_request: PlanRequest) => 'history-job'),
    activeHistoryCapture: vi.fn((_request: PlanRequest): JobState | null => null),
    refreshHistoryCaptures: vi.fn(async () => {}),
    onHistoryCapture: vi.fn((_id: string, _handler: (event: { type: string; [key: string]: unknown }) => void) => () => {}),
    cancelHistoryCapture: vi.fn(async () => {}),
    create: vi.fn(async (_request: PlanRequest, _hash: string, _key: string) => study()),
    command: vi.fn(async (_id: string, _request: unknown) => study()),
  };
}

async function plan(fake = service(), inputs: { setupName?: string; parentStudyId?: string } = {}) {
  const view = await render(OptionsSearchPlanComponent, {
    inputs: { setupName: SETUP, ...inputs },
    providers: [provideRouter([]), ...provideFakeDateWorld(), { provide: OptionsGoldenSearchService, useValue: fake }],
  });
  return { view, fake };
}

async function preparePlan(fake = service(), inputs: { parentStudyId?: string } = {}) {
  const result = await plan(fake, inputs);
  const user = userEvent.setup();
  await screen.findByText(/Original identity/);
  await user.click(screen.getByText('Additional settings to vary'));
  const stop = within(screen.getByRole('region', { name: 'Stop loss' }));
  await user.click(stop.getByRole('button', { name: 'Explore' }));
  for (const [label, value] of [['Minimum', '20'], ['Maximum', '25'], ['Step', '5']]) fireEvent.input(stop.getByRole('textbox', { name: label }), { target: { value } });

  const dateFields = screen.getAllByRole('textbox').filter((field) => field.closest('app-date-range-picker') !== null);
  fireEvent.change(dateFields[0], { target: { value: '2024-01-02' } });
  fireEvent.blur(dateFields[0]);
  fireEvent.change(dateFields[1], { target: { value: '2026-09-18' } });
  fireEvent.blur(dateFields[1]);
  return { ...result, user };
}

describe('Start a golden search', () => {
  it('reads the saved setup by its name, and says it is edited in Define and searched as last saved', async () => {
    const { fake } = await plan();

    const link = await screen.findByRole('link', { name: 'Edit setup and name in the setup editor', hidden: true });
    expect(link.getAttribute('href')).toBe('/options-setups/Saved%20two-leg%20setup/define');
    expect(fake.declarations).toHaveBeenCalledExactlyOnceWith(SETUP);
    expect(screen.getByText(/as last saved, and every check and the start name the content below/)).toBeTruthy();
  });

  it('asks for date guidance only with whole-month window lengths in range', async () => {
    const { fake } = await plan();
    await screen.findByRole('heading', { name: 'Maximum study interval selected' });
    const training = screen.getByRole('spinbutton', { name: 'Training months' });
    const refusal = 'Use whole months: 1–120 for training and 1–60 for each test window.';
    for (const value of ['1.5', '0', '121', '']) {
      fireEvent.input(training, { target: { value } });
      await screen.findByText(refusal);
      fireEvent.input(training, { target: { value: '6' } });
      await waitFor(() => expect(screen.queryByText(refusal)).toBeNull());
    }
    expect(fake.historyGuidance.mock.calls.every(([request]) => request.train_months === 6)).toBe(true);
  });

  it('checks and starts from the saved seed: its name and the hash of its content go with the plan, and the plan forks the study it was opened from', async () => {
    const { fake, user } = await preparePlan(service(), { parentStudyId: 'study-0' });
    const check = screen.getByRole('button', { name: 'Check plan' });
    await waitFor(() => expect(check).toHaveProperty('disabled', false));
    await user.click(check);
    await screen.findByText('Dollar limit: 2000 USD');
    await user.click(screen.getByRole('button', { name: 'Start search' }));

    const seed = { setup_name: SETUP, expected_setup_hash: declarations().setup_hash, parent_study_id: 'study-0' };
    await waitFor(() => expect(fake.create).toHaveBeenCalledTimes(1));
    expect(fake.preflight).toHaveBeenCalledWith(expect.objectContaining(seed));
    expect(fake.create).toHaveBeenCalledWith(expect.objectContaining(seed), preflight().preflight_hash, expect.any(String));
    expect(screen.getByRole('link', { name: 'an existing study' }).getAttribute('href')).toBe('/options-setups/evidence/study/study-0');
  });

  it('sends exact selected values and UTC-ms history, then invalidates preflight on a policy edit', async () => {
    const { fake, user } = await preparePlan();
    const check = screen.getByRole('button', { name: 'Check plan' });
    await waitFor(() => expect(check).toHaveProperty('disabled', false));
    await user.click(check);
    await screen.findByText('Dollar limit: 2000 USD');
    expect(fake.preflight).toHaveBeenCalledWith(expect.objectContaining({ knobs: [{ path: 'all_legs.stop_loss', values: [{ call: { kind: 'percent', value: '20.0000' }, put: { kind: 'percent', value: '20.0000' } }, { call: { kind: 'percent', value: '25.0000' }, put: { kind: 'percent', value: '25.0000' } }], priority: 5, mode: 'search' }], history: expect.objectContaining({ start_ms: 1704171600000, end_ms: 1789790400000 }), policy: expect.objectContaining({ max_drawdown_percent: '20' }) }));
    const lock = screen.getByRole('button', { name: 'Start search' });
    expect(lock).toHaveProperty('disabled', false);
    await user.click(screen.getByText('Evidence requirements and additional limits'));
    const floor = screen.getByRole('spinbutton', { name: 'Annualized daily Sharpe floor' });
    await user.clear(floor);
    await user.type(floor, '2');
    expect(lock).toHaveProperty('disabled', true);
    expect(screen.queryByText('Dollar limit: 2000 USD')).toBeNull();
  });

  it('captures missing underlying and listing history from the checked plan and rechecks after the job finishes', async () => {
    const fake = service();
    fake.preflight.mockResolvedValueOnce({ ...preflight(), can_lock: false, missing_history: true, blockers: ['Required underlying or listing history is absent. Capture it before locking this study.'] });
    const { user } = await preparePlan(fake);
    await user.click(screen.getByRole('button', { name: 'Check plan' }));
    const capture = await screen.findByRole('button', { name: 'Capture required history' });
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Start search' }).disabled).toBe(true);
    await user.click(capture);
    expect(fake.captureHistory).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ setup_name: SETUP, history: expect.objectContaining({ start_ms: 1704171600000, end_ms: 1789790400000 }) }));
    await waitFor(() => expect(fake.onHistoryCapture).toHaveBeenCalledWith('history-job', expect.any(Function)));
    const onEvent = fake.onHistoryCapture.mock.calls[0][1];
    onEvent({ type: 'job.progress', current: 3, total: 10 });
    expect(await screen.findByText(/3 of 10 sessions/)).toBeTruthy();
    onEvent({ type: 'job.completed' });
    await waitFor(() => expect(fake.preflight).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Start search' }).disabled).toBe(false));
    expect(screen.queryByRole('button', { name: 'Capture required history' })).toBeNull();
  });

  it('keeps the plan blocked and offers a retry when history capture fails', async () => {
    const fake = service();
    fake.preflight.mockResolvedValue({ ...preflight(), can_lock: false, missing_history: true, blockers: ['Required underlying or listing history is absent. Capture it before locking this study.'] });
    const { user } = await preparePlan(fake);
    await user.click(screen.getByRole('button', { name: 'Check plan' }));
    await user.click(await screen.findByRole('button', { name: 'Capture required history' }));
    const onEvent = fake.onHistoryCapture.mock.calls[0][1];
    onEvent({ type: 'job.failed', message: 'The data plan does not reach that far back.' });
    expect(await screen.findByText('The data plan does not reach that far back.')).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Start search' }).disabled).toBe(true);
    await user.click(screen.getByRole('button', { name: 'Capture required history' }));
    expect(fake.captureHistory).toHaveBeenCalledTimes(2);
  });

  it('reattaches a running capture and keeps its cancel control after a plan edit', async () => {
    const fake = service();
    fake.activeHistoryCapture.mockImplementation(() => ({ id: 'resumed-history', type: 'options_golden_search_history', status: 'running', current: 3, total: 10 } as JobState));
    const { user } = await preparePlan(fake);
    await waitFor(() => expect(fake.onHistoryCapture).toHaveBeenCalledWith('resumed-history', expect.any(Function)));
    expect(await screen.findByText(/3 of 10 sessions/)).toBeTruthy();
    await user.click(screen.getByText('Evidence requirements and additional limits'));
    const floor = screen.getByRole('spinbutton', { name: 'Annualized daily Sharpe floor' });
    await user.clear(floor);
    await user.type(floor, '2');
    await user.click(screen.getByRole('button', { name: 'Cancel capture' }));
    expect(fake.cancelHistoryCapture).toHaveBeenCalledWith('resumed-history');
    expect(fake.captureHistory).not.toHaveBeenCalled();
  });

  it('does not subscribe after the plan is destroyed while a capture starts', async () => {
    const fake = service();
    fake.preflight.mockResolvedValue({ ...preflight(), can_lock: false, missing_history: true });
    let finish: ((id: string) => void) | undefined;
    fake.captureHistory.mockImplementation(() => new Promise<string>((resolve) => { finish = resolve; }));
    const { user, view } = await preparePlan(fake);
    await user.click(screen.getByRole('button', { name: 'Check plan' }));
    await user.click(await screen.findByRole('button', { name: 'Capture required history' }));
    await waitFor(() => expect(fake.captureHistory).toHaveBeenCalledOnce());
    view.fixture.destroy();
    finish?.('history-job');
    await Promise.resolve();
    expect(fake.onHistoryCapture).not.toHaveBeenCalled();
  });

  it('offers the actual shared dimensions for pair audits', async () => {
    const { fake, user } = await preparePlan();
    const momentum = within(screen.getByRole('region', { name: 'Entry momentum' }));
    await user.click(momentum.getByRole('button', { name: 'Explore' }));
    for (const [label, value] of [['Minimum', '20'], ['Maximum', '25'], ['Step', '5']]) fireEvent.input(momentum.getByRole('textbox', { name: label }), { target: { value } });
    await user.click(screen.getByText('Search budget and advanced settings'));
    await user.click(screen.getByText('Predeclared pair audit'));
    await user.selectOptions(screen.getByRole('combobox', { name: 'First setting' }), 'all_legs.stop_loss');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Second setting' }), 'all_legs.momentum');
    await user.click(screen.getByRole('button', { name: 'Check plan' }));
    expect(fake.preflight).toHaveBeenCalledWith(expect.objectContaining({ pair_audits: [['all_legs.stop_loss', 'all_legs.momentum']] }));
  });
});

describe('starting the search', () => {
  async function readyToStart(fake = service()) {
    const result = await preparePlan(fake);
    const navigate = vi.spyOn(result.view.fixture.debugElement.injector.get(Router), 'navigate').mockResolvedValue(true);
    await result.user.click(screen.getByRole('button', { name: 'Check plan' }));
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Start search' }).disabled).toBe(false));
    return { ...result, navigate };
  }

  it('asks for the research before the study opens, and opens the study at its own address', async () => {
    const { fake, user, navigate } = await readyToStart();
    await user.click(screen.getByRole('button', { name: 'Start search' }));
    await waitFor(() => expect(navigate).toHaveBeenCalled());

    expect(fake.command).toHaveBeenCalledExactlyOnceWith('study-1', expect.objectContaining({ command: 'run_research', expected_revision: 4, idempotency_key: expect.any(String) }));
    expect(fake.command.mock.invocationCallOrder[0]).toBeLessThan(navigate.mock.invocationCallOrder[0]);
    expect(navigate).toHaveBeenCalledWith(['/options-setups', 'evidence', 'study', 'study-1']);
  });

  it('asks for no research of a study that is already past it, and still opens it', async () => {
    const fake = service();
    fake.create.mockResolvedValue(study({ phase: 'research', allowed_commands: ['cancel'] }));
    const { user, navigate } = await readyToStart(fake);
    await user.click(screen.getByRole('button', { name: 'Start search' }));
    await waitFor(() => expect(navigate).toHaveBeenCalled());

    expect(fake.command).not.toHaveBeenCalled();
  });

  it('stays on the plan when the research could not be requested, and starting again reuses the same study and the same request', async () => {
    const fake = service();
    fake.command.mockRejectedValueOnce(new Error('Connection lost'));
    const { user, navigate } = await readyToStart(fake);
    await user.click(screen.getByRole('button', { name: 'Start search' }));

    expect(await screen.findByText(/The plan was frozen, but its research could not be requested/)).toBeTruthy();
    expect(navigate).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Start search' }));
    await waitFor(() => expect(navigate).toHaveBeenCalled());

    expect(fake.create).toHaveBeenCalledTimes(2);
    expect(fake.create.mock.calls[0]).toEqual(fake.create.mock.calls[1]);
    expect(fake.command.mock.calls[0]).toEqual(fake.command.mock.calls[1]);
  });

  it('opens the study when the research was committed but its job did not start, so it can be resumed there', async () => {
    const fake = service();
    fake.command.mockRejectedValueOnce(new OptionsSearchDispatchError(study({ status: 'interrupted' }), new Error('Jobs offline')));
    const { user, navigate } = await readyToStart(fake);
    await user.click(screen.getByRole('button', { name: 'Start search' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith(['/options-setups', 'evidence', 'study', 'study-1']));
  });

  it('says the plan could not be locked when the server refuses it, and opens nothing', async () => {
    const fake = service();
    fake.create.mockRejectedValueOnce(new Error('Plan refused'));
    const { user, navigate } = await readyToStart(fake);
    await user.click(screen.getByRole('button', { name: 'Start search' }));

    expect(await screen.findByText('The plan could not be locked.')).toBeTruthy();
    expect(navigate).not.toHaveBeenCalled();
    expect(fake.command).not.toHaveBeenCalled();
  });
});
