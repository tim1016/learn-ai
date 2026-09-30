import { ChangeDetectionStrategy, Component, booleanAttribute, computed, input } from '@angular/core';

import { ReceiptLabelPipe, formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import type { ActionRejection } from '../v2-panel/lib/panel-action-outcome';

/**
 * A refused end in the backend's words (#2607), drawn one way on Deploy and
 * on the bot's page: what went wrong, why, what to do next, and its code as
 * a receipt label — as `deriveActionRejection` reads them from whichever
 * refusal arrived. The fleet's `next_step` is both its `why` and its next
 * step, so it is said once; a code the fallback already said as `why` is
 * not said twice. `announce` makes it an alert where nothing else says it.
 */
@Component({
  selector: 'app-bot-end-refusal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  host: { '[attr.role]': 'announce() ? "alert" : null' },
  template: `
    <p><strong>{{ refusal().message }}</strong></p>
    @if (why(); as why) { <p>{{ why }}</p> }
    @if (refusal().nextAction; as next) { <p>Next: {{ next }}</p> }
    @if (code(); as code) { <p class="bot-end-refusal__code">{{ code | receiptLabel }}</p> }
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-1);
      padding: var(--space-3);
      border-radius: var(--radius-md);
      background: color-mix(in srgb, var(--bear) 12%, var(--bg-surface));
      color: var(--text-primary);
      font-size: var(--fs-xs);
      line-height: 1.45;
    }

    p { margin: 0; }
    strong { color: var(--bear); font-weight: var(--fw-semi); }
    .bot-end-refusal__code { color: var(--text-subtle); }
  `,
})
export class BotEndRefusalComponent {
  readonly refusal = input.required<ActionRejection>();
  readonly announce = input(false, { transform: booleanAttribute });

  protected readonly why = computed(() => {
    const { why, nextAction } = this.refusal();
    return why === nextAction ? null : why;
  });

  protected readonly code = computed(() => {
    const { reasonCode, why } = this.refusal();
    return reasonCode !== null && why !== formatReceiptLabel(reasonCode) ? reasonCode : null;
  });
}
