import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { ExportColumnSelection } from '../export-csv-options';
import { ExportColumnPickerComponent } from './export-column-picker.component';

const COLUMNS = ['open', 'high', 'low', 'close', 'volume', 'rsi_length14'];

async function renderPicker(
  selection: ExportColumnSelection = null,
  timeZone: string | null = 'America/Chicago',
) {
  const selections: ExportColumnSelection[] = [];
  const zones: (string | null)[] = [];
  const { fixture } = await render(ExportColumnPickerComponent, {
    inputs: { columns: COLUMNS, timeColumn: 'time_america_chicago', selection, timeZone },
  });
  // model() is the component's two-way output: it emits every value the
  // picker sets (testing-library's `on` typing does not cover model outputs).
  fixture.componentInstance.selection.subscribe(next => selections.push(next));
  fixture.componentInstance.timeZone.subscribe(next => zones.push(next));
  return { selections, zones };
}

describe('ExportColumnPickerComponent', () => {
  it('always includes unix_ts and never lets it be unticked', async () => {
    await renderPicker();
    const unixTs = screen.getByRole('checkbox', { name: /unix_ts/ }) as HTMLInputElement;
    expect(unixTs.checked).toBe(true);
    expect(unixTs.disabled).toBe(true);
  });

  it('ticks every column until the owner edits the selection', async () => {
    await renderPicker(null);
    for (const column of COLUMNS) {
      expect((screen.getByRole('checkbox', { name: column }) as HTMLInputElement).checked).toBe(true);
    }
    expect(screen.getByText('6 of 6 included')).toBeTruthy();
    expect(screen.getByText(/including ones from indicators you add later/)).toBeTruthy();
  });

  it('unticking one column keeps the rest as an explicit selection', async () => {
    const { selections } = await renderPicker(null);
    await userEvent.click(screen.getByRole('checkbox', { name: 'high' }));
    expect(selections).toEqual([['open', 'low', 'close', 'volume', 'rsi_length14']]);
    expect(screen.getByText('5 of 6 included')).toBeTruthy();
    expect(screen.getByText(/add later start unticked/)).toBeTruthy();
  });

  it('shows an explicit selection and ticks a column back on', async () => {
    const { selections } = await renderPicker(['close']);
    expect((screen.getByRole('checkbox', { name: 'open' }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('checkbox', { name: 'close' }) as HTMLInputElement).checked).toBe(true);
    await userEvent.click(screen.getByRole('checkbox', { name: 'open' }));
    expect(selections).toEqual([['close', 'open']]);
  });

  it('select all returns to "everything"; select none clears every data column', async () => {
    const { selections } = await renderPicker(['close']);
    await userEvent.click(screen.getByRole('button', { name: 'Select all' }));
    await userEvent.click(screen.getByRole('button', { name: 'Select none' }));
    expect(selections).toEqual([null, []]);
  });

  it('names the readable time column and lets the owner pick another zone or none', async () => {
    const { zones } = await renderPicker();
    expect(screen.getByText('time_america_chicago')).toBeTruthy();
    const select = screen.getByRole('combobox', { name: /Readable time column/ });
    expect((select as HTMLSelectElement).value).toBe('America/Chicago');

    await userEvent.selectOptions(select, 'America/New_York');
    await userEvent.selectOptions(select, 'None');
    expect(zones).toEqual(['America/New_York', null]);
  });
});
