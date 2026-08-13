import { ChangeDetectionStrategy, Component } from '@angular/core';

/** Shared inner-spacing boundary for standard page bodies. */
@Component({
  selector: 'app-page-body',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '<ng-content />',
  // The existing utility remains the one spacing authority until the
  // full page-body migration replaces every page-local use in S9.
  host: { class: 'page-inset' },
})
export class PageBodyComponent {}
