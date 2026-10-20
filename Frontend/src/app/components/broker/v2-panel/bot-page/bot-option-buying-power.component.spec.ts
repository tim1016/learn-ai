import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { provideFleetDirectory } from '../../../../fleet/fleet-directory-testing';
import { panelRefusalBody } from '../../../../testing/bot-panel-fixtures';
import {
  BrokerV2PanelService,
  type OptionBuyingPowerCheck,
  type OptionBuyingPowerChecks,
  type OptionBuyingPowerHoldReview,
} from '../lib/broker-v2-panel.service';
import { BotOptionBuyingPowerComponent } from './bot-option-buying-power.component';

const amount = (cents: number): { cents: number; dollars: string } => ({
  cents,
  dollars: `${cents < 0 ? '-' : ''}${Math.floor(Math.abs(cents) / 100)}.${String(Math.abs(cents) % 100).padStart(2, '0')}`,
});

const CONSISTENT: OptionBuyingPowerCheck = {
  order_ref: 'order-consistent',
  contract: 'SPY260918C00585000',
  result: 'consistent',
  result_label: 'Consistent',
  reason_code: null,
  sentence: 'Alpaca’s option buying power fell $194.00 across this entry, which is what the contracts that filled cost.',
  contracts_asked: 1,
  contracts_filled: 1,
  set_aside: amount(19_406),
  set_aside_for_filled: amount(19_406),
  premium_paid: amount(19_400),
  first_reading: { began_ms: 1_790_000_000_000, ended_ms: 1_790_000_000_400, buying_power: amount(10_000_000) },
  second_reading: { began_ms: 1_790_000_060_000, ended_ms: 1_790_000_060_300, buying_power: amount(9_980_600) },
  sent_at_ms: 1_790_000_001_000,
  fall: amount(19_400),
  compared_at_ms: 1_790_000_075_000,
  holds: false,
  account_requirement_before: amount(0),
  account_requirement_after: amount(50_000),
  account_requirement_change: amount(50_000),
  account_requirement_sentence:
    'The account’s option requirement, over every contract the books hold for its bots, rose by $500.00 across this entry: $0.00 before and $500.00 after.',
};

const UNDER_RESERVED: OptionBuyingPowerCheck = {
  ...CONSISTENT,
  order_ref: 'order-under',
  result: 'under_reserved_unexplained',
  result_label: 'Under-reserved',
  sentence: 'Alpaca’s option buying power fell $250.00 across this entry. The bot had set aside $194.06 for the whole entry.',
  fall: amount(25_000),
  holds: true,
};

const NOT_YET: OptionBuyingPowerCheck = {
  ...CONSISTENT,
  order_ref: 'order-waiting',
  result: 'unknown',
  result_label: 'Not compared',
  reason_code: 'NOT_COMPARED_YET',
  sentence: 'The comparison for this entry has not been made yet.',
  contracts_filled: null,
  set_aside_for_filled: null,
  premium_paid: null,
  first_reading: null,
  second_reading: null,
  sent_at_ms: null,
  fall: null,
  compared_at_ms: null,
  account_requirement_before: null,
  account_requirement_after: null,
  account_requirement_change: null,
  account_requirement_sentence: 'The account’s own option requirement was not recorded across this entry.',
};

const HOLD_NOTICE: NonNullable<OptionBuyingPowerChecks['hold']> = {
  headline: 'Alpaca took more than this bot set aside for an entry',
  explanation: 'Across entry order-under Alpaca’s option buying power fell $250.00. The bot had set aside $194.06 for the whole entry.',
  impact: 'This bot will open nothing new until you have reviewed this. It can still close what it holds.',
  next_step: 'Look at this account’s activity at Alpaca around the entry, then review this notice.',
  review_consequence: 'Once you have reviewed it, this bot may open new option entries again. Nothing is sent to Alpaca and no order changes.',
  raised_at_ms: 1_790_000_080_000,
  entries: [{ order_ref: 'order-under', fall: amount(25_000), set_aside: amount(19_406) }],
};

