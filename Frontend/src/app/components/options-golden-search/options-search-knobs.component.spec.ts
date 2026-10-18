import { fireEvent, render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { OptionsSearchKnobsComponent } from './options-search-knobs.component';
import { declarations } from './testing';

const set = (region: HTMLElement, label: string, value: string) => fireEvent.input(within(region).getByRole('textbox', { name: label }), { target: { value } });

describe('rule-based search controls', () => {
  it('defaults to keeping every leg saved, then emits one synchronized dimension with exact decimal values and Off', async () => {
    const changed = vi.fn();
    await render(OptionsSearchKnobsComponent, { inputs: { setup: declarations().setup }, on: { changed } });
    expect(changed).toHaveBeenLastCalledWith([]);
    const user = userEvent.setup();
    const stop = screen.getByRole('region', { name: 'Stop loss' });
    await user.click(within(stop).getByRole('button', { name: 'Explore' }));
    set(stop, 'Minimum', '0.1'); set(stop, 'Maximum', '0.3'); set(stop, 'Step', '0.1');
    await user.click(within(stop).getByRole('checkbox', { name: 'Also test without a stop loss' }));
    expect(changed).toHaveBeenLastCalledWith([{ path: 'all_legs.stop_loss', priority: 5, mode: 'search', values: [
      { call: { kind: 'percent', value: '0.1000' }, put: { kind: 'percent', value: '0.1000' } },
      { call: { kind: 'percent', value: '0.2000' }, put: { kind: 'percent', value: '0.2000' } },
      { call: { kind: 'percent', value: '0.3000' }, put: { kind: 'percent', value: '0.3000' } },
      { call: null, put: null },
    ] }]);
    set(stop, 'Step', '0');
    expect(changed).toHaveBeenLastCalledWith(null);
    await user.click(within(stop).getByRole('button', { name: 'Keep saved' }));
    expect(changed).toHaveBeenLastCalledWith([]);
  });

  it('copies shared exploration into each leg and preserves independent edits across tab and scope changes', async () => {
    const changed = vi.fn();
    await render(OptionsSearchKnobsComponent, { inputs: { setup: declarations().setup }, on: { changed } });
    const user = userEvent.setup();
    const stop = () => screen.getByRole('region', { name: 'Stop loss' });
    await user.click(within(stop()).getByRole('button', { name: 'Explore' }));
    set(stop(), 'Minimum', '20'); set(stop(), 'Maximum', '25'); set(stop(), 'Step', '5');
    await user.click(screen.getByRole('button', { name: 'Customize by leg' }));
    set(stop(), 'Maximum', '30');
    await user.click(screen.getByRole('button', { name: /Buy 1 ATM put/ }));
    expect(within(stop()).getByRole('textbox', { name: 'Maximum' })).toHaveProperty('value', '25');
    expect(changed).toHaveBeenLastCalledWith(expect.arrayContaining([
      expect.objectContaining({ path: 'legs.call.stop_loss', values: [{ kind: 'percent', value: '20.0000' }, { kind: 'percent', value: '25.0000' }, { kind: 'percent', value: '30.0000' }] }),
      expect.objectContaining({ path: 'legs.put.stop_loss', values: [{ kind: 'percent', value: '20.0000' }, { kind: 'percent', value: '25.0000' }] }),
    ]));
    await user.click(screen.getByRole('button', { name: 'All legs together' }));
    await user.click(screen.getByRole('button', { name: 'Customize by leg' }));
    await user.click(screen.getByRole('button', { name: /Buy 1 ATM call/ }));
    expect(within(stop()).getByRole('textbox', { name: 'Maximum' })).toHaveProperty('value', '30');
  });
});
