import { provideRouter } from '@angular/router';
import axe from 'axe-core';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchStudyComponent } from './golden-search-study.component';
import { GoldenSearchService, StageDispatchError, StudyConflictError, type CommandOutcome } from './golden-search.service';
import type { StudyCommandRequest, StudyDetail } from './golden-search.types';
import { emaCapability, studyDetail } from './testing/fixtures';

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

  it('Plan step: the frozen plan is read-only and Revise starts a new linked study', async () => {
    const service = fakeService(studyDetail('awaiting_validation'));
    await renderStudy(service);

    fireEvent.click(within(screen.getByRole('navigation', { name: 'Research steps' })).getByRole('button', { name: /plan/i }));

    const table = await screen.findByRole('table', { name: /knobs, in search order/i });
    expect(within(table).queryAllByRole('spinbutton')).toHaveLength(0);
    const revise = screen.getByRole('link', { name: /revise \(new study\)/i });
    expect(revise.getAttribute('href')).toBe('/golden-search?revise=study-0001-aaaa');
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

  it('passes axe on the Plan, Search and Test over time steps', async () => {
    const view = await renderStudy(fakeService(studyDetail('awaiting_candidate')));
    const nav = within(screen.getByRole('navigation', { name: 'Research steps' }));

    for (const step of [/plan/i, /search/i, /test over time/i]) {
      fireEvent.click(nav.getByRole('button', { name: step }));
      await view.fixture.whenStable();
      const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
    }
  });
});
