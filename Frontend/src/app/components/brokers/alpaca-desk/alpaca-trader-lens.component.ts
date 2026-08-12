import { ChangeDetectionStrategy, Component } from '@angular/core';

/** Seam owned by the outcomes-focused Trader lens. */
@Component({
  selector: 'app-alpaca-trader-lens',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section id="alpaca-trader-panel" role="tabpanel" aria-labelledby="alpaca-trader-tab">
      <h2>Trader desk</h2>
      <p>Your account outcomes and activity will appear here.</p>
    </section>
  `,
})
export class AlpacaTraderLensComponent {}
