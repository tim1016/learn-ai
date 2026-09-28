import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { LaneModeChip } from '../../../services/alpaca-live-verdict.service';
import { AlpacaLaneModeChipComponent } from './alpaca-lane-mode-chip.component';

describe('AlpacaLaneModeChipComponent', () => {
  it('renders the paper tone and its mode text', async () => {
    const chip: LaneModeChip = { tone: 'paper', mode: 'Paper money' };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    const rendered = screen.getByText('Paper money');
    expect(rendered.classList.contains('lane-mode-chip--paper')).toBe(true);
  });

  it('names the Shadow simulation world with its own tone', async () => {
    const chip: LaneModeChip = { tone: 'shadow', mode: 'Shadow' };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    expect(screen.getByText('Shadow').classList.contains('lane-mode-chip--shadow')).toBe(true);
    expect(screen.queryByText(/armed/)).toBeNull();
  });

  it('renders the loud undetermined tone with neither detail span', async () => {
    const chip: LaneModeChip = {
      tone: 'undetermined',
      mode: 'Mode unknown — assume real money',
    };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    const rendered = screen.getByText('Mode unknown — assume real money');
    expect(rendered.classList.contains('lane-mode-chip--undetermined')).toBe(true);
    expect(screen.queryByText('· Shadow')).toBeNull();
  });
});