function page(overrides: Partial<OptionBuyingPowerChecks> = {}): OptionBuyingPowerChecks {
  return {
    account_id: 'PA-1',
    strategy_instance_id: 'bot-1',
    simulation: false,
    judged: true,
    hold: null,
    counts: {
      entries: 2,
      compared: 1,
      not_compared: 1,
      not_compared_by_reason: [{
        reason_code: 'NOT_COMPARED_YET',
        sentence: 'The comparison for this entry has not been made yet.',
        count: 1,
      }],
      sentence: '1 of 2 option entries were compared with Alpaca’s option buying power, 1 not.',
    },
    checks: [NOT_YET, CONSISTENT],
    ...overrides,
  };
}

const HELD = page({
  hold: HOLD_NOTICE,
  counts: {
    entries: 1, compared: 1, not_compared: 0, not_compared_by_reason: [],
    sentence: '1 of 1 option entry was compared with Alpaca’s option buying power, 0 not.',
  },
  checks: [UNDER_RESERVED],
});

type Review = () => Promise<OptionBuyingPowerHoldReview>;

/** The card as the bot page renders it: it reads through the service and hands the review to the page. */
async function renderCard(
  served: OptionBuyingPowerChecks,
  { reviewed, journalTailSeq = 7, locked = false }: {
    reviewed?: Promise<OptionBuyingPowerHoldReview>; journalTailSeq?: number; locked?: boolean;
  } = {},
) {
  const read = vi.fn<(target: unknown, sid: string) => Promise<OptionBuyingPowerChecks>>().mockResolvedValue(served);
  const review = vi.fn<Review>().mockImplementation(
    () => reviewed ?? Promise.resolve({ reviewed: true, state: page({ checks: [UNDER_RESERVED] }) }),
  );
  const rendered = await render(BotOptionBuyingPowerComponent, {
    inputs: {
      broker: 'alpaca', clerkId: 'clrk_spec0000000000000000aa', accountId: 'PA-1', sid: 'bot-1',
      journalTailSeq, review, locked,
    },
    providers: [
      provideFleetDirectory(),
      { provide: BrokerV2PanelService, useValue: { getOptionBuyingPowerChecks: read } },
    ],
  });
  return { ...rendered, read, review };
}

