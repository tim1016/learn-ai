import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { expect, it } from 'vitest';

import { OptionsCandidateEvidenceComponent } from './options-candidate-evidence.component';
import { retainedCandidate } from './testing';

it('names the mark rule and the marks it carried beside the held-mark coverage', async () => {
  const retained = retainedCandidate();
  const carried = { ...retained, coverage: { ...retained.coverage, mark_rule: 'options-held-minute-close-v2', carried_held_marks: 3, carried_share_percent: '30', longest_carried_run: 2 } };
  await render(OptionsCandidateEvidenceComponent, { inputs: { studyId: 'study-1', digest: retained.candidate_hash, loader: async () => carried }, providers: [provideRouter([])] });
  expect(await screen.findByText('options-held-minute-close-v2')).toBeTruthy();
  expect(screen.getByText(/3 required held marks valued at the previous same-session traded close \(30% of required\); longest carried run 2 minutes\./)).toBeTruthy();
  expect(screen.getByText('Required held marks: 10. Missing held marks: 0. Synthetic retained trace for UI behavior.')).toBeTruthy();
});
