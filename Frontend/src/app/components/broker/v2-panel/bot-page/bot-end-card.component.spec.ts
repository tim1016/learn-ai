import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { localWallClock } from '../../../../shared/date/local-wall-clock';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { BotEndInput, BotEndView } from '../lib/broker-v2-panel.service';
import { BotEndCardComponent } from './bot-end-card.component';

const END_AT = Date.UTC(2026, 8, 29, 19, 59);

/** A running bot's end as the panel poll carries it (`bot_end.bot_end_view`, #2607). */
const SCHEDULED: BotEndView = {
  end_at_ms: END_AT,
  end_action: 'SELL',
  status: 'scheduled',
  headline: 'Ends today 15:59 ET · sells',
  explanation: 'At 15:59 ET today the Clerk stops the bot, cancels its working orders and sells its shares at market.',
  notice: null,
  editable: true,
};

/** After Stop: the backend's own words — nothing is left to sell. */
const STOPPED: BotEndView = {
  end_at_ms: null,
  end_action: 'SELL',
  status: 'no_end',
  headline: 'No end scheduled',
  explanation: 'This bot is not running, and no end is scheduled for it.',
  notice: null,
  editable: false,
};

async function renderCard(end: BotEndView, save = vi.fn<(choice: BotEndInput) => Promise<void>>().mockResolvedValue(), dryRun = false) {
  const rendered = await render(BotEndCardComponent, { inputs: { end, save, dryRun } });
  return { ...rendered, save };
}

function editor(): HTMLElement {
  return screen.getByRole('dialog', { name: 'Change this bot’s end' });
}

describe('BotEndCardComponent (#2607)', () => {
  it('shows the end in the backend’s words, in the viewer’s time and market time', async () => {
    await renderCard({ ...SCHEDULED, notice: 'Fri Nov 27 closes early at 13:00 ET, so this bot ends at 12:59 ET.' });

    const card = screen.getByRole('region', { name: 'End' });
    expect(within(card).getByText(SCHEDULED.headline)).toBeTruthy();
    expect(within(card).getByText(SCHEDULED.explanation)).toBeTruthy();
    expect(within(card).getByText('Fri Nov 27 closes early at 13:00 ET, so this bot ends at 12:59 ET.')).toBeTruthy();
    const times = within(card).getByText(/your time/).textContent ?? '';
    expect(times).toContain(formatTimestampDisplay(END_AT, { mode: 'local', granularity: 'time' }));
    expect(times).toContain(formatTimestampDisplay(END_AT, { mode: 'et', granularity: 'time' }));
    expect(within(card).getByRole('button', { name: 'Change end' })).toBeTruthy();
  });

  it('offers no change once the backend says the end is not editable, and suggests no sale', async () => {
    await renderCard(STOPPED);

    const card = screen.getByRole('region', { name: 'End' });
    expect(card.textContent).toContain('No end scheduled');
    expect(card.textContent).not.toMatch(/sell/i);
    expect(within(card).queryByRole('button', { name: 'Change end' })).toBeNull();
  });

  it('reads the fields as Change opens them, and saves the owner’s choice with its action', async () => {
    const { save, fixture } = await renderCard(SCHEDULED);

    fireEvent.click(screen.getByRole('button', { name: 'Change end' }));
    await fixture.whenStable();
    const fields = editor();
    const wall = localWallClock(END_AT);
    expect(within(fields).getByLabelText<HTMLInputElement>('End date').value).toBe(wall.date);
    expect(within(fields).getByLabelText<HTMLInputElement>(/^End time/).value).toBe(wall.time);
    fireEvent.input(within(fields).getByLabelText(/^End time/), { target: { value: '12:30' } });
    fireEvent.click(within(fields).getByRole('radio', { name: 'Keep its shares' }));
    fireEvent.click(within(fields).getByRole('button', { name: 'Save end' }));

    const [year, month, day] = wall.date.split('-').map(Number);
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({
      end_at_ms: new Date(year, month - 1, day, 12, 30).getTime(),
      end_action: 'KEEP',
    }));
  });

  it('never rewrites the fields the owner is typing when a panel poll brings the end again', async () => {
    const { save, fixture, rerender } = await renderCard(SCHEDULED);
    fireEvent.click(screen.getByRole('button', { name: 'Change end' }));
    await fixture.whenStable();
    fireEvent.click(within(editor()).getByRole('checkbox', { name: 'No end — run until I stop it' }));

    await rerender({ partialUpdate: true, inputs: { end: { ...SCHEDULED } } });
    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({ end_at_ms: null, end_action: 'SELL' }));
  });

  it('offers no Keep for a Dry Run, which always sells at its end', async () => {
    const { save, fixture } = await renderCard({ ...SCHEDULED, end_action: 'SELL' }, undefined, true);
    fireEvent.click(screen.getByRole('button', { name: 'Change end' }));
    await fixture.whenStable();

    expect(within(editor()).queryByRole('radio', { name: 'Keep its shares' })).toBeNull();
    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith({ end_at_ms: END_AT, end_action: 'SELL' }));
  });

  it('shows a state conflict (409) in the backend’s words, its code as a label', async () => {
    const conflict = new HttpErrorResponse({
      status: 409,
      error: { detail: {
        message: 'This bot’s end can no longer be changed.',
        why: 'Its end has come and the Clerk is carrying it out.',
        next_action: 'Wait for the Clerk to finish, then read the bot’s page.',
        reason_code: 'BOT_END_REFUSED',
      } },
    });
    const { fixture } = await renderCard(SCHEDULED, vi.fn<(choice: BotEndInput) => Promise<void>>().mockRejectedValue(conflict));
    fireEvent.click(screen.getByRole('button', { name: 'Change end' }));
    await fixture.whenStable();

    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    const alert = await within(editor()).findByRole('alert');
    expect(alert.textContent).toContain('This bot’s end can no longer be changed.');
    expect(alert.textContent).toContain('Its end has come and the Clerk is carrying it out.');
    expect(alert.textContent).toContain('Next: Wait for the Clerk to finish, then read the bot’s page.');
    expect(alert.textContent).toContain('Bot End Refused');
  });

  it('shows a refused end (400) in the backend’s words', async () => {
    const refused = new HttpErrorResponse({
      status: 400,
      error: { detail: {
        message: 'The market is closed on Sat Oct 03.',
        why: 'A bot can only end while the market is open.',
        next_action: 'Choose a trading day.',
        reason_code: 'BOT_END_REFUSED',
      } },
    });
    const { fixture } = await renderCard(SCHEDULED, vi.fn<(choice: BotEndInput) => Promise<void>>().mockRejectedValue(refused));
    fireEvent.click(screen.getByRole('button', { name: 'Change end' }));
    await fixture.whenStable();

    fireEvent.click(within(editor()).getByRole('button', { name: 'Save end' }));

    expect((await within(editor()).findByRole('alert')).textContent).toContain('The market is closed on Sat Oct 03.');
  });

  it('passes AXE', async () => {
    await renderCard(SCHEDULED);

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
