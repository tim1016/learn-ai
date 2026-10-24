import { fireEvent, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { GoldenOptionSetup } from '../../../components/options-backtest/golden-option-setups.service';
import { exampleBody } from '../../../components/options-backtest/testing/fixtures';
import { libraryWorld } from '../../testing/explore-actions';
import { approvalOf, indexOf, runOf, savedName, STUDY_ID, studyOf } from '../../testing/fixtures';
import { searchEvidence, versionOf } from '../../testing/golden-fixtures';
import { renderTab } from '../../testing/render-workspace';

const id = (letter: string) => `ogs_${letter.repeat(32)}`;
const OTHER_STUDY = `os_${'c'.repeat(40)}`;

/** The versions the server holds, each read by its own id, and the index that lists them. */
async function golden(versions: GoldenOptionSetup[], index = indexOf({}), failing: readonly string[] = []) {
  const library = libraryWorld(vi);
  library.library.set('Mine', exampleBody());
  const get = vi.fn(async (versionId: string) => {
    if (failing.includes(versionId)) throw new Error('store offline');
    const found = versions.find((version) => version.id === versionId);
    if (found === undefined) throw new Error('unknown version');
    return found;
  });
  await renderTab(library.service, 'Mine', 'golden', { saved: [savedName('Mine', '1')], index: async () => index, golden: { get, list: async () => [] }, evidence: { read: vi.fn() } });
  return { get };
}

const article = (versionId: string) => {
  const found = document.querySelector<HTMLElement>(`[data-version="${versionId}"]`);
  if (found === null) throw new Error(`No version ${versionId} is listed.`);
  return within(found);
};

describe('the versions are read once for the workspace', () => {
  it('reads neither the evidence nor any version again when the owner leaves Golden and comes back', async () => {
    const library = libraryWorld(vi);
    library.library.set('Mine', exampleBody());
    const read = vi.fn(async () => indexOf({ approvals: [approvalOf('1', { id: id('a') })] }));
    const get = vi.fn(async (versionId: string) => versionOf(versionId, '1'));
    await renderTab(library.service, 'Mine', 'golden', { saved: [savedName('Mine', '1')], index: read, golden: { get, list: async () => [] }, evidence: { read: vi.fn() } });
    await screen.findByText('Single-run approval');

    const tab = (name: string) => within(screen.getByRole('navigation', { name: 'Options setup tabs' })).getByRole('link', { name });
    fireEvent.click(tab('Define'));
    await screen.findByRole('heading', { name: 'Entry settings' });
    fireEvent.click(tab('Golden'));
    await screen.findByText('Single-run approval');

    expect(read).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledTimes(1);
  });
});

describe('each approved version says which path made it and what it came from', () => {
  it('words a single-run approval as one run’s approval, never as a qualified search', async () => {
    await golden(
      [versionOf(id('a'), '1', { source_run_id: 7, note: 'Looked right.' })],
      indexOf({ approvals: [approvalOf('1', { id: id('a'), source_run_id: 7, origin: 'backtest' })] }),
    );

    const version = article(id('a'));
    expect(await version.findByText('Single-run approval')).toBeTruthy();
    expect(version.getByText(/No golden search ran for it, so no statistical qualification is claimed/)).toBeTruthy();
    expect(version.getByRole('link', { name: 'Run 7' }).getAttribute('href')).toBe('/options-setups/evidence/run/7');
    expect(version.queryByText('Golden search')).toBeNull();
    expect(version.getByText('Approved by owner: Looked right.')).toBeTruthy();
  });

  it('words a search version as a golden search with its study and the verdict the study recorded', async () => {
    await golden(
      [versionOf(id('b'), '1', { source_run_id: 9, evidence: searchEvidence(STUDY_ID) })],
      indexOf({
        studies: [studyOf('1')],
        approvals: [approvalOf('1', { id: id('b'), source_run_id: 9, study_id: STUDY_ID, origin: 'search' })],
      }),
    );

    const version = article(id('b'));
    expect(await version.findByText('Golden search')).toBeTruthy();
    expect(version.getByText('The search recorded that this candidate met its rules.')).toBeTruthy();
    expect(version.getByRole('link', { name: `study ${STUDY_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_ID}`);
    expect(version.getByRole('link', { name: 'Run 9' }).getAttribute('href')).toBe('/options-setups/evidence/run/9');
    expect(version.queryByText('Single-run approval')).toBeNull();
  });

  it('keeps an owner exception, its failed rule, the waiver and the exposure with the version, and lists its limitations', async () => {
    await golden(
      [versionOf(id('b'), '1', { evidence: searchEvidence(STUDY_ID, 'owner_exception') })],
      indexOf({
        studies: [studyOf('1')],
        approvals: [approvalOf('1', { id: id('b'), study_id: STUDY_ID, origin: 'search' })],
      }),
    );

    const version = article(id('b'));
    expect(await version.findByText(/did not meet every rule, and the failed rules stay with it/)).toBeTruthy();
    expect(version.getByText('Owner exception', { exact: true })).toBeTruthy();
    expect(version.getByText(/The final Sharpe hurdle was not met/)).toBeTruthy();
    expect(version.getByText(/Acknowledged exception: Final:sharpe/i)).toBeTruthy();
    expect(version.getByText(/The final interval was used in recorded research/)).toBeTruthy();
    expect(within(version.getByRole('list', { name: 'Limitations' })).getByText('Trade bars do not validate quote spreads or actual live fills.')).toBeTruthy();
  });

  it('claims no search for a version whose study link is incomplete', async () => {
    await golden(
      [versionOf(id('d'), '1', { evidence: { ...searchEvidence(STUDY_ID), study_manifest_sha256: null } })],
      indexOf({
        approvals: [approvalOf('1', { id: id('d'), study_id: STUDY_ID, origin: 'not_recorded' })],
      }),
    );

    const version = article(id('d'));
    expect(await version.findByText('Approval path not recorded')).toBeTruthy();
    expect(version.getByText(/No search qualification is claimed for this version/)).toBeTruthy();
    expect(version.queryByText('The search recorded that this candidate met its rules.')).toBeNull();
  });
});

describe('the versions of a setup are found by exact content, and through the study that changed it', () => {
  it('lists a changed candidate under the study it came from, and never among the exact matches', async () => {
    await golden(
      [versionOf(id('a'), '1'), versionOf(id('e'), '5', { evidence: searchEvidence(STUDY_ID) })],
      indexOf({
        studies: [studyOf('1')],
        approvals: [
          approvalOf('1', { id: id('a') }),
          approvalOf('5', { id: id('e'), study_id: STUDY_ID, origin: 'search', source_run_id: 11, recorded_name: 'Mine' }),
        ],
      }),
    );

    const exact = await screen.findByRole('heading', { name: 'Of exactly this content' });
    const linked = screen.getByRole('heading', { name: 'Made by searches that began from this content, with changed settings' });
    const articles = [...document.querySelectorAll('[data-version]')].map((node) => node.getAttribute('data-version'));
    expect(articles).toEqual([id('a'), id('e')]);
    expect(exact.compareDocumentPosition(article(id('a')).getByText('Single-run approval')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(linked.compareDocumentPosition(article(id('e')).getByText('Golden search')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const changed = article(id('e'));
    expect(changed.getByText(/changed its settings and made this version, so its content is not what is saved now/)).toBeTruthy();
    expect(changed.getAllByRole('link', { name: `study ${STUDY_ID}` })[0].getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_ID}`);
    expect(article(id('a')).queryByText(/changed its settings/)).toBeNull();
  });

  it('never lists a version by the name it was recorded under: other content, and no study of this content, is not this setup’s', async () => {
    await golden(
      [versionOf(id('f'), '6')],
      indexOf({
        studies: [studyOf('1')],
        approvals: [
          approvalOf('6', { id: id('f'), recorded_name: 'Mine' }),
          approvalOf('7', { id: id('g'), recorded_name: 'Mine', study_id: OTHER_STUDY, origin: 'search' }),
        ],
      }),
    );

    expect(await screen.findByText('No approved version holds this exact content.')).toBeTruthy();
    expect(document.querySelector('[data-version]')).toBeNull();
    expect(screen.queryByRole('heading', { name: /Made by searches/ })).toBeNull();
  });

  it('says a list was cut at its limit, so none is not taken for none at all', async () => {
    await golden([], indexOf({ totals: { approvals_total: 1500 } }));

    expect(await screen.findByText('No approved version holds this exact content, among the newest 0 of 1500.')).toBeTruthy();
  });

  it('lists the searches that began from exactly this content, each at its own address', async () => {
    await golden([], indexOf({ studies: [studyOf('1'), studyOf('9', { id: OTHER_STUDY })] }));

    const searches = await screen.findByRole('region', { name: 'Start a golden search' });
    expect(within(searches).getAllByRole('listitem').map((item) => item.getAttribute('data-study'))).toEqual([STUDY_ID]);
    expect(within(searches).getByRole('link', { name: `Study ${STUDY_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_ID}`);
  });
});

describe('what happened to a version since it was approved', () => {
  it('lists a revocation with its reason, and offers no review to revoke or re-prove what is revoked', async () => {
    await golden(
      [versionOf(id('a'), '1', { status: 'revoked', status_explanation: 'The owner revoked this Golden setup.', events: [{ id: 3, kind: 'revoked', rules_digest: null, reason: 'Stop using this evidence', actor: 'owner', created_at_ms: 1_791_900_100_000 }] })],
      indexOf({ approvals: [approvalOf('1', { id: id('a'), status: 'revoked' })] }),
    );

    const version = article(id('a'));
    const events = await version.findByRole('list', { name: 'Revoke and re-proof events' });
    expect(events.textContent).toContain('Revoked by owner');
    expect(events.textContent).toContain('Stop using this evidence');
    expect(version.getByText('The owner revoked this Golden setup.')).toBeTruthy();
    expect(version.queryByRole('link', { name: /Review run/ })).toBeNull();
  });

  it('says a re-proof replayed execution and recertified nothing, and leads to the run whose review re-proves or revokes', async () => {
    await golden(
      [versionOf(id('a'), '1', { source_run_id: 7, events: [{ id: 4, kind: 'reproved', rules_digest: 'f'.repeat(64), reason: null, actor: 'owner', created_at_ms: 1_791_900_200_000 }] })],
      indexOf({ approvals: [approvalOf('1', { id: id('a'), source_run_id: 7 })] }),
    );

    const version = article(id('a'));
    const events = await version.findByRole('list', { name: 'Revoke and re-proof events' });
    expect(events.textContent).toContain('Re-proved by owner');
    expect(events.textContent).toContain('This is not a recertification');
    expect(version.getByRole('link', { name: 'Review run 7' }).getAttribute('href')).toBe('/options-setups/Mine/golden?run=7');
  });

  it('says a version could not be read by its own id, and still lists what the index knows of it', async () => {
    await golden(
      [],
      indexOf({ approvals: [approvalOf('1', { id: id('a'), source_run_id: 7 })] }),
      [id('a')],
    );

    const version = article(id('a'));
    expect(await version.findByText('This version could not be read by its own id, so its proof is not shown.')).toBeTruthy();
    expect(version.getByText('Single-run approval')).toBeTruthy();
    expect(version.getByRole('link', { name: 'Run 7' })).toBeTruthy();
  });
});

describe('the runs and searches the tab lists', () => {
  it('offers only the completed runs of the saved content to review', async () => {
    await golden([], indexOf({ runs: [runOf(7, '1'), runOf(8, '2'), runOf(9, '1', { status: 'failed' })] }));

    const region = await screen.findByRole('region', { name: 'Approve a tested run' });
    expect(within(region).getAllByRole('link', { name: /^Review run/ }).map((link) => link.textContent)).toEqual(['Review run 7']);
  });
});
