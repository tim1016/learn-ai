import {
  ChangeDetectionStrategy,
  Component,
  input,
} from '@angular/core';

import type { BotPanelView } from '../lib/broker-v2-panel.types';

/** The one visual verdict token shared by the purpose-built bot-detail banners. */
@Component({
  selector: 'app-mission-verdict-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span
      class="mission-verdict-status"
      [class.mission-verdict-status--bare]="bare()"
      [attr.data-state]="verdict().state"
      [attr.aria-label]="verdict().label"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >{{ verdict().label }}</span>
  `,
  styleUrl: './mission-verdict-status.component.scss',
})
export class MissionVerdictStatusComponent {
  readonly verdict = input.required<BotPanelView['mission_verdict']>();
  /** Text-only, for hosts (the bot banner topline) that carry the state color themselves. */
  readonly bare = input(false);
}
