import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SavedSetup, SetupRequest } from '../components/options-backtest/options-backtest.types';
import { fakeService } from '../components/options-backtest/testing/fake-service';
import { EXAMPLE_NO_REENTRY, EXAMPLE_ONE_REENTRY, readBackOf, exampleBody, servedExamples } from '../components/options-backtest/testing/fixtures';
import { typeInto } from '../components/options-backtest/testing/page-actions';
import type { OptionsSetupIndex } from './index/options-setup-index.service';
import { approvalOf, GOLDEN_ID, indexOf, runOf, savedName } from './testing/fixtures';
import { renderDefine, setupUrl } from './testing/render-workspace';

/**
 * T14 (PRD #3490): two saved names hold identical content; one is edited, one is
 * deleted, and its name is reused. The current drafts and the exact evidence stay
 * distinct: the old evidence stays reachable from the list, and no approval moves.
 *
 * The fake server below keeps a library of saved names and one run and one approved
 * version that froze the content the first two names held. It hashes a saved setup
 * the way the real one does in kind (equal content, equal hash), so what the page
 * does with the hashes is what is under test.
 */

/** A stand-in for the server's content hash: the same JSON, in any key order, is the same digest. */
function hashOf(setup: object): string {
  const text = JSON.stringify(setup, (_key, value: unknown) =>
    value !== null && typeof value === 'object' && !Array.isArray(value) ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value,
  );
  let digest = '';
  for (let seed = 0; digest.length < 64; seed += 1) {
    let h = 2166136261 ^ seed;
    for (const char of text) h = Math.imul(h ^ char.charCodeAt(0), 16777619);
    digest += (h >>> 0).toString(16).padStart(8, '0');
  }
  return digest.slice(0, 64);
}

const FROZEN = hashOf(readBackOf(exampleBody()));

function world() {
  const library = new Map<string, SetupRequest>();
  const service = fakeService(vi, {
    list: vi.fn(async (): Promise<SavedSetup[]> => [
      ...servedExamples(),
      ...[...library].map(([name, setup]) => ({ name, example: false, saved_at_ms: 1_760_000_000_000, cannot_open_reason: null, setup: readBackOf(setup) })),
    ]),
    save: vi.fn(async (name: string, setup: SetupRequest): Promise<SavedSetup> => {
      library.set(name, setup);
      return { name, example: false, saved_at_ms: 1_760_000_000_000, cannot_open_reason: null, setup: readBackOf(setup) };
    }),
    remove: vi.fn(async (name: string) => {
      library.delete(name);
    }),
  });
  /** What the evidence read answers now: the saved names as they hold content today, and the evidence as it was frozen. */
  const index = async (): Promise<OptionsSetupIndex> =>
    indexOf({
      setups: [...library].map(([name, setup]) => savedName(name, '0', { setup_sha256: hashOf(setup) })),
      runs: [runOf(5, '0', { setup_sha256: FROZEN, recorded_name: 'Alpha' })],
      approvals: [approvalOf('0', { setup_sha256: FROZEN, recorded_name: 'Alpha', source_run_id: 5 })],
    });
  return { library, service, index };
}

const evidenceRows = async () => within(await screen.findByRole('region', { name: 'Runs, searches and approved versions' }));
const savedRows = async () => within(await screen.findByRole('region', { name: 'Saved setups' }));
const nameField = () => screen.getByLabelText<HTMLInputElement>('Setup name');
const saveButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Save setup' });
const go = (url: string) => TestBed.inject(Router).navigateByUrl(url);

async function saveAs(name: string): Promise<void> {
  typeInto('Setup name', name);
  await waitFor(() => expect(saveButton().disabled).toBe(false));
  fireEvent.click(saveButton());
  await screen.findByText(new RegExp(`Saved “${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}”`));
}

