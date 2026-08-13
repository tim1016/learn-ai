import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/** Shared inner-spacing boundary for standard page bodies. */
@Component({
  selector: 'app-page-body',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '<ng-content />',
  styleUrl: './page-body.component.scss',
  host: {
    class: 'page-body',
    '[class.page-body--full-bleed]': 'fullBleed()',
  },
})
export class PageBodyComponent {
  /** Deliberate edge-to-edge workspace surface, declared by route data. */
  readonly fullBleed = input(false);
}
