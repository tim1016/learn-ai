import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { IndicatorPickerComponent } from '../../../../shared/indicator-picker/indicator-picker.component';
import type { IndicatorPickerAdd } from '../../../../shared/indicator-picker/indicator-picker.component';
import { ChartSeriesColorPickerComponent } from '../../../../shared/trading-chart/chart-series-color-picker.component';
import type { ChartSeriesColorToken } from '../../../../shared/trading-chart/chart-series-color-tokens';
import type { IndicatorCategory } from '../../../../shared/indicator-catalog/indicator-catalog.service';

/** One active indicator chip in the collapsed "active" list. */
export interface ExploreChipEntry {
  instance: { id: string };
  label: string;
}

/** Payload of the color-selected output. */
export interface ExploreColorSelection {
  instanceId: string;
  token: ChartSeriesColorToken;
}

/**
 * Indicator controls of Data Lab Explore (PRD §9 / §10): the searchable
 * indicator drawer and the active-indicator chips (configure / remove /
 * recolor). Presentational — all mutations route back to the parent and
 * its workspace store.
 */
@Component({
  selector: 'app-explore-indicator-controls',
  imports: [IndicatorPickerComponent, ChartSeriesColorPickerComponent],
  templateUrl: './explore-indicator-controls.component.html',
  styleUrl: './explore-indicator-controls.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExploreIndicatorControlsComponent {
  readonly drawerOpen = input(false);
  readonly categories = input<readonly IndicatorCategory[]>([]);
  /** Canonical keys of active instances — drive the picker's +N badges. */
  readonly activeKeys = input<readonly string[]>([]);
  readonly loading = input(false);
  readonly chips = input<readonly ExploreChipEntry[]>([]);
  readonly activeCount = input(0);
  readonly chipsExpanded = input(false);
  readonly colorOverrides = input<Readonly<Record<string, ChartSeriesColorToken>>>({});

  readonly drawerOpenChange = output<boolean>();
  readonly add = output<IndicatorPickerAdd>();
  readonly chipsExpandedChange = output<boolean>();
  readonly configure = output<string>();
  readonly remove = output<string>();
  readonly colorSelected = output<ExploreColorSelection>();
}
