import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerOrderRequest, OrderSubmitResult } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';

function ackedResult(orderRef = 'manual/desk/v1:abc123'): OrderSubmitResult {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    results: [
      {
        status: 'acked',
        order_ref: orderRef,
        intent_id: 'abc123',
        order: {
          broker: 'alpaca',
          order_id: 'broker-order-1',
          client_order_id: orderRef,
          symbol: 'SPY',
          asset_class: 'us_equity',
          side: 'buy',
          order_type: 'market',
          time_in_force: 'day',
          quantity: 2,
          filled_quantity: 0,
          limit_price: null,
          stop_price: null,
          filled_avg_price: null,
          status: 'accepted',
          submitted_at_ms: 1_700_000_000_000,
          created_at_ms: 1_700_000_000_000,
          updated_at_ms: 1_700_000_000_000,
          filled_at_ms: null,
          canceled_at_ms: null,
          expired_at_ms: null,
          events: [],
          observed_at_ms: 1_700_000_000_000,
        },
        error: null,
      },
    ],
  };
}

async function renderPanel(
  submitOrder: (broker: string, request: BrokerOrderRequest) => Promise<OrderSubmitResult>,
) {
  return render(AlpacaOrderEntryComponent, {
    inputs: { expectedAccountId: 'PA1' },
    providers: [{ provide: BrokersService, useValue: { submitOrder } }],
  });
}

async function fillFirstLeg(symbol: string, quantity: string): Promise<void> {
  fireEvent.input(await screen.findByLabelText('Leg 1 symbol'), {
    target: { value: symbol },
  });
  const qtyInput = await screen.findByLabelText('Leg 1 quantity');
  fireEvent.input(qtyInput, { target: { value: quantity } });
  fireEvent.change(qtyInput, { target: { value: quantity } });
}

/** Select a native Signal Forms-backed control by its wire value. */
function selectOption(controlAriaLabel: string, value: string): void {
  const control = screen.getByLabelText(controlAriaLabel);
  fireEvent.input(control, { target: { value } });
  fireEvent.change(control, { target: { value } });
}

async function setLimitPrice(price: string): Promise<void> {
  const priceInput = await screen.findByLabelText('Leg 1 limit price');
  fireEvent.input(priceInput, { target: { value: price } });
  fireEvent.change(priceInput, { target: { value: price } });
}

