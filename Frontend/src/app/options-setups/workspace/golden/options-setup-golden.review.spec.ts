import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { exampleBody } from '../../../components/options-backtest/testing/fixtures';
import { editSlippage, goToDefine, libraryWorld, saveInDefine } from '../../testing/explore-actions';
import { indexOf, runOf, savedName, sha } from '../../testing/fixtures';
import { runEvidence, versionOf } from '../../testing/golden-fixtures';
import { renderTab, setupUrl } from '../../testing/render-workspace';

const VERSION_ID = `ogs_${'a'.repeat(32)}`;
const go = (target: string) => TestBed.inject(Router).navigateByUrl(target);
const url = () => decodeURIComponent(TestBed.inject(Router).url);

/**
 * A server with one saved setup, "Mine", that froze content 1 (runs 7 and 9) while run 8 froze content 2, and
 * the approvals it holds. `saved.hash` is what "Mine" is saved as now, so a spec can move it as a save in Define does.
 */
function world(changes: { list?: ReturnType<typeof vi.fn>; approve?: ReturnType<typeof vi.fn>; read?: ReturnType<typeof vi.fn> } = {}) {
  const library = libraryWorld(vi);
  library.library.set('Mine', exampleBody());
  const saved = { hash: '1' };
  const golden = {
    list: changes.list ?? vi.fn(async (_runId?: number) => []),
    approve: changes.approve ?? vi.fn(async (runId: number, note: string, _key: string, _expected?: string) => versionOf(VERSION_ID, '1', { source_run_id: runId, note })),
    get: vi.fn(),
    revoke: vi.fn(),
    reprove: vi.fn(),
  };
  const evidence = { read: changes.read ?? vi.fn(async (_kind: string, id: string) => runEvidence(Number(id), id === '8' ? '2' : '1')) };
  let reads = 0;
  const index = async () => {
    reads += 1;
    return indexOf({ runs: [runOf(7, '1'), runOf(8, '2'), runOf(9, '1'), runOf(10, '1', { status: 'failed' })] });
  };
  return { ...library, saved, savedNames: () => [savedName('Mine', saved.hash)], golden, evidence, index, reads: () => reads };
}

async function review(w: ReturnType<typeof world>, query = '?run=7') {
  await renderTab(w.service, 'Mine', 'golden', { query, saved: w.savedNames, index: w.index, golden: w.golden, evidence: w.evidence });
}

const approveButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Approve golden setup' });
const typeNote = async (text: string) => fireEvent.input(await screen.findByLabelText('Approval note'), { target: { value: text } });

