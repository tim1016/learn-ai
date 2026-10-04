import { HttpErrorResponse } from '@angular/common/http';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import type { SqliteAttestationFindings, SqliteUnknownOrderAttestation } from '../../../../api/alpaca.types';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { fakeSqliteSettleAction, panelRefusalBody } from '../../../../testing/bot-panel-fixtures';
import type { PanelAction } from '../lib/broker-v2-panel.types';
import {
  SettleOrderTicketComponent,
  offersSettlement,
  type SettleAction,
  type SettlementCheck,
} from './settle-order-ticket.component';

type Check = (attestation: SqliteUnknownOrderAttestation) => Promise<SettlementCheck | null>;
type Settle = (attestation: SqliteUnknownOrderAttestation) => Promise<void>;

/** The settle action as the page hands it to the ticket: one that offers its order. */
function settleAction(overrides: Partial<PanelAction> = {}): SettleAction {
  const action = fakeSqliteSettleAction(overrides);
  if (!offersSettlement(action)) throw new Error('The settle fixture carries its order.');
  return action;
}

const ACTION = settleAction();
const { settlement: SETTLEMENT, confirmation: CONFIRMATION } = ACTION;
if (!CONFIRMATION) throw new Error('The settle fixture carries its confirmation.');
const ORDER_ID = '61e69015-8549-4bfd-b9c3-01e75843f47d';
const READ_AT_MS = 1_700_000_100_000;

/** What the Clerk read for "Alpaca has no such order", as `unknown_order_attestation._absence_lines` words it. */
const NOTHING_AT_ALPACA: SqliteAttestationFindings = {
  attestation: 'absent',
  would_settle: true,
  refusal_reason_code: null,
  lines: [
    'Alpaca shows no order under this order\'s id.',
    'No order is working on SPY.',
    'Since this one was sent, Alpaca shows no order to sell SPY that the Clerk did not place.',
    'Alpaca holds 10 SPY, as the Clerk\'s records say.',
  ],
  observed_at_ms: READ_AT_MS,
};

/** What the Clerk read for "Alpaca shows it": the order under that id is this order. */
const IT_IS_THIS_ORDER: SqliteAttestationFindings = {
  attestation: 'exists',
  would_settle: true,
  refusal_reason_code: null,
  lines: [
    `Alpaca order ${ORDER_ID}: sell 10 SPY, filled, 10 filled.`,
    'It carries this order\'s client order id, symbol, side and quantity.',
    'The Clerk takes it up as this exit\'s order. Nothing is sent.',
  ],
  observed_at_ms: READ_AT_MS,
};

/** The Clerk's refusal of a wrong id: an answer, not an error (`check_unknown_exit_order`). */
const NO_SUCH_ORDER: SqliteAttestationFindings = {
  attestation: 'exists',
  would_settle: false,
  refusal_reason_code: 'NO_SUCH_BROKER_ORDER',
  lines: [`Alpaca has no order with the id '${ORDER_ID}'. Check the id at Alpaca. Nothing was changed.`],
  observed_at_ms: READ_AT_MS,
};

function answered(findings: SqliteAttestationFindings): SettlementCheck {
  return { attestation_findings: findings };
}

async function renderTicket(
  options: { action?: SettleAction; check?: Check; settle?: Settle; locked?: boolean } = {},
) {
  const check = vi.fn<Check>(options.check ?? (() => Promise.resolve(answered(NOTHING_AT_ALPACA))));
  const settle = vi.fn<Settle>(options.settle ?? (() => Promise.resolve()));
  const closed = vi.fn();
  const user = userEvent.setup();
  const rendered = await render(SettleOrderTicketComponent, {
    inputs: { action: options.action ?? ACTION, accountId: 'PA-TEST', check, settle, locked: options.locked ?? false },
    on: { closed },
  });
  return { ...rendered, check, settle, closed, user };
}

function ticket(): HTMLElement {
  return screen.getByRole('region', { name: 'Settle the uncertain order' });
}

const checkButton = (): HTMLButtonElement => within(ticket()).getByRole('button', { name: 'Ask the Clerk to check' });
const settleButton = (): HTMLButtonElement => within(ticket()).getByRole('button', { name: 'Settle the order…' });

