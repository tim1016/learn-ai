import { ChangeDetectionStrategy, Component, inject, input, output } from '@angular/core';

import { ProviderBoundsService } from '../../../../shared/date-picker';

import {
  TickerRangePickerComponent,
  type TickerRange,
} from '../../../../shared/ticker-range-picker';

/**
 * Edit-scope drawer of Data Lab Explore. Presentational shell
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

  /**
   * Data Lab charts the session still forming and takes any calendar day, so
   * its dates run from the provider's oldest day with no last day, and need
   * not be sessions.
   */
  protected readonly dateBounds = inject(ProviderBoundsService).openEnded;

  readonly openChange = output<boolean>();
  readonly rangeChange = output<TickerRange>();
  readonly apply = output();
}
