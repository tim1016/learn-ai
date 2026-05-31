import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import type { OperationError } from '../operation-error';

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
  template: `
    @if (error(); as e) {
      <div class="op-result" [class]="'cat-' + e.category" role="alert">
        <p class="op-title">{{ e.title }}</p>
        @if (e.detail) {
          <p class="op-detail">{{ e.detail }}</p>
        }
        <p class="op-remediation">{{ e.remediation }}</p>
      </div>
    }
  `,
  styleUrl: './broker-operation-result.component.scss',
})
export class BrokerOperationResultComponent {
  readonly error = input<OperationError | null>(null);
}
