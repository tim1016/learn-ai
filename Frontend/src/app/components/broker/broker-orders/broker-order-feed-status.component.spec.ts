import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import {
  BrokerOrderFeedStatusComponent,
  type OrderFeedStatus,
} from './broker-order-feed-status.component';

const STATUS: OrderFeedStatus = {
  broker: {
    state: 'ok',
    headline: 'Online',
    detail: 'Connected',
  },
  updates: {
    state: 'reconnecting',
    headline: 'Reconnecting',
    detail: 'Last receipt 12s ago',
  },
};

describe('BrokerOrderFeedStatusComponent', () => {
  it('keeps broker truth and live-update freshness as separate facts', async () => {
    await render(BrokerOrderFeedStatusComponent, { inputs: { status: STATUS } });

    expect(screen.getByText('Broker session')).toBeTruthy();
    expect(screen.getByText('Online')).toBeTruthy();
    expect(screen.getByText('Live order updates')).toBeTruthy();
    expect(screen.getByText('Reconnecting')).toBeTruthy();
    expect(screen.getByText('Last receipt 12s ago')).toBeTruthy();
  });
});

it.each([
  ['live', 'Live', 'state-ok'],
  ['reconnecting', 'Reconnecting', 'state-warn'],
  ['stale', 'Stale', 'state-warn'],
  ['offline_but_saved', 'Saved offline', 'state-warn'],
  ['rebuilding', 'Rebuilding', 'state-warn'],
  ['projection_unavailable', 'Projection unavailable', 'state-down'],
  ['corrupt', 'Projection corrupt', 'state-down'],
] as const)('renders backend-authored transaction feed state %s', async (state, headline, toneClass) => {
  await render(BrokerOrderFeedStatusComponent, {
    inputs: { status: { ...STATUS, updates: { state, headline, detail: 'Backend authored.' } } },
  });

  expect(screen.getByText(headline)).toBeTruthy();
  expect(screen.getByText('Live order updates').closest('.status-item')?.classList.contains(toneClass)).toBe(true);
});
