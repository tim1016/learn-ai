import { signal } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
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

function pillButton(): HTMLElement {
  return screen.getByRole('button');
}

describe('IbkrFeedPillComponent', () => {
  it('shows nothing while every lane’s feed is connected', async () => {
    const { container } = await renderPill({ clrk_paper: { kind: 'connected' }, clrk_live: { kind: 'connected' } });

    expect(container.textContent?.trim()).toBe('');
  });

  it('opens the lanes, their reasons and the one fix when IB Gateway is logged out', async () => {
    const { container } = await renderPill({ clrk_paper: DOWN, clrk_live: DOWN });
    expect(screen.getByRole('status').textContent?.trim()).toBe('IBKR down');

    await fireEvent.click(pillButton());

    const detail = container.querySelector('#ibkr-feed-detail')?.textContent ?? '';
    expect(detail).toMatch(/^IBKR market data is disconnected on Paper \(IBKR connection lost, since .+\) and Live \(IBKR connection lost, since .+\)\. Log in to IB Gateway\./);
    expect(pillButton().getAttribute('aria-expanded')).toBe('true');
    expect(pillButton().getAttribute('title')).toBe(detail);
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it('keeps each lane’s own reason, and no start time it does not have', async () => {
    await renderPill({
      clrk_paper: { kind: 'disconnected', reason: 'IBKR connection lost', sinceMs: null },
      clrk_live: { kind: 'disconnected', reason: 'Client id already in use', sinceMs: null },
    });

    expect(pillButton().getAttribute('title')).toContain(
      'disconnected on Paper (IBKR connection lost) and Live (Client id already in use). Log in',
    );
  });

  it('closes its detail on Escape', async () => {
    const { container } = await renderPill({ clrk_paper: DOWN, clrk_live: { kind: 'connected' } });
    await fireEvent.click(pillButton());

    await fireEvent.keyDown(pillButton(), { key: 'Escape' });

    expect(container.querySelector('#ibkr-feed-detail')).toBeNull();
    expect(pillButton().getAttribute('aria-expanded')).toBe('false');
  });

  it('says a lane it could not read is unknown rather than staying silent', async () => {
    await renderPill({ clrk_paper: { kind: 'connected' }, clrk_live: { kind: 'unknown' } });

    expect(pillButton().textContent?.trim()).toBe('IBKR unknown');
    expect(pillButton().getAttribute('title')).toBe('IBKR market-data status could not be read for Live.');
  });

  it('lets an outage outrank an unread lane', async () => {
    await renderPill({ clrk_paper: DOWN, clrk_live: { kind: 'unknown' } });

    expect(pillButton().textContent?.trim()).toBe('IBKR down');
  });
});