/** Type the word and confirm, in the confirmation the ticket opened. */
async function confirmSettle(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  const dialog = screen.getByRole('dialog', { name: 'Settle the uncertain order?' });
  await user.type(within(dialog).getByRole('textbox'), 'SETTLE');
  await user.click(within(dialog).getByRole('button', { name: 'Settle the order' }));
}

describe('SettleOrderTicketComponent (#2854)', () => {
  it('shows the Clerk’s ticket and both choices in the backend’s words, and asks nothing until one is chosen', async () => {
    const { check } = await renderTicket();

    expect(within(ticket()).getByText(SETTLEMENT.ticket)).toBeTruthy();
    const choices = within(ticket()).getByRole('group', { name: 'What Alpaca shows' });
    for (const choice of SETTLEMENT.choices) {
      expect(within(choices).getByRole('radio', { name: new RegExp(`^${choice.label}`) })).toBeTruthy();
      expect(within(choices).getByText(choice.explanation)).toBeTruthy();
    }
    expect(within(ticket()).queryByRole('textbox')).toBeNull();
    expect(checkButton().disabled).toBe(true);
    expect(check).not.toHaveBeenCalled();
  });

  it('settles "Alpaca has no such order" only after the Clerk checked and the word was typed', async () => {
    const { check, settle, user } = await renderTicket();

    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));
    await user.click(checkButton());

    expect(check).toHaveBeenCalledExactlyOnceWith({ kind: 'absent' });
    const found = await within(ticket()).findByRole('status', { name: 'What the Clerk found' });
    expect(within(found).getAllByRole('listitem').map((line) => line.textContent)).toEqual(NOTHING_AT_ALPACA.lines);
    expect(found.textContent).toContain(formatTimestampDisplay(READ_AT_MS, { mode: 'local' }));
    expect(settle).not.toHaveBeenCalled();

    await user.click(settleButton());
    const dialog = screen.getByRole('dialog', { name: 'Settle the uncertain order?' });
    // The confirmation says what the Clerk found, the backend's consequence, the symbol and the account.
    expect(dialog.textContent).toContain(NOTHING_AT_ALPACA.lines.join(' '));
    expect(dialog.textContent).toContain(CONFIRMATION.consequence);
    expect(dialog.textContent).toContain('PA-TEST');
    expect(dialog.querySelector('app-asset-identity')?.textContent).toContain('SPY');
    const confirm = within(dialog).getByRole<HTMLButtonElement>('button', { name: 'Settle the order' });
    expect(confirm.disabled).toBe(true);
    await user.type(within(dialog).getByRole('textbox'), 'SETTLE');
    expect(confirm.disabled).toBe(false);
    await user.click(confirm);

    expect(settle).toHaveBeenCalledExactlyOnceWith({ kind: 'absent' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('asks for the word the backend’s confirmation names, not one of its own', async () => {
    const worded = settleAction({ confirmation: { ...CONFIRMATION, required_token: 'AGREE' } });
    const { settle, user } = await renderTicket({ action: worded });
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));
    await user.click(checkButton());
    await user.click(await within(ticket()).findByRole('button', { name: 'Settle the order…' }));

    const dialog = screen.getByRole('dialog', { name: 'Settle the uncertain order?' });
    const confirm = within(dialog).getByRole<HTMLButtonElement>('button', { name: 'Settle the order' });
    await user.type(within(dialog).getByRole('textbox'), 'SETTLE');
    expect(confirm.disabled).toBe(true);
    await user.clear(within(dialog).getByRole('textbox'));
    await user.type(within(dialog).getByRole('textbox'), 'AGREE');
    await user.click(confirm);

    expect(settle).toHaveBeenCalledExactlyOnceWith({ kind: 'absent' });
  });

  it('asks for the Alpaca order id with "Alpaca shows it", trims it, and settles on that order', async () => {
    const { check, settle, user } = await renderTicket({ check: () => Promise.resolve(answered(IT_IS_THIS_ORDER)) });

    await user.click(screen.getByRole('radio', { name: /^Alpaca shows it/ }));
    const orderId = within(ticket()).getByRole<HTMLInputElement>('textbox', { name: 'Alpaca order id' });
    // The id is required: nothing can be asked while it is blank, or only spaces.
    expect(orderId.required).toBe(true);
    expect(checkButton().disabled).toBe(true);
    await user.type(orderId, '   ');
    expect(checkButton().disabled).toBe(true);
    await user.clear(orderId);
    await user.type(orderId, `  ${ORDER_ID} `);
    await user.click(checkButton());

    const said = { kind: 'exists', broker_order_id: ORDER_ID };
    expect(check).toHaveBeenCalledExactlyOnceWith(said);
    const found = await within(ticket()).findByRole('status', { name: 'What the Clerk found' });
    expect(within(found).getAllByRole('listitem').map((line) => line.textContent)).toEqual(IT_IS_THIS_ORDER.lines);

    await user.click(settleButton());
    await confirmSettle(user);

    expect(settle).toHaveBeenCalledExactlyOnceWith(said);
  });

  it('shows the Clerk’s refusal at the check as written, settles nothing, and lets the operator say the other thing', async () => {
    const check = vi.fn<Check>()
      .mockResolvedValueOnce(answered(NO_SUCH_ORDER))
      .mockResolvedValueOnce(answered(NOTHING_AT_ALPACA));
    const { settle, closed, user } = await renderTicket({ check });

    await user.click(screen.getByRole('radio', { name: /^Alpaca shows it/ }));
    await user.type(screen.getByRole('textbox', { name: 'Alpaca order id' }), ORDER_ID);
    await user.click(checkButton());

    const refused = await within(ticket()).findByRole('alert', { name: 'What the Clerk found' });
    expect(refused.textContent).toContain(NO_SUCH_ORDER.lines[0]);
    // An answer that would not settle the order offers no settlement.
    expect(within(ticket()).queryByRole('button', { name: 'Settle the order…' })).toBeNull();
    expect(closed).not.toHaveBeenCalled();

    // Going back: the other choice drops what was found for the first one.
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));
    expect(within(ticket()).queryByText(NO_SUCH_ORDER.lines[0])).toBeNull();
    await user.click(checkButton());
    expect(check).toHaveBeenLastCalledWith({ kind: 'absent' });
    await user.click(await within(ticket()).findByRole('button', { name: 'Settle the order…' }));
    await confirmSettle(user);

    expect(settle).toHaveBeenCalledExactlyOnceWith({ kind: 'absent' });
  });

  it('shows the backend’s own message for an id it cannot read (422) and for a refused check (409)', async () => {
    const notAnOrderId = 'That is not an Alpaca order id. Copy the order id from the order\'s page at Alpaca; '
      + 'it looks like 904837e3-3b76-47ec-b432-046db621571b.';
    const notAnId = new HttpErrorResponse({
      status: 422, error: { detail: { reason: 'not_an_alpaca_order_id', message: notAnOrderId } },
    });
    const sentAgain = new HttpErrorResponse({
      status: 409,
      error: { detail: { reason: 'stale_action_token', message: 'The recovery evidence changed. Refresh before confirming the action.' } },
    });
    const check = vi.fn<Check>().mockRejectedValueOnce(notAnId).mockRejectedValueOnce(sentAgain);
    const { settle, closed, user } = await renderTicket({ check });

    await user.click(screen.getByRole('radio', { name: /^Alpaca shows it/ }));
    await user.type(screen.getByRole('textbox', { name: 'Alpaca order id' }), 'close-1');
    await user.click(checkButton());

    // No second validator here: the id went to the backend as typed, and its answer is shown.
    expect(check).toHaveBeenLastCalledWith({ kind: 'exists', broker_order_id: 'close-1' });
    expect((await within(ticket()).findByRole('alert')).textContent).toContain(notAnOrderId);

    await user.click(checkButton());
    expect((await within(ticket()).findByRole('alert')).textContent)
      .toContain('The recovery evidence changed. Refresh before confirming the action.');
    expect(within(ticket()).queryByRole('button', { name: 'Settle the order…' })).toBeNull();
    expect(settle).not.toHaveBeenCalled();
    expect(closed).not.toHaveBeenCalled();
  });

  it('shows the Clerk’s refusal at the settlement as written, stays open, and can be settled again', async () => {
    const busy = new HttpErrorResponse({
      status: 409,
      error: panelRefusalBody({
        message: 'The Clerk is working on this exit right now. Try again in a few seconds. Nothing was changed.',
        why: null,
        next_action: null,
      }),
    });
    const settle = vi.fn<Settle>().mockRejectedValueOnce(busy).mockResolvedValueOnce();
    const { closed, user } = await renderTicket({ settle });

    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));
    await user.click(checkButton());
    await user.click(await within(ticket()).findByRole('button', { name: 'Settle the order…' }));
    await confirmSettle(user);

    expect((await within(ticket()).findByRole('alert')).textContent)
      .toContain('The Clerk is working on this exit right now. Try again in a few seconds. Nothing was changed.');
    expect(closed).not.toHaveBeenCalled();
    // What the Clerk found is still there, and so is the way to settle.
    expect(within(ticket()).getByRole('status', { name: 'What the Clerk found' })).toBeTruthy();

    await user.click(settleButton());
    await confirmSettle(user);

    expect(settle).toHaveBeenCalledTimes(2);
    expect(settle.mock.calls[1]).toEqual(settle.mock.calls[0]);
    expect(within(ticket()).queryByRole('alert')).toBeNull();
  });

  it('moves the keyboard to what the Clerk found, and to a refused settlement, as each arrives', async () => {
    const busy = new HttpErrorResponse({
      status: 409,
      error: panelRefusalBody({ message: 'The Clerk is working on this exit right now.', why: null, next_action: null }),
    });
    const { user } = await renderTicket({ settle: () => Promise.reject(busy) });
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));

    // The check button is disabled while it asks, which drops the keyboard: the answer picks it up.
    await user.click(checkButton());
    const found = await within(ticket()).findByRole('status', { name: 'What the Clerk found' });
    await vi.waitFor(() => expect(document.activeElement).toBe(found));
    expect(ticket().contains(document.activeElement)).toBe(true);

    // The confirmation is gone once confirmed: the refusal picks the keyboard up.
    await user.click(settleButton());
    await confirmSettle(user);
    const refused = await within(ticket()).findByRole('alert');
    await vi.waitFor(() => expect(document.activeElement).toBe(refused));
    expect(ticket().contains(document.activeElement)).toBe(true);
  });

  it('moves the keyboard to a refused check', async () => {
    const stale = new HttpErrorResponse({
      status: 409,
      error: { detail: { reason: 'stale_action_token', message: 'The recovery evidence changed. Refresh before confirming the action.' } },
    });
    const { user } = await renderTicket({ check: () => Promise.reject(stale) });
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));

    await user.click(checkButton());

    const refused = await within(ticket()).findByRole('alert');
    await vi.waitFor(() => expect(document.activeElement).toBe(refused));
    expect(ticket().contains(document.activeElement)).toBe(true);
  });

  it('shows nothing of a check the page answered itself, and leaves the keyboard where it is', async () => {
    // The refusal came back after the ticket left that send of the order: the page has said it.
    const { fixture, user } = await renderTicket({ check: () => Promise.resolve(null) });
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));

    await user.click(checkButton());
    await fixture.whenStable();

    expect(checkButton().disabled).toBe(false);
    expect(within(ticket()).queryByRole('alert')).toBeNull();
    expect(within(ticket()).queryByRole('status', { name: 'What the Clerk found' })).toBeNull();
    expect(document.activeElement).toBe(checkButton());
  });

  it('never shows a check’s answer that lands after the page presented the action anew', async () => {
    let answerLate: (check: SettlementCheck) => void = () => undefined;
    const check = vi.fn<Check>().mockReturnValueOnce(new Promise((resolve) => { answerLate = resolve; }));
    const { settle, user, rerender } = await renderTicket({ check });
    const noSuchOrder = (): HTMLInputElement => screen.getByRole('radio', { name: /^Alpaca has no such order/ });
    await user.click(noSuchOrder());
    await user.click(checkButton());

    // The Clerk sent the order again while it was checking: the action comes back under a new token.
    await rerender({ inputs: { action: settleAction({ concurrency_token: 'sent-again' }) }, partialUpdate: true });
    answerLate(answered(NOTHING_AT_ALPACA));
    await vi.waitFor(() => expect(noSuchOrder().disabled).toBe(false));

    // The keyboard stays on the ticket, which shows no answer.
    await vi.waitFor(() => expect(document.activeElement).toBe(ticket()));

    // The same thing said of the new send: what was read for the old one is not its answer.
    await user.click(noSuchOrder());
    expect(within(ticket()).queryByRole('status', { name: 'What the Clerk found' })).toBeNull();
    expect(within(ticket()).queryByRole('button', { name: 'Settle the order…' })).toBeNull();
    expect(checkButton().disabled).toBe(false);
    expect(settle).not.toHaveBeenCalled();
  });

  it('holds every control still while another command on the bot is on its way', async () => {
    const { check } = await renderTicket({ locked: true });

    for (const radio of within(ticket()).getAllByRole<HTMLInputElement>('radio')) {
      expect(radio.disabled).toBe(true);
    }
    expect(checkButton().disabled).toBe(true);
    expect(within(ticket()).getByRole<HTMLButtonElement>('button', { name: 'Close' }).disabled).toBe(true);
    expect(check).not.toHaveBeenCalled();
  });

  it('starts over when the action is presented again', async () => {
    const { user, rerender } = await renderTicket();
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));
    await user.click(checkButton());
    await within(ticket()).findByRole('status', { name: 'What the Clerk found' });

    // The Clerk sent the order again: the page presents the action anew, with a new token.
    await rerender({ inputs: { action: settleAction({ concurrency_token: 'sent-again' }) }, partialUpdate: true });

    expect(within(ticket()).queryByRole('status', { name: 'What the Clerk found' })).toBeNull();
    expect(within(ticket()).getAllByRole<HTMLInputElement>('radio').some((radio) => radio.checked)).toBe(false);
    expect(checkButton().disabled).toBe(true);
  });

  it('closes an open confirmation when the action is presented again', async () => {
    const { settle, user, rerender } = await renderTicket();
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));
    await user.click(checkButton());
    await user.click(await within(ticket()).findByRole('button', { name: 'Settle the order…' }));
    expect(screen.getByRole('dialog', { name: 'Settle the uncertain order?' })).toBeTruthy();

    // The Clerk sent the order again: what the confirmation asked about was found for the older send.
    await rerender({ inputs: { action: settleAction({ concurrency_token: 'sent-again' }) }, partialUpdate: true });

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(settle).not.toHaveBeenCalled();
  });

  it('closes on Close, having asked and settled nothing', async () => {
    const { check, settle, closed, user } = await renderTicket();

    await user.click(within(ticket()).getByRole('button', { name: 'Close' }));

    expect(closed).toHaveBeenCalledTimes(1);
    expect(check).not.toHaveBeenCalled();
    expect(settle).not.toHaveBeenCalled();
  });

  it('has no accessibility violations, with what the Clerk found shown and with its refusal shown', async () => {
    const check = vi.fn<Check>()
      .mockResolvedValueOnce(answered(NOTHING_AT_ALPACA))
      .mockResolvedValueOnce(answered(NO_SUCH_ORDER));
    const { user } = await renderTicket({ check });
    await user.click(screen.getByRole('radio', { name: /^Alpaca has no such order/ }));
    await user.click(checkButton());
    await within(ticket()).findByRole('status', { name: 'What the Clerk found' });

    const found = await axe.run(ticket(), { rules: { 'color-contrast': { enabled: false } } });
    expect(found.violations).toEqual([]);

    await user.click(screen.getByRole('radio', { name: /^Alpaca shows it/ }));
    await user.type(screen.getByRole('textbox', { name: 'Alpaca order id' }), ORDER_ID);
    await user.click(checkButton());
    await within(ticket()).findByRole('alert', { name: 'What the Clerk found' });

    const refused = await axe.run(ticket(), { rules: { 'color-contrast': { enabled: false } } });
    expect(refused.violations).toEqual([]);
  });
});
