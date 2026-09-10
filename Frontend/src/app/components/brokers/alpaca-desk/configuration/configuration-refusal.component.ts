import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import type { ConfigurationRefusal } from './broker-configuration-refusal';

/**
 * One refusal from the configuration surface, rendered the way the contract
 * splits it: the code-like `reason` through `receiptLabel`, and the
 * backend-authored `message` / `next_step` verbatim. This component composes
 * no prose of its own — if the words read oddly, they are the server's words
 * and the fix belongs there.
 *
 * A stale write (`revision_conflict`, `selection_generation_conflict`) is the
 * one case with an affordance: nothing was overwritten, so the way forward is
 * to reload and look at what the other tab left, never to retry the write.
 */
@Component({
  selector: 'app-configuration-refusal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  template: `
    @let detail = refusal();
    <div class="refusal" role="alert">
      @if (detail.reason; as reason) {
        <p class="refusal__reason">{{ reason | receiptLabel }}</p>
      }
      <p class="refusal__message">{{ detail.message }}</p>
      @if (detail.nextStep; as nextStep) {
        <p class="refusal__next-step">{{ nextStep }}</p>
      }
      @if (detail.stale) {
        <button type="button" class="refusal__reload" (click)="reloadRequested.emit()">
          Reload configuration
        </button>
      }
    </div>
  `,
  styleUrl: './configuration-refusal.component.scss',
  host: { class: 'block' },
})
export class ConfigurationRefusalComponent {
  readonly refusal = input.required<ConfigurationRefusal>();
  readonly reloadRequested = output();
}
