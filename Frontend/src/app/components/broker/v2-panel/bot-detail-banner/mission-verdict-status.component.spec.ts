import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { MissionVerdictStatusComponent } from './mission-verdict-status.component';

describe('MissionVerdictStatusComponent', () => {
  it('renders the backend-provided verdict as one live status token', async () => {
    await render(MissionVerdictStatusComponent, {
      inputs: {
        verdict: {
          state: 'blocked',
          label: 'Mission blocked',
          explanation: 'Clerk hold.',
          next_action: 'Resolve the hold.',
          evaluated_at_ms: 1_753_800_001_000,
        },
      },
    });

    const status = screen.getByRole('status', { name: 'Mission blocked' });
    expect(status.getAttribute('data-state')).toBe('blocked');
  });

  it('drops the chip decoration in bare mode without losing the accessible status', async () => {
    await render(MissionVerdictStatusComponent, {
      inputs: {
        verdict: {
          state: 'off_duty',
          label: 'Off duty',
          explanation: 'Not scheduled to run right now.',
          next_action: null,
          evaluated_at_ms: 1_753_800_001_000,
        },
        bare: true,
      },
    });

    const status = screen.getByRole('status', { name: 'Off duty' });
    expect(status.classList.contains('mission-verdict-status--bare')).toBe(true);
  });
});
