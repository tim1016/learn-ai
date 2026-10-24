import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import {
  WORKSPACE_STATE_LABELS,
  type WorkspaceStateContent,
  type WorkspaceStateKind,
} from './workspace-state';
import { WorkspaceStateBlockComponent } from './workspace-state-block.component';

// The states PRD #3490 R04 names, spelled out so a dropped one fails here.
const ALL_STATES: readonly WorkspaceStateKind[] = [
  'idle',
  'invalid',
  'checking',
  'filling',
  'queued',
  'running',
  'complete',
  'partial',
  'cancelled',
  'failed',
  'unavailable',
];
const INTERRUPTING: readonly WorkspaceStateKind[] = ['failed', 'unavailable'];

async function renderState(state: WorkspaceStateContent) {
  const triggered: string[] = [];
  const view = await render(WorkspaceStateBlockComponent, {
    inputs: { state },
    providers: [provideRouter([])],
    on: { actionTriggered: (id: string) => triggered.push(id) },
  });
  return { view, triggered };
}

describe('WorkspaceStateBlockComponent', () => {
  it.each(ALL_STATES)('renders the %s state in the words the domain supplied', async (kind) => {
    await renderState({
      kind,
      happened: `happened in ${kind}`,
      why: `why in ${kind}`,
      next: `next in ${kind}`,
    });

    const block = screen.getByRole(INTERRUPTING.includes(kind) ? 'alert' : 'status');
    expect(block.textContent).toContain(WORKSPACE_STATE_LABELS[kind]);
    expect(screen.getByText(`happened in ${kind}`)).toBeTruthy();
    expect(screen.getByText(`why in ${kind}`)).toBeTruthy();
    expect(screen.getByText(`next in ${kind}`)).toBeTruthy();
  });

  it('never invents a reason the domain did not give', async () => {
    await renderState({ kind: 'failed', happened: 'The fill stopped.' });

    // The state's name and what happened: no reason, no next step, no filler.
    const parts = Array.from(screen.getByRole('alert').children).map((part) => part.textContent?.trim());
    expect(parts).toEqual([WORKSPACE_STATE_LABELS.failed, 'The fill stopped.']);
  });

  it('shows real counts with a bar drawn from them, and no percentage', async () => {
    await renderState({
      kind: 'filling',
      happened: 'Fetching missing days.',
      progress: { kind: 'counts', done: 12, total: 30, unit: 'days' },
    });

    expect(screen.getByText('12 of 30 days')).toBeTruthy();
    const bar = screen.getByRole('progressbar') as HTMLProgressElement;
    expect([bar.value, bar.max]).toEqual([12, 30]);
    expect(screen.getByRole('status').textContent).not.toContain('%');
  });

  it('says how many are done so far when the total is not known, and draws no bar', async () => {
    await renderState({
      kind: 'running',
      happened: 'Scanning sessions.',
      progress: { kind: 'counts', done: 7, total: null, unit: 'sessions' },
    });

    expect(screen.getByText('7 sessions so far')).toBeTruthy();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('names the phase the work is in instead of guessing how far along it is', async () => {
    await renderState({
      kind: 'running',
      happened: 'Computing.',
      progress: { kind: 'phase', label: 'Walking fold 2 of 5' },
    });

    expect(screen.getByText('Walking fold 2 of 5')).toBeTruthy();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('offers the host\'s retry as a button it reports, and a follow-up as a link', async () => {
    const { triggered } = await renderState({
      kind: 'failed',
      happened: 'The fill failed.',
      actions: [
        { id: 'retry-fill', label: 'Retry fill' },
        { id: 'retry-compute', label: 'Retry computation', disabled: true },
        { id: 'open-data', label: 'Open Data', route: ['/data'] },
      ],
    });

    await userEvent.click(screen.getByRole('button', { name: 'Retry fill' }));
    await userEvent.click(screen.getByRole('button', { name: 'Retry computation' }));
    expect(triggered).toEqual(['retry-fill']);
    expect(screen.getByRole('link', { name: 'Open Data' }).getAttribute('href')).toBe('/data');
  });
});
