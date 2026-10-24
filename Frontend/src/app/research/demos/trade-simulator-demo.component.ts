import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { TradeSimDrawerComponent } from '../../components/edge/drawers/trade-sim-drawer.component';
import { EdgeMockDataService } from '../../components/edge/services/edge-mock-data.service';

/** The trade simulator as a page: sample data, labelled by the simulator itself. */
@Component({
  selector: 'app-trade-simulator-demo',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TradeSimDrawerComponent],
  template: `<app-trade-sim-drawer [data]="data" [inline]="true" />`,
})
export class TradeSimulatorDemoComponent {
  protected readonly data = inject(EdgeMockDataService).get();
}
