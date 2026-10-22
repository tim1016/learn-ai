import { ChangeDetectionStrategy, Component, input, output, signal } from '@angular/core';

/** The Clerk's reason code for a broker order it could not record (#2363). */
export const UNFOLDABLE_BROKER_ORDER_REASON_CODE = 'UNFOLDABLE_BROKER_ORDER';

export interface BrokerOrderAcknowledgementRequest {
  readonly brokerOrderId: string;
  readonly operator: string;
  readonly expectedObservationSequence?: number;
}

/**
 * Operator acknowledgement of each named broker order.
 *
 * The episode's evidence refs are exactly the broker order ids the external-order
 * acknowledgement route accepts; each is an opaque audit token and renders verbatim.
 * Acknowledging one releases only that order from the account's entry pause.
 */
@Component({
  selector: 'app-broker-order-review',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './broker-order-review.component.scss',
  template: `
    <div class="broker-order-review">
      <label>
        Reviewed by
        <input
          type="text"
          maxlength="64"
          autocomplete="off"
          [value]="operator()"
          (input)="updateOperator($event)"
        />
      </label>
      <ul aria-label="Broker orders awaiting review">
        @for (brokerOrderId of brokerOrderIds(); track brokerOrderId) {
          <li>
            <code>{{ brokerOrderId }}</code>
            <button
              type="button"
              [disabled]="busyOrderId() !== null || operator().trim() === ''"
              [attr.aria-label]="'Acknowledge broker order ' + brokerOrderId"
              (click)="acknowledgeOrder(brokerOrderId)"
            >{{ busyOrderId() === brokerOrderId ? 'Acknowledging…' : 'Acknowledge' }}</button>
          </li>
        }
      </ul>
    </div>
  `,
})
export class BrokerOrderReviewComponent {
  readonly brokerOrderIds = input.required<readonly string[]>();
  readonly busyOrderId = input<string | null>(null);
  readonly acknowledge = output<BrokerOrderAcknowledgementRequest>();
  protected readonly operator = signal('');

  protected updateOperator(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.operator.set(event.target.value);
  }

  protected acknowledgeOrder(brokerOrderId: string): void {
    const operator = this.operator().trim();
    if (operator === '') return;
    this.acknowledge.emit({ brokerOrderId, operator });
  }
}
