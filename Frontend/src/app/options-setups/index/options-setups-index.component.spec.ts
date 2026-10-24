import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { OptionsBacktestService, SetupRefusedError } from '../../components/options-backtest/options-backtest.service';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { approvalOf, GOLDEN_ID, indexOf, runOf, savedName, STUDY_ID, studyOf } from '../testing/fixtures';
import { OptionsSetupsIndexComponent } from './options-setups-index.component';
import { OptionsSetupIndexService, type OptionsSetupIndex } from './options-setup-index.service';

async function renderIndex(read: () => Promise<OptionsSetupIndex>, service: Partial<Pick<OptionsBacktestService, 'remove'>> = {}) {
  const view = await render(OptionsSetupsIndexComponent, {
    providers: [provideRouter([]), ...fakePickerWorld().providers, { provide: OptionsSetupIndexService, useValue: { read } }, { provide: OptionsBacktestService, useValue: service }],
  });
  await view.fixture.whenStable();
  return view;
}

const saved = () => screen.getByRole('region', { name: 'Saved setups' });
const evidence = () => screen.getByRole('region', { name: 'Runs, searches and approved versions' });
const rowOf = (within_: HTMLElement, name: string | RegExp) => within(within_).getByRole('row', { name });

describe('the Options setups list', () => {
  it('lists the saved setups by name, with a way to start another', async () => {
    await renderIndex(async () => indexOf({ setups: [savedName('Alpha', '1'), savedName('Beta', '2', { example: true, saved_at_ms: null })] }));

    expect(screen.getByRole('heading', { level: 1, name: 'Options setups' })).toBeTruthy();
    expect(within(saved()).getByRole('link', { name: 'Alpha' }).getAttribute('href')).toBe('/options-setups/Alpha/overview');
    expect(within(rowOf(saved(), /Beta/)).getByText('Example')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'New options setup' }).getAttribute('href')).toBe('/options-setups/_new/define');
  });

  it('puts a delete control on a saved setup and none on an example', async () => {
    await renderIndex(async () => indexOf({ setups: [savedName('Alpha', '1'), savedName('Shipped', '2', { example: true })] }));

    expect(within(saved()).getAllByRole('button', { name: /^Delete setup/ }).map((button) => button.getAttribute('aria-label'))).toEqual(['Delete setup Alpha']);
  });

  it('lists a setup that can no longer be opened with its reason, as a name with no link, and still lets it be deleted', async () => {
    await renderIndex(async () => indexOf({ setups: [savedName('Old rules', null)] }));

    const row = rowOf(saved(), /Old rules/);

    expect(within(row).queryByRole('link', { name: 'Old rules' })).toBeNull();
    expect(row.textContent).toContain('Cannot be opened: legs.0.contracts: Input should be at most 1000');
    expect(within(row).getByRole('button', { name: 'Delete setup Old rules' })).toBeTruthy();
  });

  it('says what is recorded for each setup\'s exact content, and which other names hold the same content', async () => {
    await renderIndex(async () =>
      indexOf({
        setups: [savedName('Alpha', '1'), savedName('Beta', '1'), savedName('Gamma', '2')],
        runs: [runOf(5, '1'), runOf(6, '1'), runOf(7, '2')],
        studies: [studyOf('1')],
        approvals: [approvalOf('1', { status: 'stale' })],
      }),
    );

    const alpha = rowOf(saved(), /^Alpha/).textContent ?? '';

    expect(alpha).toContain('2 runs');
    expect(alpha).toContain('1 search');
    expect(alpha).toContain('1 approved version, latest Stale');
    expect(alpha).toContain('Same content as “Beta”');
    expect(rowOf(saved(), /Gamma/).textContent?.replace(/\s+/g, ' ')).toContain('1 run · 0 searches · no approved version');
  });
});

