import { render, screen, waitFor, within } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import { RunDetailService } from '../options-backtest/run-detail/run-detail.service';
import { OptionsEvidencePageComponent } from './options-evidence-page.component';
import { OptionsEvidenceService, type EvidenceWorkspace } from './options-evidence.service';
import { RUN_WORKSPACE, STUDY_WORKSPACE } from './testing';
import { OptionsResearchLineageService } from './options-research-lineage.service';
import { savedName } from '../../options-setups/testing/fixtures';
import type { SavedSetup } from '../options-backtest/options-backtest.types';

/** The saved setups the page reads to say which of them hold the evidence's content: none, unless a spec says. */
const library = (...saved: SavedSetup[]) => ({ list: async () => saved });

describe('Options evidence workspace', () => {
  it('keeps a valid source and comparator picker usable when the selected historical run is unavailable', async () => {
    const read = vi.fn().mockResolvedValue(RUN_WORKSPACE);
    const compare = vi.fn().mockRejectedValue(new Error('Missing comparator'));
    const result = await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'run', subjectId: '76', view: 'comparison', comparator_run_id: '999999' },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read } },
        { provide: RunDetailService, useValue: { compare } },
        { provide: OptionsBacktestService, useValue: { ...library(), describe: () => new Promise(() => undefined) } }],
    });
    expect(await screen.findByText('The comparison could not be read.')).toBeTruthy();
    expect(screen.getByRole('heading', { name: RUN_WORKSPACE.headline })).toBeTruthy();
    expect(screen.getByRole('textbox', { name: 'Saved comparator run ID' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Performance' })).toBeTruthy();
    expect(read).toHaveBeenCalledWith('run', '76', undefined);
    await result.rerender({ inputs: { kind: 'run', subjectId: '76', view: 'comparison', comparator_run_id: '69' } });
    await waitFor(() => expect(compare).toHaveBeenLastCalledWith(76, 69));
    expect(read).toHaveBeenCalledTimes(1);
  });
  it.each([
    ['/brokers/alpaca/clerks/paper/accounts/PA9/bots/options-1', true],
    ['https://outside.example/bots/options-1', false],
    ['/brokers/alpaca/clerks/paper/accounts/PA9/deploy', false],
  ])('returns only to an exact in-app bot route: %s', async (bot_return, allowed) => {
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'golden', subjectId: RUN_WORKSPACE.approvals[0].golden_id, view: 'lineage', bot_return },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { ...library(), describe: () => new Promise(() => undefined) } }],
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
        { provide: OptionsBacktestService, useValue: { ...library(), run, describe: async () => { throw new Error('Unavailable'); } } }],
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
        { provide: OptionsBacktestService, useValue: { ...library(), run, describe: () => new Promise(() => undefined) } }],
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
        { provide: OptionsResearchLineageService, useValue: { read: async () => { throw new Error('Retained procedure unavailable'); } } },
        { provide: OptionsBacktestService, useValue: { ...library(), describe: async () => { throw new Error('Description unavailable'); } } }],
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

  it('stays inside the Options setups place: every link it draws is an address of that place', async () => {
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'run', subjectId: '76', view: 'lineage' },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { ...library(), describe: () => new Promise(() => undefined) } }],
    });
    await screen.findByRole('heading', { name: RUN_WORKSPACE.headline });

    expect(screen.getByRole('link', { name: 'Performance' }).getAttribute('href')).toBe('/options-setups/evidence/run/76');
    expect(screen.getByRole('link', { name: 'Open exact Golden version' }).getAttribute('href')).toBe(`/options-setups/evidence/golden/${RUN_WORKSPACE.approvals[0].golden_id}?view=lineage`);
    expect(screen.getByRole('link', { name: 'Options setups' }).getAttribute('href')).toBe('/options-setups');
    expect(document.querySelector('a[href^="/options-evidence"], a[href^="/options-backtest"]')).toBeNull();
  });

  it('says which saved setups hold this exact content today, and that identical content is shared, not owned', async () => {
    const saved = [savedName('Alpha', '0', { setup_sha256: RUN_WORKSPACE.setup_sha256 }), savedName('Beta', '0', { setup_sha256: RUN_WORKSPACE.setup_sha256 }), savedName('Other', '2')];
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'run', subjectId: '76' },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { ...library(...saved), run: vi.fn().mockRejectedValue(new Error('offline')), describe: () => new Promise(() => undefined) } }],
    });

    const holders = await screen.findByText(/Saved setups that hold this exact content today/);

    expect(within(holders).getByRole('link', { name: '“Alpha”' }).getAttribute('href')).toBe('/options-setups/Alpha/overview');
    expect(within(holders).getByRole('link', { name: '“Beta”' })).toBeTruthy();
    expect(within(holders).queryByRole('link', { name: '“Other”' })).toBeNull();
    expect(holders.textContent).toContain('none of them owns it');
  });

  it('opens its evidence from its own id when no saved setup holds the content any more', async () => {
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'golden', subjectId: RUN_WORKSPACE.approvals[0].golden_id },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { ...library(savedName('Alpha', '9')), run: vi.fn().mockRejectedValue(new Error('offline')), describe: () => new Promise(() => undefined) } }],
    });

    expect(await screen.findByRole('heading', { name: RUN_WORKSPACE.headline })).toBeTruthy();
    expect(await screen.findByText(/No saved setup holds this exact content now/)).toBeTruthy();
    expect(screen.getByText(/this evidence still opens by its own id and keeps what it froze/)).toBeTruthy();
  });

  it('does not claim which saved setups hold the content when they could not be read, and still shows the evidence', async () => {
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'run', subjectId: '76' },
      providers: [provideRouter([]),
        { provide: OptionsEvidenceService, useValue: { read: async () => RUN_WORKSPACE } },
        { provide: OptionsBacktestService, useValue: { list: async () => { throw new Error('store down'); }, run: vi.fn().mockRejectedValue(new Error('offline')), describe: () => new Promise(() => undefined) } }],
    });

    expect(await screen.findByRole('heading', { name: RUN_WORKSPACE.headline })).toBeTruthy();
    expect(await screen.findByText('Which saved setups hold this exact content could not be read.')).toBeTruthy();
  });

  it('fails in place for a kind of evidence that does not exist, and reads nothing', async () => {
    const read = vi.fn();
    await render(OptionsEvidencePageComponent, {
      inputs: { kind: 'setup', subjectId: 'Alpha' },
      providers: [provideRouter([]), { provide: OptionsEvidenceService, useValue: { read } }, { provide: OptionsBacktestService, useValue: { ...library() } }],
    });

    expect(await screen.findByText(/“setup” is not a kind of Options evidence/)).toBeTruthy();
    expect(read).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Retry evidence' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Options setups' }).getAttribute('href')).toBe('/options-setups');
  });
});
