import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService } from '../../../../services/alpaca-live-verdict.service';
import { LaneContextStripComponent } from './lane-context-strip.component';

// The pill (`AlpacaLiveBannerComponent`) reads verdict state from
// `AlpacaLiveVerdictService`; stub it so every case renders the same
// undetermined-but-present pill and the test stays focused on the strip's
// own markup (the authority fact) rather than the pill's own rendering,
// which `alpaca-live-banner.component.spec.ts` already covers.
const stubVerdictService = { provide: AlpacaLiveVerdictService, useValue: { stateFor: () => ({ verdict: null, lastError: null }) } };

async function renderStrip(lane: LaneDescriptor | null, siblingLanes: readonly LaneDescriptor[] = []) {
  return render(LaneContextStripComponent, {
    inputs: { lane },
    providers: [
      stubVerdictService,
      // The pill mounted inside the strip injects `FleetDirectoryService`
      // itself now (Major 2 fix, #2182 follow-up) — an explicit, empty-by-
      // default directory here so a test that doesn't care about collisions
      // never accidentally gets one from the fixture's own default lane.
      provideFleetDirectory({ observed_at_ms: 1, clerks: [...siblingLanes] }),
    ],
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

  it("disambiguates a nickname shared with another lane on this page too, matching the shell header (#2182 Major 2)", async () => {
    // Regression coverage: the pill used to read its siblings from an
    // `allLanes` prop that nothing on the Bots/Gallery pages ever passed
    // through the strip, so two same-nicknamed lanes rendered and announced
    // identically here even though the shell header, one row up, correctly
    // disambiguated. The pill now injects `FleetDirectoryService` itself, so
    // this needs no prop from the strip or its callers.
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });

    await renderStrip(paper, [paper, live]);

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Strategy lab');
    expect(status.textContent).toContain('(Paper)');
    expect(status.getAttribute('aria-label')).toContain('Strategy lab (Paper)');
  });
});
