import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { ChannelState } from '../lib/broker-v2-panel.types';

/**
 * Tiny dot indicator for channel health (market_data, execution streams).
 * Green = healthy, amber = unknown, red = unhealthy.
 *
 * Decorative only: every host renders the channel's state as visible text
 * beside the dot, so the dot is hidden from assistive technology rather than
 * announcing the same state twice (status is never carried by colour alone).
 */
@Component({
  selector: 'app-channel-health-dot',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { 'aria-hidden': 'true' },
  template: `
    <span
      class="channel-dot"
      [class.channel-dot--healthy]="state() === 'healthy'"
      [class.channel-dot--unhealthy]="state() === 'unhealthy'"
      [class.channel-dot--unknown]="state() === 'unknown'"
    ></span>
  `,
  styles: `
    :host { display: inline-flex; }
    .channel-dot {
      display: inline-block;
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: var(--p-text-muted-color, #888);
    }
    .channel-dot--healthy { background: var(--p-green-500, #22c55e); }
    .channel-dot--unhealthy { background: var(--p-red-500, #ef4444); }
    .channel-dot--unknown { background: var(--p-yellow-500, #eab308); }
  `,
})
export class ChannelHealthDotComponent {
  readonly state = input<ChannelState>('unknown');
}
