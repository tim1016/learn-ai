import { Component, signal } from '@angular/core';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { heldBounds, type DateBounds } from './date-bounds';
import { DateRangePickerComponent } from './date-range-picker.component';
import { QUICK_RANGES, type QuickRangeKey } from './quick-ranges';
import { fakeSessions, fakeTradingCalendar, type FakeTradingCalendar } from './testing/fake-trading-calendar';
import { TradingCalendarService } from './trading-calendar.service';

const SESSIONS = fakeSessions('2021-01-01', '2026-12-31', { '2026-09-07': 'Labor Day' });
const QQQ = heldBounds('QQQ', '2024-09-03', '2026-09-18');

@Component({
  imports: [DateRangePickerComponent],
  template: `<app-date-range-picker [(from)]="from" [(to)]="to" [bounds]="bounds()" [ranges]="ranges()" />`,
})
class HostComponent {
  readonly from = signal('2026-08-18');
  readonly to = signal('2026-09-18');
  readonly bounds = signal<DateBounds>(QQQ);
  readonly ranges = signal<readonly QuickRangeKey[]>(QUICK_RANGES);
}

async function renderPicker(calendar: FakeTradingCalendar = fakeTradingCalendar(SESSIONS, '2026-10-02')) {
  const view = await render(HostComponent, { providers: [{ provide: TradingCalendarService, useValue: calendar }] });
  const host = view.fixture.componentInstance;
  const start = screen.getByRole('textbox', { name: 'Start date' }) as HTMLInputElement;
  const end = screen.getByRole('textbox', { name: 'End date' }) as HTMLInputElement;
  const quick = (key: QuickRangeKey): HTMLElement => within(screen.getByRole('group', { name: 'Quick ranges' })).getByText(key, { exact: true });
  const retype = async (field: HTMLInputElement, text: string): Promise<void> => {
    await userEvent.clear(field);
    if (text !== '') await userEvent.type(field, text);
    await userEvent.tab();
    await view.fixture.whenStable();
  };
  return { view, host, start, end, quick, retype };
}

