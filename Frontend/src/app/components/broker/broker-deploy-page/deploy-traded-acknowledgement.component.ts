import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { components } from '../../../api/broker.types';

export type TradedAssetWarning = components['schemas']['UntestedTradedAssetWarning'];

/**
 * The owner's written acknowledgement of an untested traded asset (#2912),
 * above the Deploy button.
 *
 * Its own block, apart from the evidence-only override: that one accepts a
 * strategy whose proof is incomplete, this one a bot that trades something
 * the strategy was never tested on. Every sentence is the backend's. A Dry
 * Run's warning carries no sentence to accept, so it shows the warning alone.
 */
@Component({
  selector: 'app-deploy-traded-acknowledgement',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './deploy-traded-acknowledgement.component.html',
  styleUrl: './deploy-traded-acknowledgement.component.scss',
})
export class DeployTradedAcknowledgementComponent {
  readonly warning = input.required<TradedAssetWarning>();
  readonly acknowledged = input(false);
  readonly reason = input('');
  readonly reasonError = input<string | null>(null);

  readonly acknowledgedChange = output<boolean>();
  readonly reasonChange = output<string>();
  readonly reasonBlur = output();

  protected changeAcknowledgement(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.acknowledgedChange.emit(event.target.checked);
    }
  }

  protected changeReason(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) {
      this.reasonChange.emit(event.target.value);
    }
  }
}