describe('approving a tested run reviews that run, never the setup in Define (T15)', () => {
  it('approves run 7 once, with the hash of what it froze, after Define was edited and the page came back', async () => {
    const w = world();
    await review(w);
    await screen.findByRole('heading', { name: 'Reviewing run 7' });
    await screen.findByLabelText('Approval note');

    await goToDefine();
    editSlippage('0.4');
    // Back to the address the review has.
    await go(`${setupUrl('Mine', 'golden')}?run=7`);
    await screen.findByRole('heading', { name: 'Reviewing run 7' });
    await typeNote('Reviewed run 7.');
    await waitFor(() => expect(approveButton().disabled).toBe(false));
    fireEvent.click(approveButton());
    fireEvent.click(approveButton());

    await screen.findByText(/Choose this version in an account/);
    expect(w.golden.approve).toHaveBeenCalledTimes(1);
    expect(w.golden.approve).toHaveBeenCalledWith(7, 'Reviewed run 7.', expect.any(String), sha('1'));
    expect(w.evidence.read).toHaveBeenCalledWith('run', '7');
  });

  it('reads the evidence again once a run is approved, so the version is listed, and not before', async () => {
    const w = world();
    await review(w);
    await typeNote('Reviewed run 7.');
    await waitFor(() => expect(approveButton().disabled).toBe(false));
    expect(w.reads()).toBe(1);

    fireEvent.click(approveButton());

    await screen.findByText(/Choose this version in an account/);
    await waitFor(() => expect(w.reads()).toBe(2));
  });

  it('still approves what run 7 froze when the setup was saved as something else, and says that it is not what is saved now', async () => {
    const w = world();
    await review(w);
    await screen.findByRole('heading', { name: 'Reviewing run 7' });
    await goToDefine();
    editSlippage('0.4');
    // The save lands, and the library read after it lists "Mine" as the edited content.
    w.saved.hash = '2';
    await saveInDefine('Mine');
    await go(`${setupUrl('Mine', 'golden')}?run=7`);

    expect(await screen.findByText(/This run froze other content than this setup’s saved content now/)).toBeTruthy();
    await typeNote('Reviewed run 7.');
    await waitFor(() => expect(approveButton().disabled).toBe(false));
    fireEvent.click(approveButton());

    await screen.findByText(/Choose this version in an account/);
    expect(w.golden.approve).toHaveBeenCalledExactlyOnceWith(7, 'Reviewed run 7.', expect.any(String), sha('1'));
  });

  it('shows what the run froze, in its own words, and the hash the approval will carry', async () => {
    const w = world();
    await review(w);

    const frozen = await screen.findByText('Frozen content');
    expect(frozen.parentElement?.textContent).toContain(sha('1'));
    expect(screen.getByText('Run 7 as recorded')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open this run’s evidence' }).getAttribute('href')).toBe('/options-setups/evidence/run/7');
  });

  it('retries a lost response under the same key, with the same run and hash', async () => {
    const approve = vi.fn().mockRejectedValueOnce(new Error('response lost')).mockResolvedValue(versionOf(VERSION_ID, '1'));
    const w = world({ approve });
    await review(w);
    await screen.findByLabelText('Approval note');
    await typeNote('Reviewed run 7.');
    await waitFor(() => expect(approveButton().disabled).toBe(false));

    fireEvent.click(approveButton());
    await screen.findByText(/could not be recorded/i);
    fireEvent.click(approveButton());
    await screen.findByText(/Choose this version in an account/);

    expect(approve).toHaveBeenCalledTimes(2);
    expect(approve.mock.calls[0]).toEqual(approve.mock.calls[1]);
  });

  it('does not make a second version when the list it was reading was older than another approval of the same run', async () => {
    const existing = versionOf(VERSION_ID, '1', { source_run_id: 7 });
    const list = vi.fn().mockResolvedValueOnce([]).mockResolvedValue([existing]);
    const w = world({ list });
    await review(w);
    await screen.findByLabelText('Approval note');
    await typeNote('Reviewed run 7.');
    await waitFor(() => expect(approveButton().disabled).toBe(false));

    fireEvent.click(approveButton());

    expect(await screen.findByText('This run was already approved. That version is shown, and nothing more was approved.')).toBeTruthy();
    expect(w.golden.approve).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Approve golden setup' })).toBeNull();
  });

  it('approves nothing when what the run froze cannot be read, and says so', async () => {
    const w = world({ read: vi.fn().mockRejectedValue(new Error('offline')) });
    await review(w);

    expect(await screen.findByText('Run 7 could not be read.')).toBeTruthy();
    expect(screen.getByText('Nothing can be approved without reading what the run froze.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Approve golden setup' })).toBeNull();
    expect(w.golden.approve).not.toHaveBeenCalled();
  });

  it('reviews no run for a ?run= that is not a run id, and approves nothing', async () => {
    const w = world();
    await review(w, '?run=abc');

    expect(await screen.findByText('“abc” is not a run id, so no run is under review. Nothing was approved.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: /Reviewing run/ })).toBeNull();
    expect(w.evidence.read).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Approve golden setup' })).toBeNull();
  });
});

describe('choosing a run to review', () => {
  it('offers the completed runs of exactly the saved content, and reviews the one chosen, at an address of its own', async () => {
    const w = world();
    await review(w, '');

    const runs = await screen.findByRole('region', { name: 'Approve a tested run' });
    expect(within(runs).getAllByRole('listitem').map((item) => item.getAttribute('data-run'))).toEqual(['9', '7']);
    expect(w.evidence.read).not.toHaveBeenCalled();
    fireEvent.click(within(runs).getByRole('link', { name: 'Review run 7' }));

    expect(await screen.findByRole('heading', { name: 'Reviewing run 7' })).toBeTruthy();
    expect(url()).toBe('/options-setups/Mine/golden?run=7');
    expect(w.golden.approve).not.toHaveBeenCalled();
  });

  it('says there is no run to review for a setup that has never been backtested, and offers none', async () => {
    const w = world();
    w.index = async () => indexOf({});
    await review(w, '');

    expect(await screen.findByText(/No completed run of this exact content is recorded/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: /Review run/ })).toBeNull();
  });

  it('has nothing to review for a draft with no saved name, and leads to Define', async () => {
    const w = world();
    await renderTab(w.service, null, 'golden', { saved: w.savedNames, index: w.index, golden: w.golden, evidence: w.evidence });

    expect(await screen.findByText('This draft has no saved name, so no saved content is here to approve a run of or to search from.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Go to Define' }).getAttribute('href')).toBe('/options-setups/_new/define');
    expect(screen.queryByRole('button', { name: 'Approve golden setup' })).toBeNull();
  });
});
