import { render, screen, waitFor } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import { OptionsEvidencePageComponent } from './options-evidence-page.component';
import { OptionsEvidenceService, type EvidenceWorkspace } from './options-evidence.service';
import { RUN_WORKSPACE, STUDY_WORKSPACE } from './testing';

describe('Options evidence workspace', () => {
  it.each([
    ['/brokers/alpaca/clerks/paper/accounts/PA9/bots/options-1', true],
    ['https://outside.example/bots/options-1', false],
    ['/brokers/alpaca/clerks/paper/accounts/PA9/deploy', false],
  ])('returns only to an exact in-app bot route: %s', async (bot_return, allowed) => {
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'golden', subjectId: RUN_WORKSPACE.approvals[0].golden_id, view: 'lineage', bot_return },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { describe: () => new Promise(() => undefined) } }],
    });
    await screen.findByRole('heading', { name: RUN_WORKSPACE.headline });
    const back = screen.queryByRole('link', { name: 'Back to bot' });
    expect(back?.getAttribute('href') ?? null).toBe(allowed ? bot_return : null);
    expect(screen.getByRole('link', { name: 'Performance' }).getAttribute('href')?.includes('bot_return=')).toBe(allowed);
  });
  it('opens performance by default and keeps its exact identity visible when the replay read fails', async () => {
    const run = vi.fn().mockRejectedValue(new Error('Retained replay offline'));
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'run', subjectId: '76' },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { run, describe: async () => { throw new Error('Unavailable'); } } }],
    });
    await screen.findByText('The source replay could not be read. Its identity remains visible above.');
    expect(run).toHaveBeenCalledWith(76);
    expect(screen.getByRole('heading', { name: RUN_WORKSPACE.headline })).toBeTruthy();
  });
  it('keeps source-76 ordinary approval separate from search and execution proof', async () => {
    const run = vi.fn();
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'run', subjectId: '76', view: 'lineage' },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { run, describe: () => new Promise(() => undefined) } }],
    });
    expect(await screen.findByRole('heading', { name: 'Profitable in replay. Research qualification is missing.' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Approved from backtest' })).toBeTruthy();
    expect(screen.getByText(/Execution proof: Ready/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open recorded search' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Open exact Golden version' }).getAttribute('href')).toContain(RUN_WORKSPACE.approvals[0].golden_id);
    expect(run).not.toHaveBeenCalled();
  });

  it('discards a prior response when navigating from the combined run to the blocked PUT study', async () => {
    let resolveRun: ((value: EvidenceWorkspace) => void) | undefined;
    const oldRead = new Promise<EvidenceWorkspace>(resolve => { resolveRun = resolve; });
    const read = vi.fn().mockImplementation((kind: string) => kind === 'run' ? oldRead : Promise.resolve(STUDY_WORKSPACE));
    const result = await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'run', subjectId: '76', view: 'lineage' },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read } },
        { provide: OptionsBacktestService, useValue: { describe: async () => { throw new Error('Description unavailable'); } } }],
    });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    await result.rerender({ inputs: { kind: 'study', subjectId: STUDY_WORKSPACE.subject_id, view: 'lineage' } });
    expect(await screen.findByRole('heading', { name: 'This search did not establish a result.' })).toBeTruthy();
    resolveRun?.(RUN_WORKSPACE);
    await result.fixture.whenStable();
    expect(screen.queryByText(RUN_WORKSPACE.headline)).toBeNull();
    expect(screen.getByText(/4 observed \/ 373 scheduled sessions/)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Approved from backtest' })).toBeNull();
  });
});
