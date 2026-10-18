import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { OptionsSearchTimingComponent } from './options-search-timing.component';
import { declarations } from './testing';

describe('synced timing ranges', () => {
  it('keeps timing fixed by default and shifts open and close anchors in the same clock direction', async () => {
    const changed = vi.fn();
    await render(OptionsSearchTimingComponent, { inputs: { setup: { ...declarations().setup, entry: { anchor: 'open', offset_ms: 900000 }, exit: { anchor: 'close', offset_ms: 900000 } } }, on: { changed } });
    expect(changed).toHaveBeenLastCalledWith([]);
    const set = (label: string, value: string) => fireEvent.input(screen.getByRole('spinbutton', { name: label }), { target: { value } });
    set('entry minimum minutes', '-5');
    set('entry maximum minutes', '5');
    set('entry step minutes', '5');
    set('exit minimum minutes', '-10');
    set('exit step minutes', '10');
    expect(changed).toHaveBeenLastCalledWith([
      { path: 'entry.offset_ms', values: [600000, 900000, 1200000], priority: 5, mode: 'search' },
      { path: 'exit.offset_ms', values: [1500000, 900000], priority: 5, mode: 'search' },
    ]);
    set('entry step minutes', '0');
    expect(changed).toHaveBeenLastCalledWith(null);
    set('entry step minutes', '3');
    expect(changed).toHaveBeenLastCalledWith(null);
  });
});
