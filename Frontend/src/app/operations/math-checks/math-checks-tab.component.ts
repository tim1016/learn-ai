import { ChangeDetectionStrategy, Component } from '@angular/core';

import { GoldenFixturesCatalogComponent } from './golden-fixtures-catalog.component';
import { SpyEmaFixtureRunComponent } from './spy-ema-fixture-run.component';

/**
 * Operations → Math checks: the numerical reference fixtures, and the explicit
 * fixture-run tool beside them.
 *
 * Opening the tab reads the fixture catalog and nothing else. The fixture run
 * persists a record, so it stays behind its own button.
 */
@Component({
  selector: 'app-operations-math-checks',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [GoldenFixturesCatalogComponent, SpyEmaFixtureRunComponent],
  template: `
    <app-spy-ema-fixture-run />
    <app-golden-fixtures-catalog />
  `,
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      gap: var(--space-5);
    }
  `,
})
export class MathChecksTabComponent {}
