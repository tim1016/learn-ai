import { Component, signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { UNBOUNDED, type DateBounds } from './date-bounds';
import { DateFieldComponent } from './date-field.component';
import { fakeSessions, fakeTradingCalendar } from './testing/fake-trading-calendar';
import { TradingCalendarService } from './trading-calendar.service';

@Component({
  imports: [DateFieldComponent],
  template: `
    <label for="dev-start">Development from</label>
    <app-date-field [(value)]="value" [bounds]="bounds()" [sessionsOnly]="sessionsOnly()" [hostProblem]="hostProblem()" inputId="dev-start" />
  `,
})
class HostComponent {
  readonly value = signal('2022-11-01');
  readonly bounds = signal<DateBounds>(UNBOUNDED);
  readonly sessionsOnly = signal(false);
  readonly hostProblem = signal<string | null>(null);
}

/** A calendar that fails the test if anything reads it. */
const UNREAD_CALENDAR = {
  sessions: (): never => {
    throw new Error('a field that takes any day must not read the trading calendar');
  },
  problem: (): null => null,
  lastCompleted: (): null => null,
  retry: (): void => undefined,
};

async function renderField(calendar: unknown = UNREAD_CALENDAR) {
  const view = await render(HostComponent, { providers: [{ provide: TradingCalendarService, useValue: calendar }] });
  const host = view.fixture.componentInstance;
  const field = screen.getByLabelText('Development from') as HTMLInputElement;
  const retype = async (text: string): Promise<void> => {
    await userEvent.clear(field);
    if (text !== '') await userEvent.type(field, text);
    await userEvent.tab();
    await view.fixture.whenStable();
  };
  return { view, host, field, retype };
}

describe('DateFieldComponent', () => {
  it('carries the id its host gives it, so the host\'s label names it', async () => {
    const { field } = await renderField();

    expect(field.value).toBe('2022-11-01');
    expect(field.id).toBe('dev-start');
  });

  it('applies a typed date when the field is left; a weekend is a date like any other', async () => {
    const { host, field, retype } = await renderField();

    // 2022-10-01 is a Saturday: a month start the fold planner accepts.
    await retype('2022-10-01');

    expect(host.value()).toBe('2022-10-01');
    expect(field.getAttribute('aria-invalid')).toBe('false');
  });

  it('keeps an unusable entry in the field with its reason and hands the host an empty date', async () => {
    const { host, field, retype } = await renderField();

    await retype('11/01/2022');

    expect(field.value).toBe('11/01/2022');
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('Enter a date as YYYY-MM-DD.');
    expect(host.value()).toBe('');
  });

  it('refuses a day outside its bounds', async () => {
    const { view, host, retype } = await renderField();
    host.bounds.set({ ...UNBOUNDED, min: '2026-10-03', minReason: 'A bot cannot end before today.' });
    await view.fixture.whenStable();

    await retype('2026-10-02');

    expect(screen.getByRole('alert').textContent).toBe('A bot cannot end before today.');
    expect(host.value()).toBe('');
  });

  it('answers a host that puts back a date the field has refused with the empty date again', async () => {
    const { view, host, field, retype } = await renderField();
    await retype('2022-13-01');
    expect(host.value()).toBe('');

    host.value.set('2022-13-01');
    await view.fixture.whenStable();

    expect(host.value()).toBe('');
    expect(field.value).toBe('2022-13-01');
  });

  it("shows a problem its host found as the field's own", async () => {
    const { view, host, field } = await renderField();

    host.hostProblem.set('45 months do not divide into whole folds of 2 months.');
    await view.fixture.whenStable();

    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('45 months do not divide into whole folds of 2 months.');
    expect(host.value()).toBe('2022-11-01');
  });

  it('takes only trading sessions when its host asks for them', async () => {
    const calendar = fakeTradingCalendar(fakeSessions('2022-01-01', '2022-12-31'), '2022-12-30');
    const { view, host, retype } = await renderField(calendar);
    host.sessionsOnly.set(true);
    await view.fixture.whenStable();

    await retype('2022-10-01');

    expect(screen.getByRole('alert').textContent).toBe('2022-10-01 is a weekend. The next session is 2022-10-03.');
    expect(host.value()).toBe('');
  });

  it('picks a day from the calendar', async () => {
    const { view, host, field } = await renderField();

    await userEvent.click(screen.getByRole('button', { name: 'Open the calendar' }));
    const calendar = await screen.findByRole('dialog', { name: 'Calendar' });
    const day = calendar.querySelector<HTMLElement>('span[data-date="2022-10-15"]');
    if (day === null) throw new Error('2022-11-15 is not on the calendar');
    await userEvent.click(day);
    await view.fixture.whenStable();

    expect(host.value()).toBe('2022-11-15');
    expect(field.value).toBe('2022-11-15');
  });

  it('has no accessibility violations', async () => {
    const { view } = await renderField();

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
