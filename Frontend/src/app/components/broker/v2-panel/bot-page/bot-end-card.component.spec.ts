import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { localWallClock } from '../../../../shared/date/local-wall-clock';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { panelRefusalBody } from '../../../../testing/bot-panel-fixtures';
import type { BotEndInput, BotEndView } from '../lib/broker-v2-panel.service';
import { BotEndCardComponent } from './bot-end-card.component';

const END_AT = Date.UTC(2026, 8, 30, 19, 59);

/** A running bot's end as the panel poll carries it (`bot_end.bot_end_view`, #2607). */
const SCHEDULED: BotEndView = {
  end_at_ms: END_AT,
  end_action: 'SELL',
  status: 'scheduled',
  headline: 'Ends Wed Sep 30, 15:59 ET · sells',
  explanation: 'At Wed Sep 30, 15:59 ET the Clerk stops the bot, cancels its working orders and sells its shares at market.',
  notice: null,
  editable: true,
  default_end_at_ms: null,
};

/** A running bot with no end: the backend offers the default end — today's
 * close minus a minute — for the fields to open on when one is added (#2663). */
const NO_END: BotEndView = {
  end_at_ms: null,
  end_action: 'SELL',
  status: 'no_end',
  headline: 'No end · runs until you stop it',
  explanation: 'This bot has no end time. It runs until you stop it.',
  notice: null,
  editable: true,
  default_end_at_ms: END_AT,
};

/** After Stop: the backend's own words — a Stop cancels the end, and nothing is sold at it. */
const STOPPED: BotEndView = {
  end_at_ms: null,
  end_action: 'SELL',
  status: 'no_end',
  headline: 'No end scheduled',
  explanation: 'This bot is stopped, so it has no end: a Stop cancels any end, and nothing is sold at it.',
  notice: null,
  editable: false,
  default_end_at_ms: null,
};

const etTime = (ms: number): string => formatTimestampDisplay(ms, { mode: 'et', granularity: 'time' });

type Save = (choice: BotEndInput) => Promise<void>;

async function renderCard(
  end: BotEndView,
  save = vi.fn<Save>().mockResolvedValue(),
  { dryRun = false, locked = false }: { dryRun?: boolean; locked?: boolean } = {},
) {
  const rendered = await render(BotEndCardComponent, { inputs: { end, save, dryRun, locked } });
  return { ...rendered, save };
}

function editor(): HTMLElement {
  return screen.getByRole('dialog', { name: 'Change this bot’s end' });
}

async function openEditor(fixture: { whenStable(): Promise<unknown> }): Promise<HTMLElement> {
  fireEvent.click(screen.getByRole('button', { name: 'Change end' }));
  await fixture.whenStable();
  return editor();
}

/** The browser's `toggle` as the fields open or close over the page (jsdom has no popovers). */
function toggleEditor(newState: 'open' | 'closed'): void {
  editor().dispatchEvent(Object.assign(new Event('toggle'), { newState }));
}

function card(): HTMLElement {
  return screen.getByRole('region', { name: 'End' });
}

/** The panel's refusal of an end whose minute has gone by (400). */
const PASSED = new HttpErrorResponse({
  status: 400,
  error: panelRefusalBody({
    message: 'That end time has already passed.',
    why: 'A bot\'s end must be later than now.',
    next_action: 'Choose a time later than now.',
    reason_code: 'BOT_END_REFUSED',
  }),
});

