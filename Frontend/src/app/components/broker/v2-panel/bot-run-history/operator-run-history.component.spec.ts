import { provideRouter } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { FeedContinuityView } from '../lib/broker-v2-panel.types';
import { OperatorRunHistoryComponent } from './operator-run-history.component';

const NOT_RECORDED: FeedContinuityView = {
  provider_label: 'IBKR market data',
  state: 'not_recorded',
  state_label: 'Not recorded',
  explanation: 'No continuity evidence has been recorded for this run.',
  run_id: null,
  interruption_count: 0,
  recovery_count: 0,
  unresolved_count: 0,
  decision_impact_count: 0,
  last_interruption_at_ms: null,
  last_recovery_at_ms: null,
  latest_bar_at_ms: null,
  events: [],
};

describe('OperatorRunHistoryComponent', () => {
  it("opens History on this bot alone, with every one of its runs (owner decision 2026-09-29)", async () => {
    const { fixture } = await render(OperatorRunHistoryComponent, {
      inputs: {
        broker: 'alpaca',
        clerkId: 'clrk_spec',
        strategyInstanceId: 'spy-ema-1',
        botRunning: false,
        feedContinuity: NOT_RECORDED,
      },
      providers: [provideRouter([])],
    });
    const details = screen.getByText('Runs').closest('details') as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event('toggle'));
    fixture.detectChanges();

    expect(screen.getByRole('link', { name: 'History' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/history?account=clrk_spec&bot=spy-ema-1');
  });
});
