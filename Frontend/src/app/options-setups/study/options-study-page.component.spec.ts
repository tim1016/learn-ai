import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { OptionsBacktestService } from '../../components/options-backtest/options-backtest.service';
import { OptionsEvidenceService } from '../../components/options-evidence/options-evidence.service';
import { OptionsResearchLineageService } from '../../components/options-evidence/options-research-lineage.service';
import { STUDY_WORKSPACE } from '../../components/options-evidence/testing';
import { OptionsGoldenSearchService } from '../../components/options-golden-search/options-golden-search.service';
import { study } from '../../components/options-golden-search/testing';
import type { SavedSetup } from '../../components/options-backtest/options-backtest.types';
import { savedName } from '../testing/fixtures';
import { OptionsStudyPageComponent } from './options-study-page.component';

const BOT = '/brokers/alpaca/clerks/paper/accounts/PA9/bots/options-1';

async function page(inputs: Record<string, unknown> = {}, saved: SavedSetup[] = []) {
  const searches = { get: vi.fn(async (_id: string) => study({ id: STUDY_WORKSPACE.subject_id })), command: vi.fn() };
  const read = vi.fn(async () => STUDY_WORKSPACE);
  await render(OptionsStudyPageComponent, {
    inputs: { studyId: STUDY_WORKSPACE.subject_id, ...inputs },
    providers: [
      provideRouter([]),
      { provide: OptionsGoldenSearchService, useValue: searches },
      { provide: OptionsEvidenceService, useValue: { read } },
      { provide: OptionsResearchLineageService, useValue: { read: async () => { throw new Error('Retained procedure unavailable'); } } },
      { provide: OptionsBacktestService, useValue: { list: async () => saved, describe: () => new Promise(() => undefined) } },
    ],
  });
  return { searches, read };
}

describe('One golden search study, at the address it has by its own id', () => {
  it('opens on the study to work on, by its own id, and reads no lineage until it is asked for', async () => {
    const { searches, read } = await page();

    expect(await screen.findByRole('button', { name: 'Run research' })).toBeTruthy();
    expect(searches.get).toHaveBeenCalledExactlyOnceWith(STUDY_WORKSPACE.subject_id);
    expect(read).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'Study' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: 'Research lineage' }).getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_WORKSPACE.subject_id}?view=lineage`);
  });

  it('shows the study’s recorded lineage, and which saved setups hold the content it started from, at ?view=lineage', async () => {
    const { searches, read } = await page(
      { view: 'lineage' },
      [savedName('Alpha', '0', { setup_sha256: STUDY_WORKSPACE.setup_sha256 })],
    );

    expect(await screen.findByRole('heading', { name: 'This search did not establish a result.' })).toBeTruthy();
    expect(read).toHaveBeenCalledWith('study', STUDY_WORKSPACE.subject_id, undefined);
    expect(searches.get).not.toHaveBeenCalled();
    const holders = await screen.findByText(/Saved setups that hold this exact content today/);
    expect(holders.textContent).toContain('“Alpha”');
    expect(screen.getByRole('link', { name: 'Study' }).getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_WORKSPACE.subject_id}`);
  });

  it.each([
    [BOT, true],
    ['https://outside.example/bots/options-1', false],
  ])('goes back only to an exact in-app bot route: %s', async (bot_return, allowed) => {
    await page({ bot_return });

    await screen.findByRole('button', { name: 'Run research' });
    expect(screen.queryByRole('link', { name: 'Back to bot' })?.getAttribute('href') ?? null).toBe(allowed ? bot_return : null);
    expect(screen.getByRole('link', { name: 'Research lineage' }).getAttribute('href')?.includes('bot_return=') ?? false).toBe(allowed);
    expect(screen.getByRole('link', { name: 'Options setups' }).getAttribute('href')).toBe('/options-setups');
  });
});