describe('BotEndCardComponent (#2607)', () => {
  it('shows the end in the backend’s words, in the viewer’s time and market time', async () => {
    await renderCard({ ...SCHEDULED, notice: 'Wed Sep 30 closes early at 13:00 ET, so this bot ends at 12:59 ET.' });

    const card = screen.getByRole('region', { name: 'End' });
    expect(within(card).getByText(SCHEDULED.headline)).toBeTruthy();
    expect(within(card).getByText(SCHEDULED.explanation)).toBeTruthy();
    expect(within(card).getByText('Wed Sep 30 closes early at 13:00 ET, so this bot ends at 12:59 ET.')).toBeTruthy();
    const times = within(card).getByText(/your time/).textContent ?? '';
    expect(times).toContain(formatTimestampDisplay(END_AT, { mode: 'local', granularity: 'time' }));
    expect(times).toContain(etTime(END_AT));
    expect(within(card).getByRole('button', { name: 'Change end' })).toBeTruthy();
  });

  it('offers no change once the backend says the end is not editable, and suggests no sale', async () => {
    await renderCard(STOPPED);

    const card = screen.getByRole('region', { name: 'End' });
    expect(card.textContent).toContain('No end scheduled');
    expect(card.textContent).toContain(STOPPED.explanation);
    expect(within(card).queryByRole('button', { name: 'Change end' })).toBeNull();
  });

  it('reads the fields as Change opens them, and saves the owner’s choice with its action', async () => {
    const { save, fixture } = await renderCard(SCHEDULED);

    const fields = await openEditor(fixture);
    const wall = localWallClock(END_AT);
    expect(within(fields).getByLabelText<HTMLInputElement>('End date').value).toBe(wall.date);
    expect(within(fields).getByLabelText<HTMLInputElement>(/^End time/).value).toBe(wall.clock);
    fireEvent.input(within(fields).getByLabelText(/^End time/), { target: { value: '12:30' } });
    fireEvent.click(within(fields).getByRole('radio', { name: 'Keep its shares' }));
    fireEvent.click(within(fields).getByRole('button', { name: 'Save end' }));

    const [year, month, day] = wall.date.split('-').map(Number);
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({
      end_at_ms: new Date(year, month - 1, day, 12, 30).getTime(),
      end_action: 'KEEP',
    }));
  });

  it.each([
    { session: 'a full session', defaultAt: END_AT, etMinute: '15:59' },
    { session: 'a half-day', defaultAt: Date.UTC(2026, 10, 27, 17, 59), etMinute: '12:59' },
  ])('opens a bot with no end on the default end the backend offers, on $session', async ({ defaultAt, etMinute }) => {
    const { save, fixture } = await renderCard({ ...NO_END, default_end_at_ms: defaultAt });

    const fields = await openEditor(fixture);
    const wall = localWallClock(defaultAt);
    expect(within(fields).getByLabelText<HTMLInputElement>('End date').value).toBe(wall.date);
    expect(within(fields).getByLabelText<HTMLInputElement>(/^End time/).value).toBe(wall.clock);
    expect(within(fields).getByRole<HTMLInputElement>('checkbox', { name: 'No end — run until I stop it' }).checked).toBe(false);
    const market = within(fields).getByText(/^Market time/).textContent ?? '';
    expect(market).toContain(etTime(defaultAt));
    expect(market).toContain(etMinute);
    fireEvent.click(within(fields).getByRole('button', { name: 'Save end' }));

    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({ end_at_ms: defaultAt, end_action: 'SELL' }));
  });

  it('opens a bot the backend offers no default end on "No end"', async () => {
    const { fixture } = await renderCard({ ...NO_END, default_end_at_ms: null });

    const fields = await openEditor(fixture);

    expect(within(fields).getByRole<HTMLInputElement>('checkbox', { name: 'No end — run until I stop it' }).checked).toBe(true);
    expect(within(fields).getByLabelText<HTMLInputElement>('End date').value).toBe('');
  });

  it('says the typed time in market time beside it, and nothing while the date or time names no minute', async () => {
    const { fixture } = await renderCard(SCHEDULED);
    const fields = await openEditor(fixture);
    const time = within(fields).getByLabelText<HTMLInputElement>(/^End time/);
    const market = within(fields).getByText(/^Market time/);
    expect(time.getAttribute('aria-describedby')?.split(' ')).toContain(market.id);
    expect(market.textContent).toContain(etTime(END_AT));

    fireEvent.input(time, { target: { value: '12:30' } });

    const [year, month, day] = localWallClock(END_AT).date.split('-').map(Number);
    expect(market.textContent).toContain(etTime(new Date(year, month - 1, day, 12, 30).getTime()));

    fireEvent.input(within(fields).getByLabelText('End date'), { target: { value: '' } });

    expect(market.textContent).not.toContain('ET');
  });

  it('never rewrites the fields the owner is typing when a panel poll brings the end again', async () => {
    const { save, fixture, rerender } = await renderCard(SCHEDULED);
    await openEditor(fixture);
    fireEvent.click(within(editor()).getByRole('checkbox', { name: 'No end — run until I stop it' }));

    await rerender({ partialUpdate: true, inputs: { end: { ...SCHEDULED } } });
    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({ end_at_ms: null, end_action: 'SELL' }));
  });

  it('never rewrites the default end the owner is changing when a poll offers a later one', async () => {
    const { save, fixture, rerender } = await renderCard(NO_END);
    const fields = await openEditor(fixture);
    fireEvent.input(within(fields).getByLabelText(/^End time/), { target: { value: '12:30' } });

    await rerender({ partialUpdate: true, inputs: { end: { ...NO_END, default_end_at_ms: END_AT + 86_400_000 } } });
    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    const [year, month, day] = localWallClock(END_AT).date.split('-').map(Number);
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({
      end_at_ms: new Date(year, month - 1, day, 12, 30).getTime(),
      end_action: 'SELL',
    }));
  });

  it('offers no Keep for a Dry Run, which always sells at its end', async () => {
    const { save, fixture } = await renderCard({ ...SCHEDULED, end_action: 'SELL' }, undefined, { dryRun: true });
    await openEditor(fixture);

    expect(within(editor()).queryByRole('radio', { name: 'Keep its shares' })).toBeNull();
    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({ end_at_ms: END_AT, end_action: 'SELL' }));
  });

  it('sends nothing it cannot read, and says what the date needs', async () => {
    const { save, fixture } = await renderCard(SCHEDULED);
    const fields = await openEditor(fixture);
    fireEvent.input(within(fields).getByLabelText('End date'), { target: { value: '' } });

    fireEvent.click(within(fields).getByRole('button', { name: 'Save end' }));

    expect((await within(fields).findByRole('alert')).textContent?.trim()).toBe('Enter the end’s date.');
    expect(save).not.toHaveBeenCalled();
  });

  it('holds Change still while another command on this bot is on its way', async () => {
    const { fixture, rerender } = await renderCard(SCHEDULED, undefined, { locked: true });

    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Change end' }).disabled).toBe(true);

    await rerender({ partialUpdate: true, inputs: { locked: false } });
    await fixture.whenStable();
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Change end' }).disabled).toBe(false);
  });

  it('shows a state conflict (409) in the backend’s words, its code as a label', async () => {
    const conflict = new HttpErrorResponse({
      status: 409,
      error: panelRefusalBody({
        message: 'This bot\'s end has come; the Clerk is carrying it out.',
        why: 'An end can be changed only before its time.',
        next_action: 'The bot page shows how it ended once the Clerk is done.',
        reason_code: 'BOT_END_REFUSED',
      }),
    });
    const { fixture } = await renderCard(SCHEDULED, vi.fn<Save>().mockRejectedValue(conflict));
    await openEditor(fixture);

    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    const alert = await within(editor()).findByRole('alert');
    expect(alert.textContent).toContain('This bot\'s end has come; the Clerk is carrying it out.');
    expect(alert.textContent).toContain('An end can be changed only before its time.');
    expect(alert.textContent).toContain('Next: The bot page shows how it ended once the Clerk is done.');
    expect(alert.textContent).toContain('Bot End Refused');
  });

  it('shows a refused end (400) in the backend’s words', async () => {
    const refused = new HttpErrorResponse({
      status: 400,
      error: panelRefusalBody({
        message: 'The market is closed on Sat Oct 3.',
        why: 'A bot can only end while the market is open.',
        next_action: 'Choose a trading day.',
        reason_code: 'BOT_END_REFUSED',
      }),
    });
    const { fixture } = await renderCard(SCHEDULED, vi.fn<Save>().mockRejectedValue(refused));
    await openEditor(fixture);

    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    expect((await within(editor()).findByRole('alert')).textContent).toContain('The market is closed on Sat Oct 3.');
  });

  it('says the fleet’s next step once when it refuses the change', async () => {
    const stale = new HttpErrorResponse({
      status: 409,
      error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.', next_step: 'Reload the lane.' },
    });
    const { fixture } = await renderCard(SCHEDULED, vi.fn<Save>().mockRejectedValue(stale));
    await openEditor(fixture);

    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    const alert = await within(editor()).findByRole('alert');
    expect(alert.textContent).toContain('Expected 3 is not 4.');
    expect(alert.textContent?.match(/Reload the lane\./g)).toHaveLength(1);
    expect(alert.textContent).toContain('Clerk Binding Generation Conflict');
  });

  it('says a refusal once, in the fields, while they are open', async () => {
    const { fixture } = await renderCard(SCHEDULED, vi.fn<Save>().mockRejectedValue(PASSED));
    await openEditor(fixture);
    toggleEditor('open');

    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    expect((await within(editor()).findByRole('alert')).textContent).toContain('That end time has already passed.');
    expect(within(card()).queryByRole('alert')).toBeNull();
  });

  it('shows on the card a refusal that lands after the owner closed the fields mid-save, until Change opens them again', async () => {
    let refuse: (error: unknown) => void = () => undefined;
    const save = vi.fn<Save>().mockImplementation(() => new Promise((_resolve, reject) => { refuse = reject; }));
    const { fixture } = await renderCard(SCHEDULED, save);
    await openEditor(fixture);
    toggleEditor('open');
    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));
    await vi.waitFor(() => expect(save).toHaveBeenCalledOnce());

    toggleEditor('closed');
    refuse(PASSED);

    const alert = await within(card()).findByRole('alert');
    expect(alert.textContent).toContain('That end time has already passed.');
    expect(alert.textContent).toContain('Next: Choose a time later than now.');

    await openEditor(fixture);
    toggleEditor('open');
    await fixture.whenStable();
    expect(within(card()).queryByRole('alert')).toBeNull();
    expect(within(editor()).queryByRole('alert')).toBeNull();
  });

  it('passes AXE, closed and with its fields open', async () => {
    const { fixture } = await renderCard(SCHEDULED, vi.fn<Save>().mockRejectedValue(new HttpErrorResponse({
      status: 400,
      error: panelRefusalBody({ message: 'That end time has already passed.', why: 'A bot\'s end must be later than now.', next_action: 'Choose a time later than now.', reason_code: 'BOT_END_REFUSED' }),
    })));
    const closed = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(closed.violations.map((violation) => violation.id)).toEqual([]);

    const fields = await openEditor(fixture);
    fireEvent.click(within(fields).getByRole('button', { name: 'Save end' }));
    await within(fields).findByRole('alert');

    const open = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(open.violations.map((violation) => violation.id)).toEqual([]);
  });
});
