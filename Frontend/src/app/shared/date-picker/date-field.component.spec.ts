import { Component, signal } from '@angular/core';
import { By } from '@angular/platform-browser';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { DateFieldComponent } from './date-field.component';

@Component({
  imports: [DateFieldComponent],
  template: `
    <label for="dev-start">Development from</label>
    <app-date-field
      [(value)]="value"
      [hostProblem]="hostProblem()"
      [pickFrom]="pickFrom()"
      [calendar]="calendar()"
      [disabled]="disabled()"
      inputId="dev-start"
      (touch)="touches.set(touches() + 1)"
    />
  `,
})
class HostComponent {
  readonly value = signal('2022-11-01');
  readonly hostProblem = signal<string | null>(null);
  readonly pickFrom = signal<string | null>(null);
  readonly calendar = signal<'popup' | 'inline'>('popup');
  readonly disabled = signal(false);
  readonly touches = signal(0);
}

async function renderField() {
  const view = await render(HostComponent);
  const host = view.fixture.componentInstance;
  const field = screen.getByLabelText('Development from') as HTMLInputElement;
  const retype = async (text: string): Promise<void> => {
    await userEvent.clear(field);
    if (text !== '') await userEvent.type(field, text);
    await userEvent.tab();
    await view.fixture.whenStable();
  };
  /** PrimeNG's day cell for a date; its months are zero-based. */
  const day = (within: Element, date: string): HTMLElement => {
    const [year, month, dayOfMonth] = date.split('-').map(Number);
    const cell = within.querySelector<HTMLElement>(`span[data-date="${year}-${month - 1}-${dayOfMonth}"]`);
    if (cell === null) throw new Error(`${date} is not on the calendar`);
    return cell;
  };
  return { view, host, field, retype, day };
}

describe('DateFieldComponent', () => {
  it("carries the id its host gives it, so the host's label names it", async () => {
    const { field } = await renderField();

    expect(field.value).toBe('2022-11-01');
    expect(field.id).toBe('dev-start');
  });

  it('hands the host a typed date at once, so a click in the same task already acts on it; a weekend is a date like any other', async () => {
    const { host, field } = await renderField();

    // 2022-10-01 is a Saturday: a month start the fold planner accepts. No tick follows the change.
    field.value = '2022-10-01';
    field.dispatchEvent(new Event('change'));

    expect(host.value()).toBe('2022-10-01');
  });

  it('takes a character typed into the date it holds, so a date is edited in place', async () => {
    const { view, host, field } = await renderField();

    // The caret before the year's last digit: the new digit is typed, then the old one deleted.
    await userEvent.type(field, '3{Delete}', { initialSelectionStart: 3, initialSelectionEnd: 3 });
    await userEvent.tab();
    await view.fixture.whenStable();

    expect(field.value).toBe('2023-11-01');
    expect(host.value()).toBe('2023-11-01');
  });

  it('keeps an unusable entry in the field with its reason and hands the host an empty date', async () => {
    const { host, field, retype } = await renderField();

    await retype('11/01/2022');

    expect(field.value).toBe('11/01/2022');
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('Enter a date as YYYY-MM-DD.');
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

  it('picks a day from a calendar over the page, which offers no day before the first its host names; an earlier date can still be typed', async () => {
    const { view, host, field, retype, day } = await renderField();
    host.pickFrom.set('2022-11-10');
    await view.fixture.whenStable();

    await userEvent.click(screen.getByRole('button', { name: 'Open the calendar' }));
    const calendar = await screen.findByRole('dialog', { name: 'Calendar' });
    expect([day(calendar, '2022-11-09'), day(calendar, '2022-11-10')].map((cell) => cell.classList.contains('p-disabled'))).toEqual([true, false]);

    await userEvent.click(day(calendar, '2022-11-15'));
    await view.fixture.whenStable();
    expect(host.value()).toBe('2022-11-15');
    expect(field.value).toBe('2022-11-15');

    // The host's server judges a date that has passed, in its own words.
    await retype('2022-11-02');
    expect(host.value()).toBe('2022-11-02');
  });

  it('opens its calendar in place for a host that is itself a popover, and closes it on a pick', async () => {
    const { view, host, day } = await renderField();
    host.calendar.set('inline');
    await view.fixture.whenStable();
    const open = screen.getByRole('button', { name: 'Open the calendar' });

    await userEvent.click(open);
    await view.fixture.whenStable();

    // In the field's own element, not floated to the page body where a top-layer host would cover it.
    const calendar = screen.getByRole('group', { name: 'Calendar' });
    expect(view.container.querySelector('app-date-field')?.contains(calendar)).toBe(true);
    expect(open.getAttribute('aria-expanded')).toBe('true');

    await userEvent.click(day(calendar, '2022-11-15'));
    await view.fixture.whenStable();

    expect(host.value()).toBe('2022-11-15');
    expect(screen.queryByRole('group', { name: 'Calendar' })).toBeNull();
    // The keyboard goes back to the button that opened it, not to the page.
    expect(document.activeElement).toBe(open);
  });

  it('puts its in-place calendar away on Escape, when it is disabled, and when its host says so', async () => {
    const { view, host, day } = await renderField();
    host.calendar.set('inline');
    await view.fixture.whenStable();
    const open = screen.getByRole('button', { name: 'Open the calendar' });
    const calendar = (): HTMLElement | null => screen.queryByRole('group', { name: 'Calendar' });
    const reopen = async (): Promise<HTMLElement> => {
      await userEvent.click(open);
      await view.fixture.whenStable();
      return screen.getByRole('group', { name: 'Calendar' });
    };

    day(await reopen(), '2022-11-15').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await view.fixture.whenStable();
    expect(calendar()).toBeNull();
    expect(document.activeElement).toBe(open);

    await reopen();
    host.disabled.set(true);
    await view.fixture.whenStable();
    expect(calendar()).toBeNull();
    // Enabled again, it stays closed: nothing could have closed it while its button was disabled.
    host.disabled.set(false);
    await view.fixture.whenStable();
    expect(calendar()).toBeNull();

    await reopen();
    view.fixture.debugElement.query(By.directive(DateFieldComponent)).componentInstance.closeCalendar();
    await view.fixture.whenStable();
    expect(calendar()).toBeNull();
    expect(host.value()).toBe('2022-11-01');
  });

  it('checks nothing and hands nothing back while disabled, and says when it has been left', async () => {
    const { view, host, field } = await renderField();
    host.value.set('not a date');
    host.disabled.set(true);
    await view.fixture.whenStable();

    expect(field.disabled).toBe(true);
    expect(host.value()).toBe('not a date');
    expect(screen.queryByRole('alert')).toBeNull();

    host.disabled.set(false);
    await view.fixture.whenStable();
    field.focus();
    await userEvent.tab();

    expect(host.touches()).toBe(1);
    expect(host.value()).toBe('');
  });

  it('does not write an old date back over what its host set while it was disabled', async () => {
    const { view, host, field, retype } = await renderField();
    await retype('2022-12-01');
    host.disabled.set(true);
    await view.fixture.whenStable();

    // The host moves the date and moves it back while the field is out of use.
    host.value.set('2023-01-03');
    await view.fixture.whenStable();
    host.value.set('2022-12-01');
    await view.fixture.whenStable();
    host.disabled.set(false);
    await view.fixture.whenStable();

    expect(host.value()).toBe('2022-12-01');
    expect(field.value).toBe('2022-12-01');
  });

  it('has no accessibility violations', async () => {
    const { view } = await renderField();

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
