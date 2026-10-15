import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { OptionContractComponent } from '../../../../shared/option-contract/option-contract.component';
import type { Instrument } from '../session-rows';

/** The instrument a row is about: the contract in words when the server sent its parts, else its symbol as it came, else nothing. */
@Component({
  selector: 'app-options-backtest-instrument',
  imports: [AssetIdentityComponent, OptionContractComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (instrument().contract; as contract) {
      <app-option-contract [contract]="contract" />
    } @else if (instrument().symbol; as symbol) {
      <app-asset-identity [symbol]="symbol" size="xs" [logo]="false" />
    } @else {
      <span aria-hidden="true">—</span><span class="sr-only">none</span>
    }
  `,
  styles: ':host { display: inline; } .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }',
})
export class InstrumentComponent {
  readonly instrument = input.required<Instrument>();
}
