import { ChangeDetectionStrategy, Component, ElementRef, computed, inject, linkedSignal } from '@angular/core';

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
 * unread lane, and a fleet whose feed is connected shows nothing. Each down
 * lane keeps its own reason and start, so two outages never borrow each
 * other's evidence. */
export function ibkrFeedPill(
  lanes: readonly LaneDescriptor[],
  states: ReadonlyMap<string, LaneFeedState>,
): IbkrFeedPill | null {
  const nameOf = (lane: LaneDescriptor): string => laneDisplayNameText(laneDisplayName(lane, lanes));
  const down = lanes.flatMap((lane) => {
    const state = states.get(lane.clerk_id);
    if (state?.kind !== 'disconnected') return [];
    const since = state.sinceMs === null
      ? ''
      : `since ${formatTimestampDisplay(state.sinceMs, { granularity: 'chart' })}`;
    const facts = [state.reason, since].filter((fact) => fact !== '').join(', ');
    return [facts === '' ? nameOf(lane) : `${nameOf(lane)} (${facts})`];
  });
  if (down.length > 0) {
    return {
      tone: 'is-down',
      text: 'IBKR down',
      detail:
        `IBKR market data is disconnected on ${down.join(' and ')}. Log in to IB Gateway. `
        + 'New entries and deploys are refused until it reconnects; the hold then lifts by itself.',
    };
  }
  const unread = lanes.filter((lane) => states.get(lane.clerk_id)?.kind === 'unknown');
  if (unread.length > 0) {
    return {
      tone: 'is-unknown',
      text: 'IBKR unknown',
      detail: `IBKR market-data status could not be read for ${unread.map(nameOf).join(' and ')}.`,
    };
  }
  return null;
}

/**
 * The shell's IBKR market-data pill: absent while every lane's feed is
 * connected, red while any lane's is not. Every Clerk reads its bars
 * through IB Gateway, so a logged-out Gateway blocks every deploy at once —
 * the pill says so where the owner looks first. Clicking or tapping it opens
 * the lanes, the reason and the one fix; hover shows the same text.
 */
@Component({
  selector: 'app-ibkr-feed-pill',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:mousedown)': 'onDocumentMousedown($event)',
    '(keydown.escape)': 'open.set(false)',
  },
  styles: [`
    :host { position: relative; display: inline-flex; }
    .pill {
      display: inline-flex; align-items: center;
      min-height: 30px; padding: 0 0.6rem;
      border-radius: var(--radius-pill); border: 1px solid; cursor: pointer;
      font: inherit; font-size: var(--fs-xs); font-weight: 700; line-height: 1.2; white-space: nowrap;
    }
    .pill:focus-visible { outline: 2px solid var(--p-primary-color, #6da2ff); outline-offset: 2px; }
    .pill.is-down { color: var(--lane-on-fill); border-color: #ff8b88; background: var(--lane-live-fill); }
    .pill.is-unknown {
      color: #d7d9de; border-color: rgba(178, 181, 190, 0.45);
      background: rgba(5, 8, 14, 0.42); font-weight: 500;
    }
    .panel {
      position: absolute; top: calc(100% + 6px); left: 0; z-index: 60;
      width: max-content; max-width: min(340px, calc(100vw - 24px)); margin: 0; padding: 0.55rem 0.65rem;
      border-radius: 8px; border: 1px solid rgba(178, 181, 190, 0.45);
      background: var(--panel-bg, #10141c); color: var(--text-primary);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
      font-size: var(--fs-xs); line-height: 1.35; white-space: normal;
    }
  `],
  template: `
    @if (pill(); as p) {
      <span role="status">
        <button
          type="button"
          class="pill"
          [class]="p.tone"
          [title]="p.detail"
          [attr.aria-expanded]="open()"
          aria-controls="ibkr-feed-detail"
          (click)="open.set(!open())"
        >{{ p.text }}</button>
      </span>
      @if (open()) {
        <p id="ibkr-feed-detail" class="panel">{{ p.detail }}</p>
      }
    }
  `,
})
export class IbkrFeedPillComponent {
  private readonly feed = inject(IbkrFeedService);
  private readonly directory = inject(FleetDirectoryService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  protected readonly pill = computed(() =>
    ibkrFeedPill(this.directory.lanesOf('alpaca'), this.feed.stateByClerkId()),
  );

  /** Closed again whenever the pill changes state, so a new outage never
   * opens on its own. */
  protected readonly open = linkedSignal({ source: () => this.pill()?.tone ?? null, computation: () => false });

  /** A press anywhere outside the pill closes its detail; mousedown, as the
   * bell does, so one press cannot both close it and toggle it back open. */
  protected onDocumentMousedown(event: MouseEvent): void {
    if (!this.open()) return;
    if (event.target instanceof Node && this.host.nativeElement.contains(event.target)) return;
    this.open.set(false);
  }
}