describe('AlpacaOrderEntryComponent', () => {
  it('adds an equity leg, previews, confirms, and submits the right payload', async () => {
    const submitOrder = vi.fn().mockResolvedValue(ackedResult());
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');

    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    await vi.waitFor(() => expect(submitOrder).toHaveBeenCalledTimes(1));
    const [broker, request] = submitOrder.mock.calls[0];
    expect(broker).toBe('alpaca');
    // A market leg defaults to DAY and carries no limit_price.
    expect(request).toEqual({
      operator: 'desk',
      expected_account_id: 'PA1',
      legs: [
        {
          symbol: 'SPY',
          side: 'buy',
          quantity: 2,
          order_type: 'market',
          time_in_force: 'day',
        },
      ],
    });

    // The per-leg result renders (acked), with the opaque order_ref shown exactly.
    expect(await screen.findByText('manual/desk/v1:abc123')).toBeTruthy();
  });

  it('uses the SQLite ticket path for ordered limit/GTC legs without a browser operator', async () => {
    const submitOrder = vi.fn();
    const previewSqliteManualOrder = vi.fn().mockResolvedValue({
      capability: { available: true, unavailable: null, supported_order_shape: 'BUY or SELL market/limit DAY/GTC equity, one to eight ordered legs' },
      preview_token: 'a'.repeat(64),
      authority_generation: 1,
      db_identity_token: 'db-token',
      control_revision: 1,
      subject_id: 'manual-operator:operator',
    });
    const submitSqliteManualOrder = vi.fn().mockResolvedValue({
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'ACTIVE',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [
        {
          leg_id: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
          sequence_index: 0,
          instruction_hash: 'hash',
          instruction: { symbol: 'SPY', side: 'buy', quantity: 2, order_type: 'limit', limit_price: 500, time_in_force: 'gtc' },
          state: 'IN_PROGRESS',
          command: { command_id: 'cmd', state: 'in_progress', action: 'SUBMIT_MANUAL_ORDER', receipt_id: null },
          effect: { effect_operation_id: 'effect', state: 'in_progress', kind: 'MANUAL_ORDER', terminal_receipt_id: null },
          order: { order_ref: 'manual/operator/v1:abc', client_order_id: 'manual/operator/v1:abc', broker_order_id: 'broker', broker_state: 'accepted' },
          cancellation: null,
        },
      ],
    });
    await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
        manualLegId: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
        manualCapability: {
          available: true,
          unavailable: null,
          supported_order_shape: 'BUY or SELL market/limit DAY/GTC equity, one to eight ordered legs',
        },
      },
      providers: [{
        provide: BrokersService,
        useValue: {
          submitOrder,
          previewSqliteManualOrder,
          submitSqliteManualOrder,
          getSqliteManualOrderTicket: vi.fn().mockRejectedValue(new HttpErrorResponse({ status: 404 })),
        },
      }],
    });

    await fillFirstLeg('spy', '2');
    expect(screen.getByRole('button', { name: 'Add equity leg' })).toBeTruthy();
    expect(screen.getByLabelText('Leg 1 side')).toBeTruthy();
    expect(screen.getAllByText('Market').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Day').length).toBeGreaterThan(0);

    selectOption('Leg 1 order type', 'limit');
    await setLimitPrice('500');
    selectOption('Leg 1 time in force', 'gtc');
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    await vi.waitFor(() => expect(previewSqliteManualOrder).toHaveBeenCalledTimes(1));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    await vi.waitFor(() => expect(submitSqliteManualOrder).toHaveBeenCalledTimes(1));
    expect(previewSqliteManualOrder).toHaveBeenCalledWith('PA1', {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      legs: [
        {
          leg_id: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
          instruction: {
            symbol: 'SPY', side: 'buy', quantity: 2, order_type: 'limit', limit_price: 500, time_in_force: 'gtc',
          },
        },
      ],
    });
    expect(submitSqliteManualOrder).toHaveBeenCalledWith(
      'PA1',
      '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      expect.objectContaining({ preview_token: 'a'.repeat(64) }),
    );
    expect(submitOrder).not.toHaveBeenCalled();
    expect(await screen.findByText(/Ticket.*is Active/)).toBeTruthy();
    expect(screen.getByText(/Buy 2 Limit GTC at 500/)).toBeTruthy();
    expect(screen.getByText('manual/operator/v1:abc')).toBeTruthy();
  });

  it('lets a SQLite ticket preview a SELL reduction even when new exposure is unavailable', async () => {
    const previewSqliteManualOrder = vi.fn().mockResolvedValue({
      capability: { available: true, unavailable: null, supported_order_shape: 'BUY or SELL market/limit DAY/GTC equity, one to eight ordered legs' },
      preview_token: 'b'.repeat(64),
      authority_generation: 1,
      db_identity_token: 'db-token',
      control_revision: 1,
      subject_id: 'manual-operator:operator',
    });
    await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
        manualLegId: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
        manualCapability: {
          available: false,
          unavailable: { code: 'MANUAL_ORDER_OUTSTANDING', message: 'A manual order is unresolved.' },
          supported_order_shape: 'BUY or SELL market/limit DAY/GTC equity, one to eight ordered legs',
        },
      },
      providers: [{
        provide: BrokersService,
        useValue: {
          previewSqliteManualOrder,
          getSqliteManualOrderTicket: vi.fn().mockRejectedValue(new HttpErrorResponse({ status: 404 })),
        },
      }],
    });

    await fillFirstLeg('spy', '2');
    selectOption('Leg 1 side', 'sell');
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));

    await vi.waitFor(() => expect(previewSqliteManualOrder).toHaveBeenCalledWith('PA1', {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      legs: [
        {
          leg_id: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
          instruction: {
            symbol: 'SPY', side: 'sell', quantity: 2, order_type: 'market', time_in_force: 'day',
          },
        },
      ],
    }));
  });

  it('continues exactly one reserved ticket leg only after a refreshed preview', async () => {
    const ticket = {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'ACTIVE',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [
        {
          leg_id: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
          sequence_index: 0,
          instruction_hash: 'first-hash',
          instruction: { symbol: 'SPY', side: 'buy', quantity: 1, order_type: 'market', time_in_force: 'day' },
          state: 'IN_PROGRESS',
          command: { command_id: 'first-command', state: 'in_progress', action: 'SUBMIT_MANUAL_ORDER', receipt_id: null },
          effect: { effect_operation_id: 'first-effect', state: 'in_progress', kind: 'MANUAL_ORDER', terminal_receipt_id: null },
          order: { order_ref: 'manual/operator/v1:first', client_order_id: 'manual/operator/v1:first', broker_order_id: 'broker-first', broker_state: 'accepted' },
          cancellation: null,
        },
        {
          leg_id: '5791929d-4a3f-4ffc-a15f-62c34cb6c873',
          sequence_index: 1,
          instruction_hash: 'second-hash',
          instruction: { symbol: 'SPY', side: 'buy', quantity: 2, order_type: 'limit', limit_price: 499.5, time_in_force: 'gtc' },
          state: 'RESERVED',
          command: null,
          effect: null,
          order: null,
          cancellation: null,
        },
      ],
    };
    const previewSqliteManualOrder = vi.fn().mockResolvedValue({
      capability: { available: true, unavailable: null, supported_order_shape: 'BUY or SELL market/limit DAY/GTC equity, one to eight ordered legs' },
      preview_token: 'c'.repeat(64),
      authority_generation: 1,
      db_identity_token: 'db-token',
      control_revision: 2,
      subject_id: 'manual-operator:operator',
    });
    const continueSqliteManualOrderTicket = vi.fn().mockResolvedValue({
      ...ticket,
      legs: [ticket.legs[0], { ...ticket.legs[1], state: 'IN_PROGRESS' }],
    });
    await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: ticket.ticket_id,
      },
      providers: [{
        provide: BrokersService,
        useValue: {
          getSqliteManualOrderTicket: vi.fn().mockResolvedValue(ticket),
          previewSqliteManualOrder,
          continueSqliteManualOrderTicket,
        },
      }],
    });

    fireEvent.click(await screen.findByRole('button', { name: /Continue remaining legs/i }));

    const firstLeg = screen.getByText('Leg 1').parentElement;
    expect(firstLeg?.textContent).toContain('Buy 1 Market Day');
    expect(firstLeg?.textContent).not.toContain('Buy 1 Market Day at');

    await vi.waitFor(() => expect(previewSqliteManualOrder).toHaveBeenCalledWith('PA1', {
      ticket_id: ticket.ticket_id,
      legs: [
        { leg_id: ticket.legs[0].leg_id, instruction: ticket.legs[0].instruction },
        { leg_id: ticket.legs[1].leg_id, instruction: ticket.legs[1].instruction },
      ],
    }));
    expect(continueSqliteManualOrderTicket).toHaveBeenCalledWith(
      'PA1',
      ticket.ticket_id,
      expect.objectContaining({ preview_token: 'c'.repeat(64) }),
    );
  });

  it('shows a safe error instead of silently continuing a ticket with missing instruction evidence', async () => {
    const ticket = {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'ACTIVE',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [
        {
          leg_id: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
          sequence_index: 0,
          instruction_hash: 'first-hash',
          instruction: null,
          state: 'IN_PROGRESS',
          command: { command_id: 'first-command', state: 'in_progress', action: 'SUBMIT_MANUAL_ORDER', receipt_id: null },
          effect: { effect_operation_id: 'first-effect', state: 'in_progress', kind: 'MANUAL_ORDER', terminal_receipt_id: null },
          order: { order_ref: 'manual/operator/v1:first', client_order_id: 'manual/operator/v1:first', broker_order_id: 'broker-first', broker_state: 'accepted' },
          cancellation: null,
        },
        {
          leg_id: '5791929d-4a3f-4ffc-a15f-62c34cb6c873',
          sequence_index: 1,
          instruction_hash: 'second-hash',
          instruction: { symbol: 'SPY', side: 'buy', quantity: 2, order_type: 'market', time_in_force: 'day' },
          state: 'RESERVED',
          command: null,
          effect: null,
          order: null,
          cancellation: null,
        },
      ],
    };
    const previewSqliteManualOrder = vi.fn();
    const continueSqliteManualOrderTicket = vi.fn();
    await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: ticket.ticket_id,
      },
      providers: [{
        provide: BrokersService,
        useValue: {
          getSqliteManualOrderTicket: vi.fn().mockResolvedValue(ticket),
          previewSqliteManualOrder,
          continueSqliteManualOrderTicket,
        },
      }],
    });

    fireEvent.click(await screen.findByRole('button', { name: /Continue remaining legs/i }));

    expect(await screen.findByText('The Clerk cannot recover this ticket leg for a safe continuation.')).toBeTruthy();
    expect(previewSqliteManualOrder).not.toHaveBeenCalled();
    expect(continueSqliteManualOrderTicket).not.toHaveBeenCalled();
  });

  it('cancels the manual ticket once and renders durable unknown guidance', async () => {
    const cancellation = {
      order_ref: 'manual/operator/v1:abc',
      cancel_request_id: '50ce3186-4d73-4d3a-bce9-d0f1768f0be5',
      state: 'UNKNOWN',
      message: 'The cancellation outcome is not yet known.',
      impact: 'The Clerk will not create another cancellation while exact broker evidence is unresolved.',
      next_action: 'Refresh this ticket while the Clerk reconciles the cancellation by its exact order reference.',
      command: { command_id: 'cancel-command', state: 'unknown', action: 'CANCEL_MANUAL_ORDER', receipt_id: null },
      effect: { effect_operation_id: 'cancel-effect', state: 'unknown', kind: 'CANCEL', terminal_receipt_id: null },
    };
    const ticket = {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'ACTIVE',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [{
        leg_id: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
        sequence_index: 0,
        instruction_hash: 'hash',
        instruction: { symbol: 'SPY', side: 'buy', quantity: 1, order_type: 'market', time_in_force: 'day' },
        state: 'IN_PROGRESS',
        command: { command_id: 'cmd', state: 'in_progress', action: 'SUBMIT_MANUAL_ORDER', receipt_id: null },
        effect: { effect_operation_id: 'effect', state: 'in_progress', kind: 'MANUAL_ORDER', terminal_receipt_id: null },
        order: { order_ref: 'manual/operator/v1:abc', client_order_id: 'manual/operator/v1:abc', broker_order_id: 'broker', broker_state: 'accepted' },
        cancellation: null,
      }],
    };
    const getSqliteManualOrderTicket = vi.fn().mockResolvedValue(ticket);
    const cancelSqliteManualOrderTicket = vi.fn().mockResolvedValue({
      ...ticket,
      state: 'PAUSED_UNKNOWN',
      legs: [{ ...ticket.legs[0], cancellation }],
    });
    await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: ticket.ticket_id,
      },
      providers: [{
        provide: BrokersService,
        useValue: { cancelSqliteManualOrderTicket, getSqliteManualOrderTicket },
      }],
    });

    fireEvent.click(await screen.findByRole('button', { name: /Cancel manual ticket 7de3a77c/i }));

    await vi.waitFor(() => expect(cancelSqliteManualOrderTicket).toHaveBeenCalledWith(
      'PA1',
      ticket.ticket_id,
      { cancel_request_id: expect.any(String) },
    ));
    expect(await screen.findByText(/Refresh this ticket while the Clerk reconciles/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Cancel manual ticket/i })).toBeNull();
  });

  it('creates a new cancellation identity after the ticket target changes', async () => {
    const firstTicket = {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'ACTIVE',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [{
        leg_id: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
        sequence_index: 0,
        instruction_hash: 'hash-a',
        instruction: { symbol: 'SPY', side: 'buy', quantity: 1, order_type: 'market', time_in_force: 'day' },
        state: 'IN_PROGRESS',
        command: { command_id: 'command-a', state: 'in_progress', action: 'SUBMIT_MANUAL_ORDER', receipt_id: null },
        effect: { effect_operation_id: 'effect-a', state: 'in_progress', kind: 'MANUAL_ORDER', terminal_receipt_id: null },
        order: { order_ref: 'manual/operator/v1:first', client_order_id: 'manual/operator/v1:first', broker_order_id: 'broker-a', broker_state: 'accepted' },
        cancellation: null,
      }],
    };
    const secondTicket = {
      ...firstTicket,
      ticket_id: '2ece6033-0c8a-45a5-82cc-c9a47bc81d94',
      legs: [{
        ...firstTicket.legs[0],
        leg_id: '3d76e323-c1d3-4f8b-8a0e-6137556f77bd',
        order: { order_ref: 'manual/operator/v1:second', client_order_id: 'manual/operator/v1:second', broker_order_id: 'broker-b', broker_state: 'accepted' },
      }],
    };
    const getSqliteManualOrderTicket = vi.fn(
      (_accountId: string, ticketId: string) => Promise.resolve(
        ticketId === firstTicket.ticket_id ? firstTicket : secondTicket,
      ),
    );
    const cancelSqliteManualOrderTicket = vi.fn(
      (_accountId: string, ticketId: string, _request: { cancel_request_id: string }) => Promise.resolve(
        ticketId === firstTicket.ticket_id ? firstTicket : secondTicket,
      ),
    );
    const view = await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: firstTicket.ticket_id,
        manualLegId: firstTicket.legs[0].leg_id,
      },
      providers: [{
        provide: BrokersService,
        useValue: { cancelSqliteManualOrderTicket, getSqliteManualOrderTicket },
      }],
    });

    fireEvent.click(await screen.findByRole('button', { name: /Cancel manual ticket 7de3a77c/i }));
    await vi.waitFor(() => expect(cancelSqliteManualOrderTicket).toHaveBeenCalledTimes(1));
    view.fixture.componentRef.setInput('manualTicketId', secondTicket.ticket_id);
    view.fixture.componentRef.setInput('manualLegId', secondTicket.legs[0].leg_id);
    view.fixture.detectChanges();

    fireEvent.click(await screen.findByRole('button', { name: /Cancel manual ticket 2ece6033/i }));
    await vi.waitFor(() => expect(cancelSqliteManualOrderTicket).toHaveBeenCalledTimes(2));
    expect(cancelSqliteManualOrderTicket.mock.calls[0][2].cancel_request_id).not.toBe(
      cancelSqliteManualOrderTicket.mock.calls[1][2].cancel_request_id,
    );
  });

  it('refreshes a nonterminal SQLite ticket from its durable status resource', async () => {
    const activeTicket = {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'ACTIVE',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [],
    };
    const getSqliteManualOrderTicket = vi.fn().mockResolvedValue(activeTicket);
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');

    await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: activeTicket.ticket_id,
      },
      providers: [{
        provide: BrokersService,
        useValue: { getSqliteManualOrderTicket },
      }],
    });

    await vi.waitFor(() => expect(getSqliteManualOrderTicket).toHaveBeenCalledTimes(1));
    const refreshCall = await vi.waitFor(() => {
      const call = setIntervalSpy.mock.calls.find(([, delay]) => delay === 5_000);
      expect(call).toBeDefined();
      return call;
    });
    if (refreshCall === undefined) throw new Error('expected a five-second refresh callback');
    const [refresh] = refreshCall;
    if (typeof refresh !== 'function') throw new Error('expected a refresh callback');
    refresh();
    await vi.waitFor(() => expect(getSqliteManualOrderTicket).toHaveBeenCalledTimes(2));
    setIntervalSpy.mockRestore();
  });

  it('stops polling a terminally uncertain SQLite ticket', async () => {
    const pausedTicket = {
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'PAUSED_UNKNOWN',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [],
    };
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');
    const getSqliteManualOrderTicket = vi.fn().mockResolvedValue(pausedTicket);

    await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: pausedTicket.ticket_id,
      },
      providers: [{
        provide: BrokersService,
        useValue: { getSqliteManualOrderTicket },
      }],
    });

    expect(await screen.findByLabelText('Manual ticket status')).toBeTruthy();
    expect(setIntervalSpy.mock.calls.some(([, delay]) => delay === 5_000)).toBe(false);
    setIntervalSpy.mockRestore();
  });

  it('drops a late ticket read when the desk account changes', async () => {
    let resolveInitialTicket: ((ticket: {
      ticket_id: string;
      subject_id: string;
      state: string;
      created_at_ms: number;
      updated_at_ms: number;
      legs: never[];
    }) => void) | undefined;
    const initialTicket = new Promise<{
      ticket_id: string;
      subject_id: string;
      state: string;
      created_at_ms: number;
      updated_at_ms: number;
      legs: never[];
    }>((resolve) => { resolveInitialTicket = resolve; });
    const getSqliteManualOrderTicket = vi.fn()
      .mockReturnValueOnce(initialTicket)
      .mockRejectedValueOnce(new HttpErrorResponse({ status: 404 }));
    const view = await render(AlpacaOrderEntryComponent, {
      inputs: {
        expectedAccountId: 'PA1',
        sqliteManualAuthority: true,
        manualTicketId: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      },
      providers: [{
        provide: BrokersService,
        useValue: { getSqliteManualOrderTicket },
      }],
    });

    await vi.waitFor(() => expect(getSqliteManualOrderTicket).toHaveBeenCalledWith(
      'PA1',
      '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
    ));
    view.fixture.componentRef.setInput('expectedAccountId', 'PA2');
    view.fixture.detectChanges();
    await vi.waitFor(() => expect(getSqliteManualOrderTicket).toHaveBeenCalledWith(
      'PA2',
      '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
    ));
    if (resolveInitialTicket === undefined) throw new Error('expected initial ticket resolver');
    resolveInitialTicket({
      ticket_id: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
      subject_id: 'manual-operator:operator',
      state: 'ACTIVE',
      created_at_ms: 1,
      updated_at_ms: 2,
      legs: [],
    });

    await vi.waitFor(() => expect(screen.queryByLabelText('Manual ticket status')).toBeNull());
  });

  it('reveals the limit-price field only when the order type is Limit', async () => {
    await renderPanel(vi.fn());
    await fillFirstLeg('spy', '2');

    // Market by default → no limit-price input.
    expect(screen.queryByLabelText('Leg 1 limit price')).toBeNull();

    selectOption('Leg 1 order type', 'limit');
    expect(await screen.findByLabelText('Leg 1 limit price')).toBeTruthy();

    // Switching back to Market hides it again.
    selectOption('Leg 1 order type', 'market');
    await vi.waitFor(() =>
      expect(screen.queryByLabelText('Leg 1 limit price')).toBeNull(),
    );
  });

  it('threads a GTC limit order into the submit payload with its price', async () => {
    const submitOrder = vi.fn().mockResolvedValue(ackedResult());
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');
    selectOption('Leg 1 order type', 'limit');
    await setLimitPrice('240.5');
    selectOption('Leg 1 time in force', 'gtc');

    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    await vi.waitFor(() => expect(submitOrder).toHaveBeenCalledTimes(1));
    const [, request] = submitOrder.mock.calls[0];
    expect(request).toEqual({
      operator: 'desk',
      expected_account_id: 'PA1',
      legs: [
        {
          symbol: 'SPY',
          side: 'buy',
          quantity: 2,
          order_type: 'limit',
          time_in_force: 'gtc',
          limit_price: 240.5,
        },
      ],
    });
  });

  it('submits a DAY market order without a limit_price', async () => {
    const submitOrder = vi.fn().mockResolvedValue(ackedResult());
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');
    // Leave defaults (Market / Day).

    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    await vi.waitFor(() => expect(submitOrder).toHaveBeenCalledTimes(1));
    const [, request] = submitOrder.mock.calls[0];
    expect(request.legs[0].order_type).toBe('market');
    expect(request.legs[0].time_in_force).toBe('day');
    expect('limit_price' in request.legs[0]).toBe(false);
  });

  it('presents the supported equity order capabilities', async () => {
    await renderPanel(vi.fn());

    expect(screen.getAllByText('Market').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Limit').length).toBeGreaterThan(0);
    expect(screen.getByText('Day or GTC')).toBeTruthy();
  });

  it('renders a typed per-leg failure without a raw error', async () => {
    const failing: OrderSubmitResult = {
      broker: 'alpaca',
      account_id: 'PA1',
      results: [
        {
          status: 'failed',
          order_ref: 'manual/desk/v1:zzz',
          intent_id: 'zzz',
          order: null,
          error: { message: 'insufficient buying power', why: 'HTTP 422' },
        },
      ],
    };
    const submitOrder = vi.fn().mockResolvedValue(failing);
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    const results = await screen.findByLabelText('Submission results');
    expect(within(results).getByText(/insufficient buying power/)).toBeTruthy();
  });

  it('renders an uncertain result as a warning, not a failure', async () => {
    // An uncertain submit MAY have landed at the broker. It must not render as a
    // red failure — that would invite a double-submit of a live order.
    const uncertain: OrderSubmitResult = {
      broker: 'alpaca',
      account_id: 'PA1',
      results: [
        {
          status: 'uncertain',
          order_ref: 'manual/desk/v1:unc',
          intent_id: 'unc',
          order: null,
          error: { message: 'The order outcome is not yet known.', why: 'timeout' },
        },
      ],
    };
    const submitOrder = vi.fn().mockResolvedValue(uncertain);
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    const results = await screen.findByLabelText('Submission results');
    // Status reads "Uncertain" (not "Failed").
    expect(within(results).getByText('Uncertain')).toBeTruthy();
    // The detail message is not styled as an error.
    const message = within(results).getByText(/outcome is not yet known/);
    expect(message.classList.contains('text-red-500')).toBe(false);
  });

  it('treats a lost browser response as an uncertain submission', async () => {
    const submitOrder = vi.fn().mockRejectedValue(new Error('network lost'));
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    expect(
      await screen.findByText(
        'The submission outcome is uncertain. Check Alpaca orders and the journal before submitting again.',
      ),
    ).toBeTruthy();
    await vi.waitFor(() => expect(screen.queryByText('Confirm order')).toBeNull());
  });

  it('renders a definite HTTP rejection and closes the confirmation dialog', async () => {
    const submitOrder = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 422,
        statusText: 'Unprocessable Entity',
        error: { detail: 'GTC fractional orders are not supported.' },
      }),
    );
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    expect(
      await screen.findByText('Order rejected: GTC fractional orders are not supported.'),
    ).toBeTruthy();
    await vi.waitFor(() => expect(screen.queryByText('Confirm order')).toBeNull());
  });
});
