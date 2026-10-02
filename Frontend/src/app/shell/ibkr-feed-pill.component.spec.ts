import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane } from '../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../fleet/fleet-directory.types';
import { IbkrFeedService, type LaneFeedState } from '../services/ibkr-feed.service';
import { IbkrFeedPillComponent } from './ibkr-feed-pill.component';

const PAPER = testLane({ clerk_id: 'clrk_paper', display_label: 'Paper' });
const LIVE = testLane({ clerk_id: 'clrk_live', display_label: 'Live' });
const DOWN: LaneFeedState = { kind: 'disconnected', reason: 'IBKR connection lost', sinceMs: 1_790_916_315_993 };

async function renderPill(states: Record<string, LaneFeedState>, lanes: LaneDescriptor[] = [PAPER, LIVE]) {
  return render(IbkrFeedPillComponent, {
    providers: [
      provideFleetDirectory({ observed_at_ms: 1_790_000_000_000, clerks: lanes }),
      { provide: IbkrFeedService, useValue: { stateByClerkId: signal(new Map(Object.entries(states))) } },
    ],
  });
}

describe('IbkrFeedPillComponent', () => {
  it('shows nothing while every lane’s feed is connected', async () => {
    const { container } = await renderPill({ clrk_paper: { kind: 'connected' }, clrk_live: { kind: 'connected' } });

    expect(container.textContent?.trim()).toBe('');
  });

  it('names the outage, the lanes it blocks, and the one fix when IB Gateway is logged out', async () => {
    const { container } = await renderPill({ clrk_paper: DOWN, clrk_live: DOWN });

    const pill = screen.getByRole('status');
    expect(pill.textContent?.trim()).toBe('IBKR down');
    const detail = pill.getAttribute('aria-label') ?? '';
    expect(detail).toContain('IBKR market data is disconnected on Paper and Live (IBKR connection lost), since ');
    expect(detail).toContain('Log in to IB Gateway.');
    expect(pill.getAttribute('title')).toBe(detail);
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it('names only the lane that is down, without a start time it does not have', async () => {
    await renderPill({ clrk_paper: { kind: 'connected' }, clrk_live: { ...DOWN, sinceMs: null } });

    const detail = screen.getByRole('status').getAttribute('aria-label') ?? '';
    expect(detail).toContain('disconnected on Live (IBKR connection lost). Log in');
    expect(detail).not.toContain('since');
  });

  it('says a lane it could not read is unknown rather than staying silent', async () => {
    await renderPill({ clrk_paper: { kind: 'connected' }, clrk_live: { kind: 'unknown' } });

    const pill = screen.getByRole('status');
    expect(pill.textContent?.trim()).toBe('IBKR unknown');
    expect(pill.getAttribute('aria-label')).toBe('IBKR market-data status could not be read for Live.');
  });

  it('lets an outage outrank an unread lane', async () => {
    await renderPill({ clrk_paper: DOWN, clrk_live: { kind: 'unknown' } });

    expect(screen.getByRole('status').textContent?.trim()).toBe('IBKR down');
  });
});
