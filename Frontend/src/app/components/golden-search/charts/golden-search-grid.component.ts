import { ChangeDetectionStrategy, Component, ViewEncapsulation } from '@angular/core';

/**
 * The 12-column page grid of the Golden Search steps (#2821). A child takes
 * `data-span` of 12, 8, 6, 4 or 3 columns (12 when unset). The spans follow
 * the grid's own width, not the window's: below 1100px the 3- and 4-column
 * spans take half the row and the 8-column span the whole row, and below
 * 720px every child takes the whole row. Unencapsulated so the rules reach
 * projected children; every selector is scoped to this element.
 */
@Component({
  selector: 'app-golden-search-grid',
  template: '<ng-content />',
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
  styles: `
    app-golden-search-grid { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: var(--space-4); align-items: start; container: gs-grid / inline-size; }
    app-golden-search-grid > * { grid-column: span 12; min-width: 0; }
    app-golden-search-grid > [data-span='8'] { grid-column: span 8; }
    app-golden-search-grid > [data-span='6'] { grid-column: span 6; }
    app-golden-search-grid > [data-span='4'] { grid-column: span 4; }
    app-golden-search-grid > [data-span='3'] { grid-column: span 3; }
    @container gs-grid (max-width: 1100px) {
      app-golden-search-grid > [data-span='8'] { grid-column: span 12; }
      app-golden-search-grid > [data-span='4'], app-golden-search-grid > [data-span='3'] { grid-column: span 6; }
    }
    @container gs-grid (max-width: 720px) {
      app-golden-search-grid > [data-span] { grid-column: span 12; }
    }
  `,
})
export class GoldenSearchGridComponent {}
