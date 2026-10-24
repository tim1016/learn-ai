import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { GoldenOptionSetup } from '../../../components/options-backtest/golden-option-setups.service';
import { exampleBody } from '../../../components/options-backtest/testing/fixtures';
import { editSlippage, libraryWorld } from '../../testing/explore-actions';
import { approvalOf, indexOf, savedName, STUDY_ID, studyOf } from '../../testing/fixtures';
import { searchEvidence, versionOf } from '../../testing/golden-fixtures';
import { renderTab, setupUrl } from '../../testing/render-workspace';
import type { OptionsSetupTab } from '../../options-setups.address';
import type { OptionsSetupIndex } from '../../index/options-setup-index.service';

const id = (letter: string) => `ogs_${letter.repeat(32)}`;

const QUIET_BOTS = { kind: 'known' as const, bots: [] };

/** One saved setup, `Mine`, with the approved versions the server holds for it, each read by its own id. */
async function open(
  versions: GoldenOptionSetup[],
  index: OptionsSetupIndex,
  options: { name?: string | null; tab?: OptionsSetupTab; failing?: readonly string[]; index?: () => Promise<OptionsSetupIndex> } = {},
) {
  const library = libraryWorld(vi);
  library.library.set('Mine', exampleBody());
  library.library.set('Other', exampleBody());
  const get = vi.fn(async (versionId: string) => {
    if (options.failing?.includes(versionId)) throw new Error('store offline');
    const found = versions.find((version) => version.id === versionId);
    if (found === undefined) throw new Error('unknown version');
    return found;
  });
  const name = options.name === undefined ? 'Mine' : options.name;
  await renderTab(library.service, name, options.tab ?? 'overview', {
    index: options.index ?? (async () => index),
    bots: QUIET_BOTS,
    golden: { get, list: async () => [] },
    evidence: { read: vi.fn() },
  });
  return { get, library };
}

async function openChooser(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole('button', { name: 'Deploy an options bot' }));
  return screen.findByRole('region', { name: 'Deploy an options bot' });
}

const hrefs = (scope: HTMLElement) => within(scope).getAllByRole('link').map((link) => link.getAttribute('href'));