describe('DateRangePickerComponent', () => {
  it("shows the host's dates and lights only the quick range they match exactly", async () => {
    const { start, end, quick } = await renderPicker();

    expect([start.value, end.value]).toEqual(['2026-08-18', '2026-09-18']);
    expect(quick('1M').getAttribute('aria-pressed')).toBe('true');
    expect(quick('3M').getAttribute('aria-pressed')).toBe('false');
  });

  it('applies a typed date when the field is left, and counts the sessions in the range', async () => {
    const { host, start, quick, retype } = await renderPicker();

    await retype(start, '2026-09-14');

    expect(host.from()).toBe('2026-09-14');
    expect(quick('1M').getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByText(/^5 sessions/)).not.toBeNull();
  });

  it('keeps a half-typed date in the field, says why, and hands the host an empty date until it is whole', async () => {
    const { host, start, retype } = await renderPicker();

    await retype(start, '2026-09');

    expect(start.value).toBe('2026-09');
    expect(start.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByText('Enter a date as YYYY-MM-DD.')).not.toBeNull();
    expect(host.from()).toBe('');

    await retype(start, '2026-09-14');

    expect(host.from()).toBe('2026-09-14');
    expect(start.getAttribute('aria-invalid')).toBe('false');
  });

  it('refuses a day the data does not reach and a closed day, each with its reason', async () => {
    const { host, start, end, retype } = await renderPicker();

    await retype(start, '2024-08-30');
    expect(screen.getByText('QQQ is held from 2024-09-03.')).not.toBeNull();
    expect(host.from()).toBe('');

    await retype(start, '2026-08-18');
    await retype(end, '2026-09-07');
    expect(screen.getByText('2026-09-07 is a market holiday (Labor Day). The next session is 2026-09-08.')).not.toBeNull();
    expect(host.to()).toBe('');
    expect(end.value).toBe('2026-09-07');
  });

  it('sets both dates with one click on a quick range', async () => {
    const { host, start, end, quick } = await renderPicker();

    await userEvent.click(quick('1Y'));

    expect([host.from(), host.to()]).toEqual(['2025-09-18', '2026-09-18']);
    expect([start.value, end.value]).toEqual(['2025-09-18', '2026-09-18']);
    expect(quick('1Y').getAttribute('aria-pressed')).toBe('true');
  });

  it('greys a range the data cannot serve, says why, and does nothing when it is clicked', async () => {
    const { host, quick } = await renderPicker();

    expect(quick('5Y').getAttribute('aria-disabled')).toBe('true');
    expect(screen.getByRole('button', { name: '5Y, unavailable: QQQ is held from 2024-09-03.' })).toBe(quick('5Y'));

    await userEvent.click(quick('5Y'));

    expect([host.from(), host.to()]).toEqual(['2026-08-18', '2026-09-18']);
  });

  it('offers only the quick ranges the page asks for', async () => {
    const { view, host } = await renderPicker();

    host.ranges.set(['1Y', '2Y', 'Max']);
    await view.fixture.whenStable();

    const row = screen.getByRole('group', { name: 'Quick ranges' });
    expect(within(row).getAllByRole('button').map((button) => button.textContent?.trim())).toEqual(['1Y', '2Y', 'Max']);
  });

  it('says when the held data stops short of the market', async () => {
    await renderPicker();

    // The market's last completed session is 2026-10-01; QQQ is held through 2026-09-18.
    expect(screen.getByText(/QQQ is held through 2026-09-18\./)).not.toBeNull();
  });

  it('follows the host when it changes the dates', async () => {
    const { view, host, start } = await renderPicker();

    host.from.set('2026-09-01');
    await view.fixture.whenStable();

    expect(start.value).toBe('2026-09-01');
  });

  it('re-checks the dates when the bounds change, so a new instrument cannot keep dates it does not hold', async () => {
    const { view, host, start } = await renderPicker();

    host.bounds.set(heldBounds('AAPL', '2026-09-01', '2026-09-18'));
    await view.fixture.whenStable();

    expect(host.from()).toBe('');
    expect(start.value).toBe('2026-08-18');
    expect(screen.getByText('AAPL is held from 2026-09-01.')).not.toBeNull();
  });

  it('says when the trading calendar cannot be read and offers a retry', async () => {
    const calendar = { ...fakeTradingCalendar(null, '2026-10-02', 'The data plane did not respond.'), retry: vi.fn() };
    const { quick } = await renderPicker(calendar);

    expect(screen.getByText(/The trading calendar could not be read: The data plane did not respond\./)).not.toBeNull();
    expect(quick('1M').getAttribute('aria-disabled')).toBe('true');

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(calendar.retry).toHaveBeenCalledTimes(1);
  });

  it('picks both ends in one calendar, where closed days cannot be picked', async () => {
    const { view, host } = await renderPicker();

    await userEvent.click(screen.getByRole('button', { name: 'Open the calendar' }));
    await view.fixture.whenStable();
    const calendar = await screen.findByRole('dialog', { name: 'Calendar' });
    const day = (date: string): HTMLElement => {
      const [year, month, dayOfMonth] = date.split('-').map(Number);
      const cell = calendar.querySelector<HTMLElement>(`span[data-date="${year}-${month - 1}-${dayOfMonth}"]`);
      if (cell === null) throw new Error(`${date} is not on the calendar`);
      return cell;
    };

    // The range's last month sits on the right, so no month is spent past the last held day.
    expect([...calendar.querySelectorAll('.p-datepicker-title')].map((title) => title.textContent?.replace(/\s+/g, ' ').trim())).toEqual(['August 2026', 'September 2026']);
    // Labor Day, a Saturday, and a day past the last held day cannot be picked.
    for (const closed of ['2026-09-07', '2026-09-05', '2026-09-21']) expect(day(closed).classList.contains('p-disabled')).toBe(true);
    expect(day('2026-09-08').classList.contains('p-disabled')).toBe(false);

    await userEvent.click(day('2026-08-20'));
    await userEvent.click(day('2026-09-10'));
    await view.fixture.whenStable();

    expect([host.from(), host.to()]).toEqual(['2026-08-20', '2026-09-10']);
  });

  it('has no accessibility violations', async () => {
    const { view } = await renderPicker();

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
