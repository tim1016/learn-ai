import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { DeployConfirmStepComponent } from './deploy-confirm-step.component';

describe('Deploy startup estimate', () => {
  it('shows the backend estimate and stop warning before confirmation', async () => {
    const note = 'Spawning: needs about 10 more trading sessions of live bars before it decides. Spawn progress is lost if the bot stops.';
    await render(DeployConfirmStepComponent, { inputs: {
      trades: 'SPY shares', exits: 'Sell at the end', spawningNote: note,
    } });
    expect(screen.getByText('Startup')).toBeTruthy();
    expect(screen.getByText(note)).toBeTruthy();
  });
});
