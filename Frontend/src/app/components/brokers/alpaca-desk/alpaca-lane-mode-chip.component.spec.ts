import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { LaneModeChip } from '../../../services/alpaca-live-verdict.service';
import { AlpacaLaneModeChipComponent } from './alpaca-lane-mode-chip.component';

describe('AlpacaLaneModeChipComponent', () => {
  it('renders the paper tone and its mode text', async () => {
    const chip: LaneModeChip = { tone: 'paper', mode: 'Paper money', armedCount: null, shadow: false };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    const rendered = screen.getByText('Paper money');
    expect(rendered.classList.contains('lane-mode-chip--paper')).toBe(true);
  });

  it('names the armed count and the Shadow authority on a live lane', async () => {
    const chip: LaneModeChip = { tone: 'live', mode: 'Live', armedCount: 2, shadow: true };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    expect(screen.getByText('Live').classList.contains('lane-mode-chip--live')).toBe(true);
    expect(screen.getByText('· 2 armed')).toBeTruthy();
    expect(screen.getByText('· Shadow')).toBeTruthy();
  });

  it('renders the loud undetermined tone with neither detail span', async () => {
    const chip: LaneModeChip = {
      tone: 'undetermined',
      mode: 'Mode unknown — assume real money',
      armedCount: null,
      shadow: false,
    };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    const rendered = screen.getByText('Mode unknown — assume real money');
    expect(rendered.classList.contains('lane-mode-chip--undetermined')).toBe(true);
    expect(screen.queryByText('· Shadow')).toBeNull();
  });
});
