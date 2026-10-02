import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import { laneDisplayName, laneDisplayNameText, type LaneDescriptor } from '../fleet/fleet-directory.types';
import { IbkrFeedService, type LaneFeedState } from '../services/ibkr-feed.service';
import { formatTimestampDisplay } from '../shared/timestamp';

/** What the pill says. The words carry the meaning, never the colour alone (WCAG 1.4.1). */
export interface IbkrFeedPill {
  readonly tone: 'is-down' | 'is-unknown';
  readonly text: string;
  readonly detail: string;
}

/** Fold every lane's feed state into at most one pill: an outage outranks an
 * unread lane, and a fleet whose feed is connected shows nothing. */
export function ibkrFeedPill(
  lanes: readonly LaneDescriptor[],
  states: ReadonlyMap<string, LaneFeedState>,
): IbkrFeedPill | null {
  const names = (picked: readonly LaneDescriptor[]): string =>
    picked.map((lane) => laneDisplayNameText(laneDisplayName(lane, lanes))).join(' and ');
  const down = lanes.flatMap((lane) => {
    const state = states.get(lane.clerk_id);
    return state?.kind === 'disconnected' ? [{ lane, state }] : [];
  });
  if (down.length > 0) {
    const reason = down[0].state.reason;
    const starts = down.flatMap(({ state }) => (state.sinceMs === null ? [] : [state.sinceMs]));
    const since = starts.length > 0
      ? `, since ${formatTimestampDisplay(Math.min(...starts), { granularity: 'chart' })}`
      : '';
    return {
      tone: 'is-down',
      text: 'IBKR down',
      detail:
        `IBKR market data is disconnected on ${names(down.map(({ lane }) => lane))}`
        + `${reason ? ` (${reason})` : ''}${since}. Log in to IB Gateway. `
        + 'New entries and deploys are refused until it reconnects; the hold then lifts by itself.',
    };
  }
  const unread = lanes.filter((lane) => states.get(lane.clerk_id)?.kind === 'unknown');
  if (unread.length > 0) {
    return {
      tone: 'is-unknown',
      text: 'IBKR unknown',
      detail: `IBKR market-data status could not be read for ${names(unread)}.`,
    };
  }
  return null;
}

/**
 * The shell's IBKR market-data pill: absent while every lane's feed is
 * connected, red while any lane's is not. Every Clerk reads its bars
 * through IB Gateway, so a logged-out Gateway blocks every deploy at once —
 * the pill says so where the owner looks first, with the one fix.
 */
@Component({
  selector: 'app-ibkr-feed-pill',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: [`
    :host { display: inline-flex; }
    .pill {
      display: inline-flex; align-items: center;
      min-height: 30px; padding: 0 0.6rem;
      border-radius: var(--radius-pill); border: 1px solid;
      font-size: var(--fs-xs); font-weight: 700; line-height: 1.2; white-space: nowrap;
      cursor: help;
    }
    .pill.is-down { color: var(--lane-on-fill); border-color: #ff8b88; background: var(--lane-live-fill); }
    .pill.is-unknown {
      color: #d7d9de; border-color: rgba(178, 181, 190, 0.45);
      background: rgba(5, 8, 14, 0.42); font-weight: 500;
    }
  `],
  template: `
    @if (pill(); as p) {
      <span class="pill" [class]="p.tone" role="status" [attr.aria-label]="p.detail" [title]="p.detail">
        {{ p.text }}
      </span>
    }
  `,
})
export class IbkrFeedPillComponent {
  private readonly feed = inject(IbkrFeedService);
  private readonly directory = inject(FleetDirectoryService);

  protected readonly pill = computed(() =>
    ibkrFeedPill(this.directory.lanesOf('alpaca'), this.feed.stateByClerkId()),
  );
}