describe('every run, search and approved version', () => {
  const world = () =>
    indexOf({
      setups: [savedName('Alpha', '1'), savedName('Beta', '1'), savedName('Gamma', '2')],
      runs: [runOf(5, '1', { recorded_name: 'Alpha' }), runOf(6, '3'), runOf(7, null, { recorded_name: 'Gone' })],
      studies: [studyOf('2')],
      approvals: [approvalOf('1', { recorded_name: 'Alpha', study_id: null })],
    });

  it('lists evidence that was never named, and opens each by its own identity', async () => {
    await renderIndex(async () => world());

    const unnamed = rowOf(evidence(), /Run 6/);

    expect(unnamed.textContent).toContain('Unnamed');
    expect(within(unnamed).getByRole('link', { name: 'Run 6' }).getAttribute('href')).toBe('/options-setups/evidence/run/6');
    expect(within(evidence()).getByRole('link', { name: `Search ${STUDY_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_ID}`);
    expect(within(evidence()).getByRole('link', { name: `Approved version ${GOLDEN_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/golden/${GOLDEN_ID}`);
  });

  it('says which saved names hold each piece of evidence\'s exact content, never by the name it was recorded under', async () => {
    await renderIndex(async () => world());

    const shared = rowOf(evidence(), /Run 5/);
    const edited = rowOf(evidence(), /Run 6/);
    const unreadable = rowOf(evidence(), /Run 7/);

    expect(within(shared).getByRole('link', { name: '“Alpha”' })).toBeTruthy();
    expect(within(shared).getByRole('link', { name: '“Beta”' })).toBeTruthy();
    expect(shared.textContent).toContain('identical content, so each shares this evidence and none owns it');
    // Run 6 was recorded as nothing, and no saved setup holds its content.
    expect(edited.textContent).toContain('No saved setup holds this content now');
    // Run 7 was recorded as "Gone", which says nothing about what it froze, and it cannot be read.
    expect(unreadable.textContent).toContain('Recorded as “Gone”');
    expect(unreadable.textContent).toContain('Not known: its setup can no longer be read');
    expect(unreadable.textContent).toContain('legs: Field required');
  });

  it('keeps an approved version where it is when its name has been reused for other content', async () => {
    await renderIndex(async () =>
      indexOf({
        // "Alpha" now holds different content; the version was recorded as "Alpha" and froze content 1.
        setups: [savedName('Alpha', '9')],
        approvals: [approvalOf('1', { recorded_name: 'Alpha' })],
      }),
    );

    const version = rowOf(evidence(), new RegExp(GOLDEN_ID));

    expect(version.textContent).toContain('Recorded as “Alpha”');
    expect(version.textContent).toContain('No saved setup holds this content now');
    expect(within(version).queryByRole('link', { name: '“Alpha”' })).toBeNull();
    expect(version.textContent).toContain('Ready');
  });

  it('filters by kind, and says how many exist when a list was cut at its limit', async () => {
    await renderIndex(async () => {
      const index = world();
      return { ...index, runs_total: 1500 };
    });

    expect(screen.getByRole('status').textContent).toBe('Showing the newest 3 of 1500 runs.');
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }));
    expect(within(evidence()).getAllByRole('row').slice(1)).toHaveLength(3);
    fireEvent.click(screen.getByRole('button', { name: 'Approved versions' }));
    expect(within(evidence()).getAllByRole('row').slice(1)).toHaveLength(1);
  });
});

describe('deleting a saved setup', () => {
  it('asks once, deletes by name, and reads the list again, leaving its evidence listed', async () => {
    let listed = indexOf({ setups: [savedName('Mine', '1')], runs: [runOf(5, '1', { recorded_name: 'Mine' })] });
    const read = vi.fn(async () => listed);
    const remove = vi.fn(async () => {
      listed = indexOf({ runs: listed.runs });
    });
    await renderIndex(read, { remove });

    fireEvent.click(screen.getByRole('button', { name: 'Delete setup Mine' }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith('Mine'));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(saved()).queryByRole('link', { name: 'Mine' })).toBeNull());
    const run = rowOf(evidence(), /Run 5/);
    expect(run.textContent).toContain('Recorded as “Mine”');
    expect(run.textContent).toContain('No saved setup holds this content now');
  });

  it('shows the server’s reason when it refuses, and still reads the list again', async () => {
    const read = vi.fn(async () => indexOf({ setups: [savedName('Gone', '1')] }));
    const remove = vi.fn(async () => {
      throw new SetupRefusedError('OPTIONS_BACKTEST_SETUP_NOT_FOUND', "No saved setup is named 'Gone'.", 404);
    });
    await renderIndex(read, { remove });

    fireEvent.click(screen.getByRole('button', { name: 'Delete setup Gone' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await within(saved()).findByText("No saved setup is named 'Gone'.");
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  });
});

describe('when the list cannot be read', () => {
  it('says so in place, names the reason, and reads again on request', async () => {
    let fail = true;
    const read = vi.fn(async () => {
      if (fail) throw new Error('research store down');
      return indexOf({ setups: [savedName('Alpha', '1')] });
    });
    await renderIndex(read);

    const failure = await screen.findByRole('alert');
    expect(failure.textContent).toContain('The saved setups and their evidence could not be read.');
    expect(screen.queryByRole('region', { name: 'Saved setups' })).toBeNull();
    fail = false;
    fireEvent.click(within(failure).getByRole('button', { name: 'Read again' }));

    await within(await screen.findByRole('region', { name: 'Saved setups' })).findByRole('link', { name: 'Alpha' });
  });
});
