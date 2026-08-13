import { ChangeDetectionStrategy, Component } from '@angular/core';

/** Shared inner-spacing boundary for standard page bodies. */
@Component({
  selector: 'app-page-body',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '<ng-content />',
  styleUrl: './page-body.component.scss',
  host: { class: 'page-body' },
})
export class PageBodyComponent {}
