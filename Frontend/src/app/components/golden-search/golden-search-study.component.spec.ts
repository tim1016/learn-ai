import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import axe from 'axe-core';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchStudyComponent } from './golden-search-study.component';
import { GoldenSearchRefusedError, GoldenSearchService, StageDispatchError, StudyConflictError, type CommandOutcome } from './golden-search.service';
import type { StudyCommandRequest, StudyDetail } from './golden-search.types';
import { emaCapability, frequencyProtocol, QUALIFICATION_FAILURE, studyDetail, tradeActivity } from './testing/fixtures';

interface FakeService {
  get: ReturnType<typeof vi.fn<(id: string) => Promise<StudyDetail>>>;
  command: ReturnType<typeof vi.fn<(id: string, request: StudyCommandRequest) => Promise<CommandOutcome>>>;
  hide: ReturnType<typeof vi.fn<(id: string) => Promise<void>>>;
}

function fakeService(study: StudyDetail): FakeService {
  return {
    get: vi.fn(async (_id: string) => study),
    command: vi.fn(async (_id: string, _request: StudyCommandRequest) => ({ study, jobId: null })),
    hide: vi.fn(async (_id: string) => undefined),
  };
}

async function renderStudy(service: FakeService) {
  const view = await render(GoldenSearchStudyComponent, {
    inputs: { studyId: 'study-0001-aaaa', capabilities: [emaCapability()], pollMs: 0 },
    providers: [provideRouter([]), { provide: GoldenSearchService, useValue: service }],
  });
  await screen.findByRole('navigation', { name: 'Research steps' });
  return view;
}

/** The guidance panel, named by the server's headline, which holds the next action and the record controls. */
function guidance(headline: RegExp): HTMLElement {
  return screen.getByRole('region', { name: headline });
}

function currentStep(): string {
  const nav = screen.getByRole('navigation', { name: 'Research steps' });
  return nav.querySelector('[aria-current="step"]')?.textContent?.trim() ?? '';
}

