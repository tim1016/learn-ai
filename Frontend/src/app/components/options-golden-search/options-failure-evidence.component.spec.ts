import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { provideRouter } from '@angular/router';
import { expect, it, vi } from 'vitest';

import raw from '../../../../../PythonDataService/tests/fixtures/options_evidence/failure-development.json';
import type { components } from '../../api/broker.types';
import { OptionsFailureEvidenceComponent } from './options-failure-evidence.component';
import { OptionsGoldenSearchService } from './options-golden-search.service';

const FIXTURE: components['schemas']['OptionsFailureEvidence'] = { ...raw, scope: 'development', final_exposure: 'not_opened', items: raw.items.map(item => ({ ...item, code: 'missing_held_mark' })) };
type Inspection = components['schemas']['OptionsFailureInspection'];
const inspection = (code: Inspection['code'] = 'no_eligible_trade_bar'): Inspection => ({ study_id: FIXTURE.study_id, candidate_hash: FIXTURE.candidate_hash, evidence_identity: FIXTURE.evidence_identity, event_id: FIXTURE.items[0].id,
  code, retained_path: FIXTURE.items[0].retained_path, expected_input_sha256: FIXTURE.items[0].expected_input_sha256, snapshot_input_sha256: FIXTURE.items[0].expected_input_sha256,
  input_verified: code === 'no_eligible_trade_bar', contains_eligible_trade_bar: code === 'no_eligible_trade_bar' ? false : null, explanation: code === 'no_eligible_trade_bar' ? 'Verified retained input has no eligible trade bar; its upstream cause remains unknown.' : 'The archived file is unavailable. Current lake data cannot replace it.' });

it('shows partial scope and real close, and inspects retained input only on request', async () => {
  const inspectFailure = vi.fn(async () => inspection());
  await render(OptionsFailureEvidenceComponent, { inputs: { studyId: FIXTURE.study_id, digest: FIXTURE.candidate_hash }, providers: [provideRouter([]), { provide: OptionsGoldenSearchService, useValue: { failurePage: async () => FIXTURE, inspectFailure } }] });
  await screen.findByText('597 / 599 required held marks present; 2 missing.');
  expect(screen.getByText('0 / 6 eligible forward-fold winners. Final test: Not Opened.')).toBeTruthy();
  expect(screen.getAllByText(/Cause not established/)).toHaveLength(2);
  expect(screen.getAllByText(/Real closing fill:/)).toHaveLength(2);
  expect(inspectFailure).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getAllByRole('button', { name: 'Inspect retained input' })[0]);
  expect(await screen.findByText('No Eligible Trade Bar')).toBeTruthy();
  expect(inspectFailure).toHaveBeenCalledExactlyOnceWith(FIXTURE.study_id, FIXTURE.candidate_hash, 'development', FIXTURE.items[0]);
  expect(screen.getByRole('link', { name: 'Prepare a new linked plan' }).getAttribute('href')).toContain(`parent_study_id=${FIXTURE.study_id}`);
});

it('does not let an old input inspection overwrite a changed candidate', async () => {
  let finish: ((value: Inspection) => void) | undefined;
  const api = { failurePage: vi.fn(async () => FIXTURE), inspectFailure: vi.fn(() => new Promise<Inspection>(resolve => { finish = resolve; })) };
  const view = await render(OptionsFailureEvidenceComponent, { inputs: { studyId: FIXTURE.study_id, digest: FIXTURE.candidate_hash }, providers: [provideRouter([]), { provide: OptionsGoldenSearchService, useValue: api }] });
  await userEvent.setup().click((await screen.findAllByRole('button', { name: 'Inspect retained input' }))[0]);
  await screen.findByText('Verifying the selected archived file…');
  view.fixture.componentRef.setInput('digest', 'f'.repeat(64));
  view.fixture.detectChanges();
  await waitFor(() => expect(api.failurePage).toHaveBeenCalledTimes(2));
  finish?.(inspection());
  await view.fixture.whenStable();
  expect(screen.queryByText('No Eligible Trade Bar')).toBeNull();
  expect(screen.queryByRole('heading', { name: 'Selected input inspection' })).toBeNull();
});

it('keeps a failed diagnostic read actionable without starting a replay', async () => {
  const failurePage = vi.fn().mockRejectedValueOnce(new Error('Archived file unavailable.')).mockResolvedValue(FIXTURE);
  const inspectFailure = vi.fn();
  await render(OptionsFailureEvidenceComponent, { inputs: { studyId: FIXTURE.study_id, digest: FIXTURE.candidate_hash }, providers: [provideRouter([]), { provide: OptionsGoldenSearchService, useValue: { failurePage, inspectFailure } }] });
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Retry diagnostics' }));
  await screen.findByText('597 / 599 required held marks present; 2 missing.');
  expect(failurePage).toHaveBeenCalledTimes(2);
  expect(inspectFailure).not.toHaveBeenCalled();
});
