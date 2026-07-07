import { provideZonelessChangeDetection } from '@angular/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BotEventPage } from '../../../../../api/live-runs.types';
import { LiveRunsService } from '../../../../../services/live-runs.service';
import { BotEventStreamComponent } from './bot-event-stream.component';

const PAGE: BotEventPage = {
  next_seq: null,
  rows: [
    {
      schema_version: 1,
      seq: 7,
      ts_ms: 1_700_000_000_000,
      event_type: 'order_rejected',
      source_authority: 'broker_session',
      identity: {
        evaluation_id: 'eval-1',
        intent_id: 'intent-1',
        order_ref: 'learn-ai/bot-a/v1:intent-1',
        req_id: 42,
        order_id: 100,
        perm_id: 200,
        exec_id: null,
      },
      severity: 'critical',
      headline: 'IBKR rejected the order',
      narrative: 'Order rejected - insufficient buying power',
      gate_steps: [
        {
          evaluation_id: 'eval-1',
          gate_id: 'broker.place_order',
          gate_result: 'block',
          source_authority: 'broker_session',
          facts: { reason_code: 'INSUFFICIENT_BUYING_POWER' },
        },
      ],
      terminal_error: {
        code: 'order_rejected',
        source: 'ibkr',
        gate_id: 'broker.place_order',
        message: 'IBKR order rejected',
        detail: null,
        external_code: 201,
        external_message: 'Order rejected - insufficient buying power',
        cause_chain: [],
        forensic_facts: { external_code: 201, reason_code: 'INSUFFICIENT_BUYING_POWER' },
      },
      facts: { raw_event_types: ['order_rejected'], order_ref: 'learn-ai/bot-a/v1:intent-1' },
    },
  ],
};

describe('BotEventStreamComponent', () => {
  it('loads and renders authored bot event rows', async () => {
    const service = fakeService(PAGE);

    await render(BotEventStreamComponent, {
      inputs: { runId: 'run-1', refreshKey: 1 },
      providers: [
        provideZonelessChangeDetection(),
        { provide: LiveRunsService, useValue: service },
      ],
    });

    await screen.findByText('IBKR rejected the order');
    expect(screen.getByText('Order rejected - insufficient buying power')).toBeTruthy();
    expect(screen.getByText('Order Rejected')).toBeTruthy();
    expect(screen.getByText('Broker Session')).toBeTruthy();
    expect(service.getBotEvents).toHaveBeenCalledWith('run-1', { limit: 100 });
  });

  it('expands gate and terminal evidence', async () => {
    await render(BotEventStreamComponent, {
      inputs: { runId: 'run-1', refreshKey: 1 },
      providers: [
        provideZonelessChangeDetection(),
        { provide: LiveRunsService, useValue: fakeService(PAGE) },
      ],
    });
    await screen.findByText('IBKR rejected the order');

    fireEvent.click(screen.getByRole('button', { name: 'Toggle bot event row 7' }));

    await waitFor(() => {
      expect(screen.getAllByText('Broker Place Order').length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText('Insufficient Buying Power').length).toBeGreaterThan(0);
    expect(screen.getAllByText('learn-ai/bot-a/v1:intent-1').length).toBeGreaterThan(0);
    expect(screen.getAllByText('201').length).toBeGreaterThan(0);
  });

  it('renders empty state without a run id', async () => {
    await render(BotEventStreamComponent, {
      inputs: { runId: null },
      providers: [
        provideZonelessChangeDetection(),
        { provide: LiveRunsService, useValue: fakeService(PAGE) },
      ],
    });

    await screen.findByTestId('bot-event-stream-empty');
    expect(screen.getByText('No bot events yet for this run.')).toBeTruthy();
  });
});

function fakeService(page: BotEventPage): Pick<LiveRunsService, 'getBotEvents'> {
  return {
    getBotEvents: vi.fn().mockResolvedValue(page),
  };
}
