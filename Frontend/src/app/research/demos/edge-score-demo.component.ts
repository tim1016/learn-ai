import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { EdgeScoreDrawerComponent } from '../../components/edge/drawers/edge-score-drawer.component';
import { EdgeMockDataService } from '../../components/edge/services/edge-mock-data.service';

/** The Edge score inspector as a page: sample data, labelled by the inspector itself. */
@Component({
  selector: 'app-edge-score-demo',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [EdgeScoreDrawerComponent],
  template: `<app-edge-score-drawer [data]="data" [inline]="true" />`,
})
export class EdgeScoreDemoComponent {
  protected readonly data = inject(EdgeMockDataService).get();
}
