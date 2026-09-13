import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import {
  TickerRangePickerComponent,
  type TickerRange,
} from '../../../../shared/ticker-range-picker';

/**
 * Edit-scope drawer of Data Lab Explore (PRD §7.3). Presentational shell
 * around the shared ticker/range picker — string dates live in the picker
 * state and convert to int64 ms at the parent's apply boundary.
 */
@Component({
  selector: 'app-explore-scope-drawer',
  imports: [TickerRangePickerComponent],
  templateUrl: './explore-scope-drawer.component.html',
  styleUrl: './explore-scope-drawer.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExploreScopeDrawerComponent {
  readonly open = input(false);
  readonly range = input<TickerRange>({
    symbol: '',
    from: '',
    to: '',
    resolution: 'minute',
    autoFetch: false,
  });

  readonly openChange = output<boolean>();
  readonly rangeChange = output<TickerRange>();
  readonly apply = output();
}
