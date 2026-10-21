import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../../options-backtest/golden-option-setups.service';
import { exceptionGoldenSetup, goldenSetup } from '../../../options-backtest/testing/golden-fixture';
import type { OptionsBotConfiguration } from '../../broker-deploy-page/options-bot-deploy.service';
import { OptionsBotProofComponent } from './options-bot-proof.component';

const PINNED = goldenSetup({ source_run_id: 76 });
const CONFIGURATION: OptionsBotConfiguration = {
  golden_setup_id: PINNED.id, setup_sha256: PINNED.setup_sha256,
  rules_digest: PINNED.rules_digest, setup: PINNED.setup,
};

async function proofCard(retained = PINNED, configuration = CONFIGURATION) {
  const get = vi.fn().mockResolvedValue(retained);
  const result = await render(OptionsBotProofComponent, {
    inputs: { configuration },
    providers: [provideRouter([]), { provide: GoldenOptionSetupsService, useValue: { get } }],
  });
  await screen.findByText(`Approval note: ${retained.note}`);
  return { ...result, get };
}

describe('Options bot pinned evidence', () => {
  it('follows the pinned ID among same-name versions and never infers research from ordinary approval', async () => {
    const newer = goldenSetup({ id: 'ogs_' + '2'.repeat(32), name: PINNED.name, source_run_id: 99, setup_sha256: 'f'.repeat(64) });
    const versions = [newer, PINNED];
    const get = vi.fn(async (id: string) => {
      const version = versions.find(item => item.id === id);
      if (!version) throw new Error('Version not found');
      return version;
    });
    await render(OptionsBotProofComponent, {
      inputs: { configuration: CONFIGURATION },
      providers: [provideRouter([]), { provide: GoldenOptionSetupsService, useValue: { get } }],
    });
    await screen.findByText('Source backtest 76');
    expect(get).toHaveBeenCalledExactlyOnceWith(PINNED.id);
    expect(screen.queryByText('Source backtest 99')).toBeNull();
    expect(screen.getByText('Approved from backtest. No linked search qualification is recorded.')).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open recorded search' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Inspect pinned Golden evidence' }).getAttribute('href')).toContain(`/options-evidence/golden/${PINNED.id}?view=lineage`);
  });

  it.each(['stale', 'revoked'] as const)('refreshes %s execution proof without rewriting historical approval', async status => {
    const { get } = await proofCard();
    const reason = status === 'stale' ? 'Running rules have changed.' : 'The owner revoked this version.';
    get.mockResolvedValue({ ...PINNED, status, status_explanation: reason });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh execution proof' }));
    await screen.findByText(new RegExp(reason));
    expect(get).toHaveBeenLastCalledWith(PINNED.id);
    expect(screen.getByText(`Approval note: ${PINNED.note}`)).toBeTruthy();
    expect(screen.getByText('Approved from backtest. No linked search qualification is recorded.')).toBeTruthy();
  });

  it('retains the search manifest and permanent waivers separately from revoked execution proof', async () => {
    const retained = exceptionGoldenSetup();
    await proofCard({ ...retained, status: 'revoked', status_explanation: 'The owner revoked execution.',
      evidence: { ...retained.evidence, study_manifest_sha256: 'd'.repeat(64) } });
    expect(screen.getByText('Approved from search. Historical research: Owner Exception')).toBeTruthy();
    expect(screen.getByText('d'.repeat(64))).toBeTruthy();
    expect(screen.getByText(/Acknowledged exception: final:sharpe/i)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open recorded search' }).getAttribute('href')).toBe(`/options-golden-search/${retained.evidence.options_search_study_id}`);
    expect(screen.getByText(/Revoked. The owner revoked execution./)).toBeTruthy();
  });

  it.each([
    { study_manifest_sha256: null, verdict: 'meets_rules' as const },
    { study_manifest_sha256: 'd'.repeat(64), verdict: null },
  ])('retains an incomplete study reference without claiming search approval: %j', async evidence => {
    const study = 'os_' + 'd'.repeat(40);
    await proofCard({ ...PINNED, evidence: { ...PINNED.evidence, options_search_study_id: study, ...evidence } });
    expect(screen.getByText(/Approval origin is incompletely recorded/)).toBeTruthy();
    expect(screen.queryByText(/Approved from search/)).toBeNull();
    expect(screen.getByRole('link', { name: 'Open recorded search' }).getAttribute('href')).toBe(`/options-golden-search/${study}`);
  });

  it.each<Partial<GoldenOptionSetup>>([
    { setup_sha256: '0'.repeat(64) }, { rules_digest: '0'.repeat(64) }, { id: 'ogs_' + '0'.repeat(32) },
  ])('exposes a mismatched receipt alongside the pinned identity: %j', async changes => {
    const retained = { ...PINNED, ...changes };
    await proofCard(retained);
    expect(screen.getByRole('status').textContent).toContain('does not establish execution readiness for the bot');
    expect(screen.getByText(/^Exact setup/).textContent).toContain(PINNED.setup_sha256);
    expect(screen.getByText('Retained setup', { exact: false }).textContent).toContain(retained.setup_sha256);
    expect(screen.getByRole('link', { name: 'Inspect pinned Golden evidence' }).getAttribute('href')).toContain(PINNED.id);
  });
});
