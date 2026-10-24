import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { expect, it } from 'vitest';

import { OptionsCandidateEvidenceComponent, type CandidateEvidence } from './options-candidate-evidence.component';
import { retainedCandidate } from './testing';

const CARRIED = /required held marks valued at the previous same-session traded close/;

async function show(read: CandidateEvidence): Promise<void> {
  await render(OptionsCandidateEvidenceComponent, { inputs: { studyId: 'study-1', digest: read.candidate_hash, loader: async () => read }, providers: [provideRouter([])] });
}

it('names the original mark rule without claiming it carries any price', async () => {
  await show(retainedCandidate());
  expect(await screen.findByText('options-held-minute-close-v1')).toBeTruthy();
  expect(screen.getByText('Required held marks: 10. Missing held marks: 0. Synthetic retained trace for UI behavior.')).toBeTruthy();
  expect(screen.queryByText(CARRIED)).toBeNull();
  expect(screen.queryByText(/carried price/)).toBeNull();
});

it('names the carrying mark rule and the marks it carried beside the held-mark coverage', async () => {
  const retained = retainedCandidate();
  await show({ ...retained, coverage: { ...retained.coverage, mark_rule: 'options-held-minute-close-v2', carried_held_marks: 3, carried_share_percent: '30', longest_carried_run: 2 } });
  expect(await screen.findByText('options-held-minute-close-v2')).toBeTruthy();
  expect(screen.getByText(/3 required held marks valued at the previous same-session traded close \(30% of required\); longest carried run 2 minutes\. A carried price is an assumption/)).toBeTruthy();
});
