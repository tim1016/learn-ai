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
  template: '<div class="gs-page-grid"><ng-content /></div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
  styleUrl: './golden-search-grid.component.scss',
})
export class GoldenSearchGridComponent {}