describe('GoldenSearchStudyComponent', () => {
  it('locked: names the decision, offers Start the search, and sends continue against the revision on screen', async () => {
    const service = fakeService(studyDetail('locked'));
    service.command.mockResolvedValueOnce({ study: studyDetail('search_running', { revision: 4 }), jobId: 'job-1' });
    await renderStudy(service);

    expect(screen.getByRole('heading', { name: /start the search when the plan is right/i })).not.toBeNull();
    expect(currentStep()).toMatch(/^2Search/);
    expect(screen.getByText(/the search has not started/i)).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));

    await screen.findByRole('heading', { name: /the search is running/i });
    const [id, request] = service.command.mock.calls[0];
    expect(id).toBe('study-0001-aaaa');
    expect(request).toMatchObject({ command: 'continue', expected_revision: 3, payload: {} });
    expect(request.idempotency_key).toBeTruthy();
    expect(screen.getByRole('button', { name: /^cancel$/i })).not.toBeNull();
  });

  it('running: shows progress and Cancel, and offers neither a next step nor Hide', async () => {
    const service = fakeService(studyDetail('search_running'));
    await renderStudy(service);

    expect(screen.getByText(/120 of at most 410 evaluations/)).not.toBeNull();
    expect(within(guidance(/the search is running/i)).queryAllByRole('button').map((b) => b.textContent?.trim())).toEqual(['Cancel']);
    expect(screen.queryByRole('button', { name: 'Hide' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(service.command.mock.calls[0]?.[1]).toMatchObject({ command: 'cancel' }));
  });

  it('interrupted: Finish resumes the stage when the server permits it', async () => {
    const service = fakeService(studyDetail('search_running', { presented_status: 'interrupted', permitted_actions: ['finish'] }));
    await renderStudy(service);

    fireEvent.click(screen.getByRole('button', { name: /^finish$/i }));

    await waitFor(() => expect(service.command.mock.calls[0]?.[1]).toMatchObject({ command: 'finish', expected_revision: 3 }));
  });

  it('interrupted: says why Finish is unavailable instead of offering it', async () => {
    const service = fakeService(
      studyDetail('validation_running', { presented_status: 'interrupted', permitted_actions: [], action_refusals: { finish: 'the engine code changed since launch; revise into a new study' } }),
    );
    await renderStudy(service);

    expect(screen.queryByRole('button', { name: /^finish$/i })).toBeNull();
    expect(screen.getByText(/finish unavailable — the engine code changed/i)).not.toBeNull();
  });

  it('a stopped stage whose guidance is its failure reason says the reason once', async () => {
    const reason = 'The data lake could not be read for 2024-03.';
    const view = await renderStudy(
      fakeService(studyDetail('validation_running', { presented_status: 'failed', failure_reason: reason, guidance: { headline: 'This stage stopped before it finished', detail: reason } })),
    );

    expect(view.container.textContent?.split(reason)).toHaveLength(2);
  });

  it('a stopped approval offers Finish beside the retain choices when the server permits both', async () => {
    await renderStudy(fakeService(studyDetail('qualification_pending', { presented_status: 'interrupted', permitted_actions: ['finish', 'retain', 'revise'] })));

    expect(currentStep()).toMatch(/Final decision/);
    expect(screen.getByRole('button', { name: /^finish$/i })).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Keep current settings' })).not.toBeNull();
  });

  it('qualification failed: the reason appears once, in the proof-failure alert beside Retry qualification', async () => {
    const view = await renderStudy(fakeService(studyDetail('qualification_failed')));

    expect(view.container.textContent?.split(QUALIFICATION_FAILURE)).toHaveLength(2);
    expect(screen.getByRole('alert').textContent).toContain(QUALIFICATION_FAILURE);
    expect(screen.getByRole('heading', { name: 'Qualification failed · current default unchanged' })).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Retry qualification' })).not.toBeNull();
  });

  it('awaiting validation: the Search step shows the procedure and the next action is Test over time', async () => {
    const service = fakeService(studyDetail('awaiting_validation'));
    service.command.mockResolvedValueOnce({ study: studyDetail('validation_running', { revision: 4 }), jobId: 'job-2' });
    await renderStudy(service);

    expect(currentStep()).toMatch(/Search/);
    expect(screen.getByRole('heading', { name: 'All-period search' })).not.toBeNull();

    fireEvent.click(within(guidance(/check whether the search procedure holds up/i)).getByRole('button', { name: /test over time/i }));

    await waitFor(() => expect(currentStep()).toMatch(/Test over time/));
    expect(service.command.mock.calls[0]?.[1]).toMatchObject({ command: 'continue' });
  });

  it('awaiting a candidate: opens on Compare with the server guidance, and only another step offers the way back to it', async () => {
    const service = fakeService(studyDetail('awaiting_candidate'));
    await renderStudy(service);

    expect(currentStep()).toMatch(/Compare/);
    expect(screen.getAllByText(/pick one candidate for the final test/i).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /compare candidates/i })).toBeNull();

    fireEvent.click(within(screen.getByRole('navigation', { name: 'Research steps' })).getByRole('button', { name: /test over time/i }));

    expect(await screen.findByRole('button', { name: /compare candidates/i })).not.toBeNull();
  });

  it('stale revision: shows the study as it is now and asks to review before trying again', async () => {
    const service = fakeService(studyDetail('locked'));
    service.command.mockRejectedValueOnce(new StudyConflictError('STALE_REVISION', 'stale', studyDetail('awaiting_validation', { revision: 6 })));
    await renderStudy(service);

    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));

    expect(await screen.findByText(/this study changed since it was shown/i)).not.toBeNull();
    expect(screen.getByRole('heading', { name: /check whether the search procedure holds up/i })).not.toBeNull();
  });

  it('a stage that was authorized but did not start points to Finish', async () => {
    const service = fakeService(studyDetail('locked'));
    service.command.mockRejectedValueOnce(new StageDispatchError(studyDetail('search_running', { presented_status: 'interrupted', permitted_actions: ['finish'] }), new Error('502')));
    await renderStudy(service);

    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));

    expect(await screen.findByText(/its job did not start\. use finish/i)).not.toBeNull();
    expect(screen.getByRole('button', { name: /^finish$/i })).not.toBeNull();
  });

  it('a poll that was in flight when a command answered cannot roll the study back', async () => {
    const service = fakeService(studyDetail('locked'));
    service.command.mockResolvedValueOnce({ study: studyDetail('search_running', { revision: 4 }), jobId: 'job-1' });
    const view = await renderStudy(service);
    let answerPoll: (study: StudyDetail) => void = () => undefined;
    service.get.mockImplementationOnce(() => new Promise<StudyDetail>((resolve) => (answerPoll = resolve)));
    const poll = view.fixture.componentInstance.reload();

    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));
    await screen.findByRole('heading', { name: /the search is running/i });
    answerPoll(studyDetail('locked'));
    await poll;
    await view.fixture.whenStable();

    expect(screen.getByRole('heading', { name: /the search is running/i })).not.toBeNull();
    expect(screen.queryByRole('button', { name: /start the search/i })).toBeNull();
  });

  it("a command answer that lands after moving to another study leaves that study's page alone", async () => {
    const other = studyDetail('awaiting_validation', { id: 'study-0002-bbbb' });
    const service = fakeService(studyDetail('locked'));
    let answerFirst: (outcome: CommandOutcome) => void = () => undefined;
    service.command.mockImplementationOnce(() => new Promise<CommandOutcome>((resolve) => (answerFirst = resolve)));
    const view = await renderStudy(service);

    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));
    service.get.mockResolvedValue(other);
    view.fixture.componentRef.setInput('studyId', other.id);
    await screen.findByRole('heading', { name: 'All-period search' });
    answerFirst({ study: studyDetail('search_running', { revision: 4 }), jobId: 'job-1' });
    await view.fixture.whenStable();

    expect(screen.queryByRole('heading', { name: /the search is running/i })).toBeNull();
    expect(screen.getByRole('heading', { name: 'All-period search' })).not.toBeNull();
    service.command.mockResolvedValueOnce({ study: studyDetail('validation_running', { id: other.id, revision: 4 }), jobId: 'job-2' });
    fireEvent.click(within(guidance(/check whether the search procedure holds up/i)).getByRole('button', { name: /test over time/i }));
    await waitFor(() => expect(service.command.mock.calls.at(-1)?.[0]).toBe(other.id));
  });

  it('a command answer older than the revision on screen is dropped', async () => {
    const service = fakeService(studyDetail('locked'));
    let answerCommand: (outcome: CommandOutcome) => void = () => undefined;
    service.command.mockImplementationOnce(() => new Promise<CommandOutcome>((resolve) => (answerCommand = resolve)));
    const view = await renderStudy(service);
    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));
    await waitFor(() => expect(service.command).toHaveBeenCalledOnce());
    service.get.mockResolvedValueOnce(studyDetail('awaiting_validation', { revision: 5 }));
    await view.fixture.componentInstance.reload();

    answerCommand({ study: studyDetail('search_running', { revision: 4 }), jobId: null });

    // The command has answered once its busy state clears; the newer study is still the one shown.
    await waitFor(() => expect((within(guidance(/check whether the search procedure holds up/i)).getByRole('button', { name: /test over time/i }) as HTMLButtonElement).disabled).toBe(false));
  });

  it('a poll answering with a revision older than the one on screen is dropped', async () => {
    const service = fakeService(studyDetail('search_running', { revision: 4 }));
    const view = await renderStudy(service);
    service.get.mockResolvedValueOnce(studyDetail('locked', { revision: 3 }));

    await view.fixture.componentInstance.reload();
    await view.fixture.whenStable();

    expect(screen.getByRole('heading', { name: /the search is running/i })).not.toBeNull();
  });

  it('a failed poll keeps the study on screen, says it is retrying, and polls again', async () => {
    const running = studyDetail('search_running');
    const service = fakeService(running);
    let answerRetry: (study: StudyDetail) => void = () => undefined;
    service.get
      .mockResolvedValueOnce(running)
      .mockRejectedValueOnce(new Error('network down'))
      .mockImplementationOnce(() => new Promise<StudyDetail>((resolve) => (answerRetry = resolve)));
    await render(GoldenSearchStudyComponent, {
      inputs: { studyId: 'study-0001-aaaa', capabilities: [emaCapability()], pollMs: 5 },
      providers: [provideRouter([]), { provide: GoldenSearchService, useValue: service }],
    });

    expect(await screen.findByText('This study could not be refreshed; retrying.')).not.toBeNull();
    expect(screen.queryByText('This study could not be loaded.')).toBeNull();
    expect(screen.getByRole('heading', { name: /the search is running/i })).not.toBeNull();
    await waitFor(() => expect(service.get).toHaveBeenCalledTimes(3));
    answerRetry(running);
    await waitFor(() => expect(screen.queryByText('This study could not be refreshed; retrying.')).toBeNull());
  });

  it('a study the server cannot load, or a command it fails, says why in its words', async () => {
    const service = fakeService(studyDetail('locked'));
    service.get.mockRejectedValueOnce(new HttpErrorResponse({ status: 404, error: { detail: { code: 'NOT_FOUND', message: 'No Golden Search study study-0001-aaaa exists.' } } }));
    const view = await render(GoldenSearchStudyComponent, {
      inputs: { studyId: 'study-0001-aaaa', capabilities: [emaCapability()], pollMs: 0 },
      providers: [provideRouter([]), { provide: GoldenSearchService, useValue: service }],
    });
    expect((await screen.findByRole('alert')).textContent).toContain('No Golden Search study study-0001-aaaa exists.');

    await view.fixture.componentInstance.reload();
    service.command.mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: { detail: { message: 'The research store is not reachable.' } } }));
    fireEvent.click(await screen.findByRole('button', { name: /start the search/i }));

    expect(await screen.findByText('The research store is not reachable.')).not.toBeNull();
  });

  it('a command with no answer can be retried with the same idempotency key', async () => {
    const service = fakeService(studyDetail('locked'));
    service.command.mockRejectedValueOnce(new Error('network down'));
    await renderStudy(service);

    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));
    await screen.findByText(/the command got no answer/i);
    fireEvent.click(screen.getByRole('button', { name: /start the search/i }));

    await waitFor(() => expect(service.command).toHaveBeenCalledTimes(2));
    expect(service.command.mock.calls[1][1].idempotency_key).toBe(service.command.mock.calls[0][1].idempotency_key);
  });

  it('every step shows the server scope: development dates, the final test and whether it is still locked, capital and costs', async () => {
    const study = studyDetail('awaiting_candidate');
    const service = fakeService(study);
    const view = await renderStudy(service);
    const nav = within(screen.getByRole('navigation', { name: 'Research steps' }));

    for (const step of [/plan/i, /search/i, /test over time/i, /compare/i]) {
      fireEvent.click(nav.getByRole('button', { name: step }));
      await view.fixture.whenStable();
      const scope = screen.getByRole('note', { name: 'Study scope' });
      expect(scope.textContent).toMatch(/Development:\s*2024-01-01\s*–\s*2025-12-31/);
      expect(scope.textContent).toMatch(/Final test\s*2026-01-01\s*–\s*2026-03-31\s*: locked/);
      expect(scope.textContent).toContain('$100,000 starting capital · No commission');
    }

    service.get.mockResolvedValueOnce({ ...study, scope: { ...study.scope, final_state: 'opened_once' } });
    await view.fixture.componentInstance.reload();
    await view.fixture.whenStable();
    expect(screen.getByRole('note', { name: 'Study scope' }).textContent).toMatch(/Final test\s*2026-01-01\s*–\s*2026-03-31\s*: opened once/);
  });

  it.each([
    ['awaiting_candidate', 'Final test held back · Current default unchanged'],
    ['awaiting_review', 'Final test opened once · Current default unchanged'],
    ['qualification_failed', 'Final test opened once · Current default unchanged'],
    ['approved', 'Golden settings ready · Deploy checks still apply'],
  ] as const)('the footer names the data and, %s, where the final test and the default stand', async (state, standing) => {
    await renderStudy(fakeService(studyDetail(state)));

    const footer = screen.getByText('Historical research: Polygon, split adjusted, regular sessions').parentElement;
    expect(footer?.textContent).toContain(standing);
  });

  it('Plan step: the frozen plan is read-only and Revise starts a new linked study', async () => {
    const service = fakeService(studyDetail('awaiting_validation'));
    await renderStudy(service);

    fireEvent.click(within(screen.getByRole('navigation', { name: 'Research steps' })).getByRole('button', { name: /plan/i }));

    const table = await screen.findByRole('table', { name: /knobs, in search order/i });
    expect(within(table).queryAllByRole('spinbutton')).toHaveLength(0);
    const revise = screen.getByRole('link', { name: /revise \(new study\)/i });
    expect(revise.getAttribute('href')).toBe('/golden-search?revise=study-0001-aaaa');
  });

  it('Plan step: a frequency plan shows the floors its receipt froze for each window, and no fixed floors', async () => {
    const service = fakeService(studyDetail('awaiting_validation', { protocol: frequencyProtocol(), activity: tradeActivity() }));
    await renderStudy(service);

    fireEvent.click(within(screen.getByRole('navigation', { name: 'Research steps' })).getByRole('button', { name: /plan/i }));

    const activity = await screen.findByLabelText('Duration-based minimum trades');
    expect(activity.textContent).toContain('Expected trade frequency: 50 completed trades per trading year');
    expect(activity.textContent).toMatch(/Final test\s*At least 13 trades\s*61 trading days/);
    expect(activity.textContent).toMatch(/All forward tests\s*At least 76 trades/);
    expect(within(activity).getByText('Minimum trades for each training window')).not.toBeNull();
    expect(screen.queryByText('Minimum completed trades')).toBeNull();
    expect(screen.queryByText('Minimum final-test trades')).toBeNull();
  });

  it('Hide asks once, then hides the study and reports it', async () => {
    const service = fakeService(studyDetail('awaiting_validation'));
    const view = await renderStudy(service);
    const hidden = vi.fn();
    view.fixture.componentInstance.hidden.subscribe(hidden);

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
    expect(service.hide).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /confirm hide/i }));

    await waitFor(() => expect(hidden).toHaveBeenCalledWith('study-0001-aaaa'));
    expect(service.hide).toHaveBeenCalledWith('study-0001-aaaa');
  });

  it('Compare: locking a candidate sends the pick against the revision on screen and opens its final-test lock', async () => {
    const service = fakeService(studyDetail('awaiting_candidate'));
    service.command.mockResolvedValueOnce({ study: studyDetail('candidate_locked', { revision: 4 }), jobId: null });
    await renderStudy(service);

    fireEvent.click(screen.getByRole('button', { name: /^recent fit/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Review final-test lock' }));

    await screen.findByRole('heading', { name: /is still sealed/i });
    expect(currentStep()).toMatch(/Final decision/);
    expect(service.command.mock.calls[0][1]).toMatchObject({ command: 'select_candidate', expected_revision: 3, payload: { candidate_key: 'recent' } });
  });

  it('a changed pick on a study already locked to a candidate still moves on to the lock once the server accepts it', async () => {
    const locked = studyDetail('candidate_locked');
    const service = fakeService(locked);
    service.command.mockResolvedValueOnce({ study: { ...locked, revision: 4, candidate_key: 'recent' }, jobId: null });
    await renderStudy(service);
    expect(currentStep()).toMatch(/Final decision/);

    fireEvent.click(screen.getByRole('button', { name: 'Return to candidates' }));
    fireEvent.click(await screen.findByRole('button', { name: /^recent fit/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Review final-test lock' }));

    await waitFor(() => expect(currentStep()).toMatch(/Final decision/));
    expect(screen.getByRole('region', { name: /is still sealed/i }).textContent).toContain('Only Recent fit and the frozen incumbent');
  });

  it('a refused pick stays on Compare and shows why', async () => {
    const service = fakeService(studyDetail('awaiting_candidate'));
    service.command.mockRejectedValueOnce(new GoldenSearchRefusedError({ code: 'INCUMBENT_NOT_EXAMINABLE', message: 'Use Keep current settings to finish without consuming the test.' }));
    await renderStudy(service);

    fireEvent.click(screen.getByRole('button', { name: 'Review final-test lock' }));

    expect(await screen.findByText(/use keep current settings to finish without consuming the test/i)).not.toBeNull();
    expect(currentStep()).toMatch(/Compare/);
  });

  it('Final decision: approval dispatches the qualification and the page follows it to the golden configuration', async () => {
    const service = fakeService(studyDetail('awaiting_review'));
    service.command.mockResolvedValueOnce({ study: studyDetail('qualification_pending', { revision: 4 }), jobId: 'job-q' });
    await renderStudy(service);

    fireEvent.click(screen.getByRole('checkbox', { name: /missing independent engine agreement/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve golden configuration' }));

    await screen.findByText(/building the proof and publishing the version\. the current default/i);
    expect(service.command.mock.calls[0][1]).toMatchObject({ command: 'approve', expected_revision: 3, payload: { acknowledge_missing_parity: true } });
  });

  it('announces the new state once the server moves the study on, and says nothing on the first load', async () => {
    const service = fakeService(studyDetail('awaiting_review'));
    service.command.mockResolvedValueOnce({ study: studyDetail('qualification_pending', { revision: 4 }), jobId: 'job-q' });
    await renderStudy(service);
    const live = screen.getAllByRole('status').find((element) => element.classList.contains('sr-only'));
    expect(live?.textContent?.trim()).toBe('');

    fireEvent.click(screen.getByRole('checkbox', { name: /missing independent engine agreement/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve golden configuration' }));

    await waitFor(() => expect(live?.textContent?.trim()).toBe('Building the proof. Qualification is running.'));
  });

  it('passes axe on the Compare and Final decision steps', async () => {
    const view = await renderStudy(fakeService(studyDetail('awaiting_review')));
    const nav = within(screen.getByRole('navigation', { name: 'Research steps' }));

    for (const step of [/compare/i, /final decision/i]) {
      fireEvent.click(nav.getByRole('button', { name: step }));
      await view.fixture.whenStable();
      const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
    }
  });

  it('passes axe on the Plan, Search and Test over time steps', async () => {
    const view = await renderStudy(fakeService(studyDetail('awaiting_candidate', { protocol: frequencyProtocol(), activity: tradeActivity() })));
    const nav = within(screen.getByRole('navigation', { name: 'Research steps' }));

    for (const step of [/plan/i, /search/i, /test over time/i]) {
      fireEvent.click(nav.getByRole('button', { name: step }));
      await view.fixture.whenStable();
      const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
    }
  });
});
