import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import type { OperationError } from '../operation-error';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

/**
 * Inline result for a broker operation (handoff: surfacing is inline-only).
 * Renders the structured {category, title, detail, remediation} next to the
 * control that produced it — never a toast. The detail line is the backend's
 * literal message; the remediation is the "what to do next" the caller derived
 * from (operation, status).
 */
@Component({
  selector: 'app-broker-operation-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  template: `
    @if (error(); as e) {
      <div class="op-result" [class]="'cat-' + e.category" role="alert">
        <span class="sr-only">{{ alertText(e) }}</span>
        <div class="op-header" aria-hidden="true">
          <p class="op-title">{{ e.title }}</p>
          @if (e.status !== null) {
            <span class="op-status">HTTP {{ e.status }}</span>
          }
        </div>
        @if (e.reason_code || e.gate_id) {
          <dl class="op-contract" aria-hidden="true">
            @if (e.reason_code) {
              <div>
                <dt>Server reason</dt>
                <dd>{{ e.reason_code | receiptLabel }}</dd>
              </div>
            }
            @if (e.gate_id) {
              <div>
                <dt>Rejected at</dt>
                <dd>{{ e.gate_id | receiptLabel }}</dd>
              </div>
            }
          </dl>
        }
        @if (e.detail) {
          {{ ' ' }}
          <p class="op-detail" aria-hidden="true">{{ e.detail }}</p>
        }
        {{ ' ' }}
        <p class="op-remediation" aria-hidden="true"><strong>Next:</strong> {{ e.remediation }}</p>
      </div>
    }
  `,
  styleUrl: './broker-operation-result.component.scss',
})
export class BrokerOperationResultComponent {
  readonly error = input<OperationError | null>(null);

  alertText(error: OperationError): string {
    return [error.title, error.detail, error.remediation].filter(Boolean).join(' ');
  }
}