describe('saved names, edits, deletes and reused names (T14)', () => {
  it('leaves old evidence reachable and every approval where it was, whatever happens to the names', async () => {
    const server = world();
    // Two names are saved from one setup: "Alpha", then "Beta" as a copy of it. Identical content.
    await renderDefine(server.service, undefined, { setupName: EXAMPLE_ONE_REENTRY, index: server.index });
    await screen.findByRole('article', { name: 'Leg 2' });
    await saveAs('Alpha');
    await saveAs('Beta');
    expect(hashOf(server.library.get('Alpha') ?? {})).toBe(hashOf(server.library.get('Beta') ?? {}));

    await go('/options-setups');
    expect(await (await savedRows()).findByRole('link', { name: 'Beta' })).toBeTruthy();
    const run = (await evidenceRows()).getByRole('row', { name: /Run 5/ });
    // Both names hold the content the run and the version froze, and neither owns it.
    expect(within(run).getByRole('link', { name: '“Alpha”' })).toBeTruthy();
    expect(within(run).getByRole('link', { name: '“Beta”' })).toBeTruthy();
    expect(run.textContent).toContain('none owns it');

    // "Alpha" is edited and saved over: it no longer holds what the evidence froze, and "Beta" still does.
    await go(setupUrl('Alpha', 'define'));
    await waitFor(() => expect(nameField().value).toBe('Alpha'));
    typeInto('Slippage (percent)', '0.9');
    await waitFor(() => expect(saveButton().disabled).toBe(false));
    fireEvent.click(saveButton());
    await waitFor(() => expect(server.library.get('Alpha')?.slippage_percent).toBe('0.9'));
    await go('/options-setups');
    await (await savedRows()).findByRole('link', { name: 'Alpha' });
    const afterEdit = (await evidenceRows()).getByRole('row', { name: /Run 5/ });
    expect(within(afterEdit).queryByRole('link', { name: '“Alpha”' })).toBeNull();
    expect(within(afterEdit).getByRole('link', { name: '“Beta”' })).toBeTruthy();
    // Though the run was recorded under the name "Alpha", which now holds other content.
    expect(afterEdit.textContent).toContain('Recorded as “Alpha”');

    // "Beta" is deleted. The evidence stays listed and opens by its id; no saved setup holds it now.
    fireEvent.click((await savedRows()).getByRole('button', { name: 'Delete setup Beta' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(server.library.has('Beta')).toBe(false));
    await waitFor(() => expect(screen.queryByRole('link', { name: 'Beta' })).toBeNull());
    const orphan = (await evidenceRows()).getByRole('row', { name: /Run 5/ });
    expect(orphan.textContent).toContain('No saved setup holds this content now');
    expect(within(orphan).getByRole('link', { name: 'Run 5' }).getAttribute('href')).toBe('/options-setups/evidence/run/5');
    expect(within((await evidenceRows()).getByRole('row', { name: new RegExp(GOLDEN_ID) })).getByRole('link', { name: `Approved version ${GOLDEN_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/golden/${GOLDEN_ID}`);

    // The name "Beta" is reused for different content. The version does not move onto it.
    await go(setupUrl(EXAMPLE_NO_REENTRY, 'define'));
    await screen.findByRole('article', { name: 'Leg 2' });
    await saveAs('Beta');
    expect(hashOf(server.library.get('Beta') ?? {})).not.toBe(FROZEN);
    await go(setupUrl('Beta', 'overview'));
    const approved = await screen.findByRole('region', { name: 'Approved versions of this content' });
    expect(approved.textContent).toContain('No approved version holds this exact content.');
    expect(screen.getByRole('region', { name: 'Last backtest of this content' }).textContent).toContain('No run of this exact content is recorded.');

    await go('/options-setups');
    await (await savedRows()).findByRole('link', { name: 'Beta' });
    const stillThere = (await evidenceRows()).getByRole('row', { name: new RegExp(GOLDEN_ID) });
    expect(stillThere.textContent).toContain('Recorded as “Alpha”');
    expect(stillThere.textContent).toContain('No saved setup holds this content now');
    expect(stillThere.textContent).toContain('Ready');
    expect(within(stillThere).queryByRole('link', { name: '“Beta”' })).toBeNull();
    expect(server.service.save).toHaveBeenCalledWith('Beta', expect.objectContaining({ underlying: 'SPY' }));
  });

  it('creates a library entry, and no approval, when a setup is saved under another name, and shows the version it shares', async () => {
    const server = world();
    await renderDefine(server.service, undefined, { setupName: EXAMPLE_ONE_REENTRY, index: server.index });
    await screen.findByRole('article', { name: 'Leg 2' });
    await saveAs('Gamma');

    await go(setupUrl('Gamma', 'overview'));

    const approved = await screen.findByRole('region', { name: 'Approved versions of this content' });
    // The one version that froze this content, found by the content; saving added nothing to the approved versions.
    expect(within(approved).getAllByRole('article')).toHaveLength(1);
    expect(within(approved).getByRole('link', { name: `Open exact version ${GOLDEN_ID}` })).toBeTruthy();
    expect((await server.index()).approvals_total).toBe(1);
    expect(server.service.save).toHaveBeenCalledTimes(1);
  });
});
