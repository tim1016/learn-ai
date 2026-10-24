import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { StrategyProofDossier } from '../../../services/strategy-validation.types';
import { StrategyProofPipelineComponent } from './strategy-proof-pipeline.component';

const STALE_PROOF: StrategyProofDossier = {
  state: 'stale',
  completed_stages: 1,
  total_stages: 2,
  blocking_stage_id: 'reference_source',
  blocking_summary: 'Upload the current audit algorithm.',
  stages: [
    {
      stage_id: 'program_contract',
      title: 'Signal Program contract',
      state: 'complete',
      authority: 'Strategy registry',
      summary: 'The program contract is qualified.',
      next_step: null,
      actions: [],
      evidence: [],
    },
    {
      stage_id: 'reference_source',
      title: 'QuantConnect reference algorithm',
      state: 'stale',
      authority: 'Committed QuantConnect audit copy',
      summary: 'The audit copy changed after the recorded reference run.',
      next_step: 'Upload the current audit algorithm.',
      actions: [
        {
          kind: 'external_link',
          label: 'QuantConnect project files guide',
          href: 'https://www.quantconnect.com/docs/v2/cloud-platform/projects/files',
        },
      ],
      evidence: [
        {
          label: 'Reference audit copy',
          ref: 'references/qc-shadow/Example.py',
          state: 'stale',
          recorded_sha256: 'recorded-sha',
          current_sha256: 'current-sha',
        },
      ],
    },
  ],
};

describe('StrategyProofPipelineComponent', () => {
  it('shows the first blocker, recovery link, and recorded-versus-current evidence', async () => {
    await render(StrategyProofPipelineComponent, {
      inputs: { proof: STALE_PROOF },
    });

    expect(screen.getByRole('heading', { name: 'Strategy proof' })).toBeTruthy();
    expect(screen.getByText('1 of 2 required stages complete')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain(
      'Next: Upload the current audit algorithm.',
    );

    const reference = screen.getByRole('link', { name: /QuantConnect project files guide/ });
    expect(reference.getAttribute('href')).toBe(
      'https://www.quantconnect.com/docs/v2/cloud-platform/projects/files',
    );
    expect(reference.getAttribute('target')).toBe('_blank');
    expect(reference.getAttribute('rel')).toContain('noopener');

    const details = screen.getByText('Technical evidence');
    details.click();
    expect(screen.getByText('recorded-sha')).toBeTruthy();
    expect(screen.getByText('current-sha')).toBeTruthy();
  });
});
