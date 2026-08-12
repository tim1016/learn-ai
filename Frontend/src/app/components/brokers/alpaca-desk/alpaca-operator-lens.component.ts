import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';

/** Seam owned by the diagnostic and repair-focused Operator lens. */
@Component({
  selector: 'app-alpaca-operator-lens',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section id="alpaca-operator-panel" role="tabpanel" aria-labelledby="alpaca-operator-tab">
      <h2>Operator desk</h2>
      @if (status.isLoading()) {
        <p role="status">Loading operator data…</p>
      } @else if (status.error()) {
        <p role="alert">Operator data is unavailable right now.</p>
      } @else {
        <p>Operator data is ready.</p>
      }
    </section>
  `,
})
export class AlpacaOperatorLensComponent {
  protected readonly status = inject(AlpacaOperatorLensDataService).status;
}
