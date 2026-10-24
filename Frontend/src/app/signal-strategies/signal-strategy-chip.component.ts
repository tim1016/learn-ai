import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { ChipTone } from './signal-strategy-evidence';

/**
 * One word of state in a tone: a proof state, a version's status, a world.
 * The words carry the state; the tone only helps the eye, so a chip without a
 * tone is plain information.
 */
@Component({
  selector: 'app-signal-strategy-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="chip" [attr.data-tone]="tone()"><ng-content /></span>`,
  styles: `
    :host { display: inline-flex; min-width: 0; }

    .chip {
      padding: 0 var(--space-2);
      border: 1px solid var(--badge-neutral-border);
      border-radius: var(--radius-pill);
      background: var(--badge-neutral-bg);
      color: var(--badge-neutral-text);
      font-size: var(--fs-xs);
      line-height: 1.6;
      white-space: nowrap;
    }

    .chip[data-tone='ready'] { border-color: var(--verdict-ready); background: var(--verdict-ready-soft); color: var(--text-primary); }
    .chip[data-tone='degraded'] { border-color: var(--verdict-degraded); background: var(--verdict-degraded-soft); color: var(--text-primary); }
    .chip[data-tone='blocked'] { border-color: var(--verdict-blocked); background: var(--verdict-blocked-soft); color: var(--text-primary); }
    .chip[data-tone='unknown'] { border-color: var(--verdict-unknown); background: var(--verdict-unknown-soft); color: var(--text-primary); }
  `,
})
export class SignalStrategyChipComponent {
  readonly tone = input<ChipTone | null>(null);
}