describe('Deploy an options bot, from a setup’s header', () => {
  it('is a header button on every tab that goes nowhere by itself', async () => {
    await open([versionOf(id('a'), '1')], indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('1', { id: id('a') })] }));

    const button = await screen.findByRole('button', { name: 'Deploy an options bot' });
    expect(screen.queryByRole('link', { name: 'Deploy an options bot' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Deploy an options bot' })).toBeNull();

    fireEvent.click(button);
    await screen.findByRole('region', { name: 'Deploy an options bot' });
    expect(decodeURIComponent(TestBed.inject(Router).url)).toBe('/options-setups/Mine/overview');

    await TestBed.inject(Router).navigateByUrl(setupUrl('Mine', 'golden'));
    expect(await screen.findByRole('button', { name: 'Deploy an options bot' })).toBeTruthy();
  });

  it('is not offered for a draft with no saved name, nor for a name the library does not hold', async () => {
    await open([], indexOf({}), { name: null, tab: 'define' });
    await screen.findByRole('heading', { name: 'Entry settings' });
    expect(screen.queryByRole('button', { name: 'Deploy an options bot' })).toBeNull();
  });

  it('is not offered for a name the library does not hold', async () => {
    await open([], indexOf({}), { name: 'No such setup' });

    expect((await screen.findByRole('alert')).textContent).toContain('No saved setup is named “No such setup”.');
    expect(screen.queryByRole('button', { name: 'Deploy an options bot' })).toBeNull();
  });

  it('lists each version by its own id and links a ready one under Deploy’s own key, chosen by the owner and never first or latest', async () => {
    await open(
      [versionOf(id('a'), '1', { source_run_id: 7 }), versionOf(id('b'), '1', { source_run_id: 9, evidence: searchEvidence(STUDY_ID) })],
      indexOf({
        setups: [savedName('Mine', '1')],
        studies: [studyOf('1')],
        approvals: [
          approvalOf('1', { id: id('a'), source_run_id: 7, origin: 'backtest', created_at_ms: 1_791_900_000_000 }),
          approvalOf('1', { id: id('b'), source_run_id: 9, study_id: STUDY_ID, origin: 'search', created_at_ms: 1_791_900_100_000 }),
        ],
      }),
    );

    const chooser = await openChooser();

    // Each version names the path that made it, and the older one is as offered as the newer.
    expect(await within(chooser).findByRole('link', { name: `Deploy version ${id('a')}` })).toBeTruthy();
    const older = within(chooser).getByRole('link', { name: `Deploy version ${id('a')}` });
    const newer = within(chooser).getByRole('link', { name: `Deploy version ${id('b')}` });
    expect(older.getAttribute('href')).toBe(`/brokers/alpaca?options_setup=${id('a')}`);
    expect(newer.getAttribute('href')).toBe(`/brokers/alpaca?options_setup=${id('b')}`);
    expect(within(chooser).getByText('Single-run approval')).toBeTruthy();
    expect(within(chooser).getByText('Golden search')).toBeTruthy();
    // Nothing is carried until the owner clicks one.
    expect(decodeURIComponent(TestBed.inject(Router).url)).toBe('/options-setups/Mine/overview');
  });

  it('chooses no version when there is only one', async () => {
    await open([versionOf(id('a'), '1')], indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('1', { id: id('a') })] }));

    await openChooser();

    expect(decodeURIComponent(TestBed.inject(Router).url)).toBe('/options-setups/Mine/overview');
  });

  it('lists a stale, revoked or unreadable version with why and no link, and offers no other in its place', async () => {
    await open(
      [
        versionOf(id('a'), '1', { status: 'stale', status_explanation: 'The rules changed since approval.' }),
        versionOf(id('b'), '1', { status: 'revoked', status_explanation: 'Revoked by the owner.' }),
        versionOf(id('c'), '1'),
        versionOf(id('d'), '1'),
      ],
      indexOf({
        setups: [savedName('Mine', '1')],
        approvals: [
          approvalOf('1', { id: id('a'), status: 'stale', created_at_ms: 4 }),
          approvalOf('1', { id: id('b'), status: 'revoked', created_at_ms: 3 }),
          approvalOf('1', { id: id('c'), created_at_ms: 2 }),
          approvalOf('1', { id: id('d'), created_at_ms: 1 }),
        ],
      }),
      { failing: [id('d')] },
    );

    const chooser = await openChooser();

    expect(await within(chooser).findByText('Not offered: The rules changed since approval.')).toBeTruthy();
    expect(within(chooser).getByText('Not offered: Revoked by the owner.')).toBeTruthy();
    expect(within(chooser).getByText('Not offered: this version could not be read by its own id.')).toBeTruthy();
    // Only the one ready version is offered, by its own id; the stale and revoked ones are not swapped for it.
    expect(within(chooser).getAllByRole('link', { name: /^Deploy version/ }).map((link) => link.getAttribute('href')))
      .toEqual([`/brokers/alpaca?options_setup=${id('c')}`]);
  });

  it('says the versions are of the content as last saved when Define holds an edit that is not saved', async () => {
    await open(
      [versionOf(id('a'), '1')],
      indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('1', { id: id('a') })] }),
      { tab: 'define' },
    );
    await screen.findByRole('heading', { name: 'Entry settings' });
    editSlippage('0.7');

    const chooser = await openChooser();

    expect(await within(chooser).findByText(/Define holds changes that are not saved/)).toBeTruthy();
    expect(await within(chooser).findByRole('link', { name: `Deploy version ${id('a')}` })).toBeTruthy();
  });

  it('lists a version a search made with changed settings, and says so', async () => {
    await open(
      [versionOf(id('e'), '5', { evidence: searchEvidence(STUDY_ID) })],
      indexOf({
        setups: [savedName('Mine', '1')],
        studies: [studyOf('1')],
        approvals: [approvalOf('5', { id: id('e'), study_id: STUDY_ID, origin: 'search' })],
      }),
    );

    const chooser = await openChooser();

    expect(await within(chooser).findByRole('link', { name: `Deploy version ${id('e')}` })).toBeTruthy();
    expect(within(chooser).getByText(/made by a search that began from this setup, with changed settings/)).toBeTruthy();
  });

  it('says no version holds this content, and points to Golden, rather than offering a version of other content', async () => {
    await open(
      [versionOf(id('a'), '9')],
      indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('9', { id: id('a') })] }),
    );

    const chooser = await openChooser();

    expect(await within(chooser).findByText(/No approved version holds this exact content/)).toBeTruthy();
    expect(within(chooser).queryByRole('link', { name: /^Deploy version/ })).toBeNull();
    expect(within(chooser).getByRole('link', { name: 'Approve a tested run, or start a golden search, in Golden' }).getAttribute('href'))
      .toBe('/options-setups/Mine/golden');
  });

  it('says when the list was cut, so that none does not mean none among the newest', async () => {
    await open(
      [versionOf(id('a'), '1')],
      indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('1', { id: id('a') })], totals: { approvals_total: 40 } }),
    );

    const chooser = await openChooser();

    expect(await within(chooser).findByText(/older ones may not be listed here/)).toBeTruthy();
  });

  it('says the versions could not be read, offers none, and reads them again on request', async () => {
    let fail = true;
    await open(
      [versionOf(id('a'), '1')],
      indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('1', { id: id('a') })] }),
      {
        index: async () => {
          if (fail) throw new Error('research database down');
          return indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('1', { id: id('a') })] });
        },
      },
    );
    // The tab beneath the panel reads the same evidence; the panel says it in its own words.
    const chooser = await openChooser();
    expect(await within(chooser).findByText('The approved versions of this setup could not be read.')).toBeTruthy();
    expect(within(chooser).queryByRole('link', { name: /^Deploy version/ })).toBeNull();

    fail = false;
    fireEvent.click(within(chooser).getByRole('button', { name: 'Read them again' }));
    expect(await within(chooser).findByRole('link', { name: `Deploy version ${id('a')}` })).toBeTruthy();
  });

  it('closes from its own button and from the header button, and stays closed for another setup', async () => {
    await open(
      [versionOf(id('a'), '1')],
      indexOf({ setups: [savedName('Mine', '1'), savedName('Other', '2')], approvals: [approvalOf('1', { id: id('a') })] }),
    );

    const chooser = await openChooser();
    fireEvent.click(within(chooser).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Deploy an options bot' })).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Deploy an options bot' }));
    await screen.findByRole('region', { name: 'Deploy an options bot' });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy an options bot' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Deploy an options bot' })).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Deploy an options bot' }));
    await screen.findByRole('region', { name: 'Deploy an options bot' });
    await TestBed.inject(Router).navigateByUrl(setupUrl('Other', 'overview'));
    await screen.findByRole('heading', { level: 1, name: 'Other' });
    expect(screen.queryByRole('region', { name: 'Deploy an options bot' })).toBeNull();
  });

  it('offers nothing about a signal program, which deploys from its own workflow', async () => {
    await open([versionOf(id('a'), '1')], indexOf({ setups: [savedName('Mine', '1')], approvals: [approvalOf('1', { id: id('a') })] }));

    const chooser = await openChooser();
    await within(chooser).findByRole('link', { name: `Deploy version ${id('a')}` });

    // Every address the panel offers is the account list under the options key; none carries a strategy or a signal version.
    for (const link of hrefs(chooser).filter((href) => href?.startsWith('/brokers'))) {
      expect(link).toBe(`/brokers/alpaca?options_setup=${id('a')}`);
    }
  });
});
