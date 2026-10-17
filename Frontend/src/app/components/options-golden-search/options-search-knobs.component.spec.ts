import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { OptionsSearchKnobsComponent } from './options-search-knobs.component';
import { declarations } from './testing';

describe('Options search knob intent', () => {
  it('holds saved rules until varied and supports explicit Off plus independently edited On candidates', async () => {
    const changed = vi.fn();
    const knob = declarations().knobs[1];
    await render(OptionsSearchKnobsComponent, { inputs: { knobs: [knob] }, on: { changed } });
    expect(changed).toHaveBeenLastCalledWith([]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: 'Vary in search' }));
    await user.click(screen.getByRole('button', { name: 'Add another On candidate' }));
    const counts = screen.getAllByRole('spinbutton', { name: 'Re-entry count' });
    await user.clear(counts[1]);
    await user.type(counts[1], '2');
    await user.selectOptions(screen.getAllByRole('combobox', { name: 'Rule kind' })[1], 'on_momentum');
    await waitFor(() => expect(changed).toHaveBeenLastCalledWith([{ path: knob.path, priority: 5, mode: 'search', values: expect.arrayContaining([null, { kind: 'at_once', count: 1 }, { kind: 'on_momentum', count: 2 }]) }]));
    await user.click(screen.getByRole('checkbox', { name: 'Vary in search' }));
    expect(changed).toHaveBeenLastCalledWith([]);
  });

  it('converts shared timing offsets from whole-minute controls to millisecond candidates', async () => {
    const changed = vi.fn();
    await render(OptionsSearchKnobsComponent, { inputs: { knobs: [{ ...declarations().knobs[0], path: 'entry.offset_ms', leg_id: null, label: 'Entry offset', value_type: 'integer', current_value: 300000 }] }, on: { changed } });
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: 'Vary in search' }));
    const values = screen.getByRole('textbox', { name: 'Candidate values, comma separated' });
    await user.clear(values);
    await user.type(values, '5,10');
    await waitFor(() => expect(changed).toHaveBeenLastCalledWith([{ path: 'entry.offset_ms', priority: 5, mode: 'search', values: [300000, 600000] }]));
  });
});
