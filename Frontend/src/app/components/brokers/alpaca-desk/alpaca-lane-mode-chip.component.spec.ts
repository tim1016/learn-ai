import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import {
  UNPOLLED_LANE_STATE,
  verdictModeChip,
  type LaneModeChip,
} from '../../../services/alpaca-live-verdict.service';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { AlpacaLaneModeChipComponent } from './alpaca-lane-mode-chip.component';

describe('AlpacaLaneModeChipComponent', () => {
  it('renders the paper tone and its mode text', async () => {
    const chip: LaneModeChip = { tone: 'paper', mode: 'PAPER · practice money' };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    const rendered = screen.getByText('PAPER · practice money');
    expect(rendered.classList.contains('lane-mode-chip--paper')).toBe(true);
  });

  it('names the Shadow simulation world with its own tone', async () => {
    const chip: LaneModeChip = { tone: 'shadow', mode: 'SHADOW · simulated fills on your live account' };
    await render(AlpacaLaneModeChipComponent, { inputs: { chip } });

    expect(
      screen.getByText('SHADOW · simulated fills on your live account').classList.contains('lane-mode-chip--shadow'),
    ).toBe(true);
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

  it('stacks the world over its detail, and keeps a mode with no detail on one line', async () => {
    const view = await render(AlpacaLaneModeChipComponent, {
      inputs: { chip: { tone: 'live', mode: 'LIVE · real money' }, variant: 'stacked' },
    });

    expect(screen.getByText('LIVE').nextElementSibling?.textContent?.trim()).toBe('real money');

    await view.rerender({ inputs: { chip: { tone: 'reading', mode: 'Reading account mode…' }, variant: 'stacked' } });
    expect(screen.getByText('Reading account mode…')).toBeTruthy();
  });

  it('words each world one way, from the server verdict alone', () => {
    expect(verdictModeChip(fakeVerdictState('paper'))).toEqual({ tone: 'paper', mode: 'PAPER · practice money' });
    expect(verdictModeChip(fakeVerdictState('live'))).toEqual({ tone: 'live', mode: 'LIVE · real money' });
    expect(verdictModeChip(fakeVerdictState('shadow'))).toEqual({
      tone: 'shadow',
      mode: 'SHADOW · simulated fills on your live account',
    });
  });

  it('reads a cold load as reading, and keeps the real-money assumption for a failed or unknown read (H4)', async () => {
    const reading = verdictModeChip(UNPOLLED_LANE_STATE);
    expect(reading).toEqual({ tone: 'reading', mode: 'Reading account mode…' });
    await render(AlpacaLaneModeChipComponent, { inputs: { chip: reading } });
    expect(screen.getByText('Reading account mode…').classList.contains('lane-mode-chip--reading')).toBe(true);

    expect(verdictModeChip({ verdict: null, lastError: new Error('read failed') }).mode).toBe('Mode unknown — assume real money');
    expect(verdictModeChip(fakeVerdictState('unknown')).tone).toBe('undetermined');
  });
});
