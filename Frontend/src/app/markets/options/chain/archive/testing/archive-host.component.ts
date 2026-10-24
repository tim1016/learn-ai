import { ChangeDetectionStrategy, Component, signal } from '@angular/core';

import { ArchiveChainComponent } from '../archive-chain.component';

/**
 * Stands in for the address: it holds what the address would hold and answers the archive chain's picks the way
 * the chain page does, so a spec drives the chain by its controls and sees the inputs it would then be given.
 */
@Component({
  selector: 'app-archive-chain-host',
  imports: [ArchiveChainComponent],
  template: `
    <app-archive-chain
      [symbol]="symbol()"
      [date]="date()"
      [atMs]="atMs()"
      [expiryMs]="expiryMs()"
      (datePicked)="onDate($event)"
      (timePicked)="atMs.set($event)"
      (expiryPicked)="expiryMs.set($event)"
    />
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ArchiveHostComponent {
  readonly symbol = signal('SPY');
  readonly date = signal<string | null>(null);
  readonly atMs = signal<number | null>(null);
  readonly expiryMs = signal<number | null>(null);

  protected onDate(picked: { readonly date: string; readonly atMs: number | null }): void {
    this.date.set(picked.date);
    this.atMs.set(picked.atMs);
  }
}
