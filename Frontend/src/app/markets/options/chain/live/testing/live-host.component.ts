import { ChangeDetectionStrategy, Component, signal } from '@angular/core';

import { LiveChainComponent } from '../live-chain.component';

/**
 * Stands in for the address: it holds the instrument and the expiry the address would hold and
 * answers the live chain's picks the way the chain page does, so a spec drives the chain by its
 * controls and sees the inputs it would then be given.
 */
@Component({
  selector: 'app-live-chain-host',
  imports: [LiveChainComponent],
  template: `
    <app-live-chain
      [symbol]="symbol()"
      [expiryMs]="expiryMs()"
      (expiryPicked)="expiryMs.set($event)"
      (expiryAdopted)="expiryMs.set($event)"
    />
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LiveHostComponent {
  readonly symbol = signal('SPY');
  readonly expiryMs = signal<number | null>(null);
}
