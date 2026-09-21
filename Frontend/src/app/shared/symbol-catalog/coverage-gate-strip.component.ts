import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../pipes/receipt-label.pipe';
import type { CoverageGateState } from './ensure-coverage.service';

/**
 * The picker's gate strip: the one rendering of "this pick is being covered"
 * and "this pick could not be covered". Pure presentation — the state comes
 * from a coverage session and the buttons just report intent.
 *
 * A `view_not_backfillable` failure offers no Retry: retrying cannot derive
 * the view; the operator must switch the picker's adjustment mode.
 */
@Component({
  selector: 'app-coverage-gate-strip',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="gate" role="status" aria-live="polite">
      @if (state().phase === 'backfilling') {
        <span class="gate__msg">
          {{ state().symbol }} is not in the lake — backfilling trade bars
          before it can be selected…
        </span>
        @if (state().percent !== null) {
          <span class="gate__percent mono">{{ state().percent }}%</span>
        }
        <button type="button" class="gate__btn" (click)="cancelled.emit()">
          Cancel
        </button>
      } @else {
        <span class="gate__msg">
          <span class="mono">{{ state().reason | receiptLabel }}</span> —
          {{ state().message }}
        </span>
        @if (state().reason !== 'view_not_backfillable') {
          <button type="button" class="gate__btn" (click)="retry.emit()">
            Retry
          </button>
        }
        <button type="button" class="gate__btn" (click)="dismiss.emit()">
          Dismiss
        </button>
      }
    </div>
  `,
  styles: `
    .gate {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 8px 10px;
      font-size: 11px;
      color: var(--text-subtle);
      border-top: 1px solid var(--border-light);
    }

    .gate__msg {
      flex: 1;
      min-width: 0;
    }

    .gate__percent {
      font-size: 10px;
      color: var(--text-secondary);
    }

    .gate__btn {
      margin-left: 4px;
      padding: 0;
      background: none;
      border: none;
      font: inherit;
      color: var(--accent);
      cursor: pointer;

      &:hover {
        color: var(--accent-hover);
        text-decoration: underline;
      }
    }
  `,
})
export class CoverageGateStripComponent {
  readonly state = input.required<CoverageGateState>();

  readonly retry = output();
  readonly cancelled = output();
  readonly dismiss = output();
}