/** What an entry's card says under one label of its figures. */
function figure(entry: HTMLElement, label: string): string {
  const term = within(entry).getByText(label, { selector: 'dt' });
  return (term.nextElementSibling?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('BotOptionBuyingPowerComponent (#3098)', () => {
  it('says how many entries were compared, how many were not and why, above the entries, and no limit when every entry is listed', async () => {
    await renderCard(page());

    const card = await screen.findByRole('region', { name: 'Alpaca option buying power' });
    expect.soft(screen.queryByText(/Showing the newest/), 'says nothing of a limit when every entry is listed').toBeNull();
    expect.soft(
      within(card).getByText('1 of 2 option entries were compared with Alpaca’s option buying power, 1 not.'),
      'says how many entries were compared and how many were not',
    ).toBeTruthy();
    const reasons = within(card).getByRole('list', { name: 'Why entries were not compared' });
    expect.soft(within(reasons).getAllByRole('listitem'), 'lists one line per reason entries were not compared')
      .toHaveLength(1);
    expect.soft(
      within(reasons).getByText('The comparison for this entry has not been made yet.'),
      'says why entries were not compared',
    ).toBeTruthy();
    // The counts come first, then the entries.
    const counts = within(card).getByText('1 of 2 option entries were compared with Alpaca’s option buying power, 1 not.');
    const entries = within(card).getByRole('list', { name: 'Option entries, newest first' });
    expect.soft(counts.compareDocumentPosition(entries) & Node.DOCUMENT_POSITION_FOLLOWING, 'puts the counts above the entries')
      .toBeTruthy();
  });

  it('shows each entry’s result in the backend’s words, both readings, the fall, the commitment and the account’s requirement change', async () => {
    await renderCard(page());

    const entries = within(await screen.findByRole('list', { name: 'Option entries, newest first' }))
      .getAllByRole('listitem');
    expect(entries).toHaveLength(2);
    const compared = entries[1];
    expect(within(compared).getByText('Consistent')).toBeTruthy();
    expect(within(compared).getByText(CONSISTENT.sentence)).toBeTruthy();
    expect(figure(compared, 'Set aside for the entry')).toBe('$194.06');
    expect(figure(compared, 'Option buying power before')).toContain('$100000.00');
    expect(figure(compared, 'Option buying power after')).toContain('$99806.00');
    // The fall is the reading before less the reading after, said as a fall: $100,000.00 to $99,806.00.
    expect(figure(compared, 'Fall in option buying power')).toBe('$194.00');
    expect(figure(compared, 'Account requirement')).toBe('$0.00 to $500.00 change $500.00');
    // Each reading carries the time it was read.
    expect(compared.querySelectorAll('app-timestamp-display').length).toBe(2);
    expect(within(compared).getByText(CONSISTENT.account_requirement_sentence)).toBeTruthy();
  });

  it('says a figure that rose rose, and a requirement the entry released as a negative change', async () => {
    const rose: OptionBuyingPowerCheck = {
      ...CONSISTENT,
      order_ref: 'order-rose',
      result: 'over_reserved',
      result_label: 'Over-reserved',
      fall: amount(-5_000),
      account_requirement_before: amount(100_000),
      account_requirement_after: amount(0),
      account_requirement_change: amount(-100_000),
    };
    await renderCard(page({ checks: [rose] }));

    const entry = within(await screen.findByRole('list', { name: 'Option entries, newest first' }))
      .getAllByRole('listitem')[0];

    // The label says fall, so a negative fall is told as the figure having risen.
    expect(figure(entry, 'Fall in option buying power')).toBe('-$50.00 it rose');
    expect(figure(entry, 'Account requirement')).toBe('$1000.00 to $0.00 change -$1000.00');
  });

  it('says only the newest entries are listed when the counts cover more', async () => {
    await renderCard(page({ counts: { ...page().counts, entries: 37, compared: 30, not_compared: 7 } }));

    expect(await screen.findByText(/Showing the newest 2 of 37 entries\./)).toBeTruthy();
    expect(screen.getByText(/The counts above are over all of them\./)).toBeTruthy();
  });

  it('shows an entry nobody compared as that, with no readings and nothing about the account', async () => {
    await renderCard(page());

    const waiting = within(await screen.findByRole('list', { name: 'Option entries, newest first' }))
      .getAllByRole('listitem')[0];
    expect(within(waiting).getByText('Not compared')).toBeTruthy();
    expect(within(waiting).getByText('The comparison for this entry has not been made yet.')).toBeTruthy();
    expect(waiting.textContent).not.toContain('Option buying power before');
    expect(within(waiting).getByText(NOT_YET.account_requirement_sentence)).toBeTruthy();
  });

  it('says a bot with no option entry has made none, and shows no list and no review', async () => {
    await renderCard(page({
      counts: { entries: 0, compared: 0, not_compared: 0, not_compared_by_reason: [], sentence: 'This bot has made no option entry.' },
      checks: [],
    }));

    expect(await screen.findByText('This bot has made no option entry.')).toBeTruthy();
    expect(screen.queryByRole('list', { name: 'Option entries, newest first' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Review this notice/ })).toBeNull();
  });

  it('shows the hold with its words and the review action, and sends nothing until the owner confirms', async () => {
    const { review } = await renderCard(HELD);

    expect(await screen.findByText(HOLD_NOTICE.headline)).toBeTruthy();
    expect(screen.getByText(HOLD_NOTICE.explanation)).toBeTruthy();
    expect(screen.getByText(HOLD_NOTICE.impact)).toBeTruthy();
    expect(screen.getByText('Named by the notice')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Review this notice…' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(HOLD_NOTICE.review_consequence)).toBeTruthy();
    expect(review).not.toHaveBeenCalled();
  });

  it('records the review once the owner confirms, and shows the bot as no longer held', async () => {
    const { review } = await renderCard(HELD);
    fireEvent.click(await screen.findByRole('button', { name: 'Review this notice…' }));

    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Mark as reviewed' }));

    await waitFor(() => expect(review).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText(HOLD_NOTICE.headline)).toBeNull());
    expect(screen.queryByRole('button', { name: /Review this notice/ })).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('reviews nothing when the owner cancels the confirmation', async () => {
    const { review } = await renderCard(HELD);
    fireEvent.click(await screen.findByRole('button', { name: 'Review this notice…' }));

    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(review).not.toHaveBeenCalled();
    expect(screen.getByText(HOLD_NOTICE.headline)).toBeTruthy();
  });

  it('keeps the hold and says so in the backend’s words when the review is refused', async () => {
    const refused = new HttpErrorResponse({
      status: 409,
      error: panelRefusalBody({
        message: 'The Clerk is not serving this account right now.',
        why: 'A review is recorded only by a serving Clerk.',
        next_action: 'Try again once the account is back.',
        reason_code: 'CLERK_UNAVAILABLE',
      }),
    });
    await renderCard(HELD, { reviewed: Promise.reject(refused) });
    fireEvent.click(await screen.findByRole('button', { name: 'Review this notice…' }));

    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Mark as reviewed' }));

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('The Clerk is not serving this account right now.')).toBeTruthy();
    expect(screen.getByText(HOLD_NOTICE.headline)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Review this notice…' })).toBeTruthy();
  });

  it('leaves the review to the page while another command on the bot is on its way', async () => {
    const { review } = await renderCard(HELD, { locked: true });

    const button = await screen.findByRole('button', { name: 'Review this notice…' });

    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(review).not.toHaveBeenCalled();
  });

  it('reads again, keeping what is shown, when the panel’s journal moves', async () => {
    const { read, fixture } = await renderCard(page(), { journalTailSeq: 7 });
    await screen.findByRole('region', { name: 'Alpaca option buying power' });
    expect(read).toHaveBeenCalledTimes(1);

    fixture.componentRef.setInput('journalTailSeq', 8);
    await fixture.whenStable();

    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('region', { name: 'Alpaca option buying power' })).toBeTruthy();
  });

  it('reads again after a failed read, by itself on the next record and when asked', async () => {
    const { read, fixture } = await renderCard(page(), { journalTailSeq: 7 });
    await screen.findByRole('region', { name: 'Alpaca option buying power' });
    // A fresh card whose first read failed: no page, an alert, and a way to ask again.
    read.mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: null }));
    fixture.componentRef.setInput('broker', 'alpaca');
    fixture.componentRef.setInput('sid', 'bot-2');
    await fixture.whenStable();
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(2);

    fixture.componentRef.setInput('journalTailSeq', 8);
    await fixture.whenStable();
    await waitFor(() => expect(read).toHaveBeenCalledTimes(3));
    expect(await screen.findByRole('region', { name: 'Alpaca option buying power' })).toBeTruthy();
  });

  it('offers Try again on a failed first read', async () => {
    const read = vi.fn<(target: unknown, sid: string) => Promise<OptionBuyingPowerChecks>>()
      .mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: null }))
      .mockResolvedValue(page());
    await render(BotOptionBuyingPowerComponent, {
      inputs: {
        broker: 'alpaca', clerkId: 'clrk_spec0000000000000000aa', accountId: 'PA-1', sid: 'bot-1',
        journalTailSeq: 7, review: vi.fn<Review>(), locked: false,
      },
      providers: [provideFleetDirectory(), { provide: BrokerV2PanelService, useValue: { getOptionBuyingPowerChecks: read } }],
    });
    await screen.findByRole('alert');

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('region', { name: 'Alpaca option buying power' })).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(2);
  });
});
