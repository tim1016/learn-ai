import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { testLane } from '../../../../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService } from '../../../../services/alpaca-live-verdict.service';
import { LaneContextStripComponent } from './lane-context-strip.component';

// The pill (`AlpacaLiveBannerComponent`) reads verdict state from
// `AlpacaLiveVerdictService`; stub it so every case renders the same
// undetermined-but-present pill and the test stays focused on the strip's
// own markup (the authority fact) rather than the pill's own rendering,
// which `alpaca-live-banner.component.spec.ts` already covers.
const stubVerdictService = { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => ({ verdict: null, lastError: null }) } };

async function renderStrip(lane: LaneDescriptor | null) {
  return render(LaneContextStripComponent, {
    inputs: { lane },
    providers: [stubVerdictService],
  });
}

describe('LaneContextStripComponent', () => {
  it('renders the lane pill and its authority fact when the lane has an authority state', async () => {
    const lane = testLane({
      provider_summary: {
        provider_id: 'alpaca',
        adapter_version: 'alpaca-fleet.3',
        confirmed_account_id: 'acct_test',
        confirmed_binding_generation: 3,
        endpoint_mode: 'paper',
        authority_state: 'real_paper',
      },
    });

    await renderStrip(lane);

    expect(screen.getByRole('status')).toBeTruthy();
    expect(screen.getByText('Authority: Real Paper')).toBeTruthy();
  });

  it('renders the pill but no authority fact when the lane has no authority state', async () => {
    const lane = testLane({ provider_summary: null });

    await renderStrip(lane);

    expect(screen.getByRole('status')).toBeTruthy();
    expect(screen.queryByText(/Authority:/)).toBeNull();
  });

  it('renders nothing when the lane is null', async () => {
    const { container } = await renderStrip(null);

    expect(container.textContent).toBe('');
    expect(screen.queryByRole('status')).toBeNull();
  });
});
