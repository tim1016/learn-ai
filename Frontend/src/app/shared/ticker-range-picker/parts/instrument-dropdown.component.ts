import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { PickerSymbol } from '../../symbol-catalog/symbol-catalog.types';
import type { CoverageGateState } from '../../symbol-catalog/ensure-coverage.service';
import { CoverageGateStripComponent } from '../../symbol-catalog/coverage-gate-strip.component';
import { PickerOptionRowComponent } from '../../symbol-catalog/picker-option-row.component';

/**
 * The instrument card's open dropdown: the scrolled option list, the
 * degraded banner, the gate strip and the empty states.
 *
 * Pure presentation over values the card computes — every row, banner and
 * button either renders an input or reports intent through an output. The
 * row markup itself lives in `app-picker-option-row`, shared with the
 * multi-symbol card; the gate strip in `app-coverage-gate-strip`.
 */
@Component({
  selector: 'app-instrument-dropdown',
  imports: [RouterLink, PickerOptionRowComponent, CoverageGateStripComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-dropdown.component.html',
  styleUrls: ['./instrument-dropdown.component.scss'],
})
export class InstrumentDropdownComponent {
  readonly visible = input.required<readonly PickerSymbol[]>();
  readonly recent = input<readonly PickerSymbol[]>([]);
  readonly matchCount = input.required<number>();
  readonly activeSymbol = input<string | null>(null);
  readonly query = input('');
  readonly loading = input(false);
  readonly unavailable = input<string | null>(null);
  /** The live catalog is dark but the lake answered — degraded, not empty. */
  readonly degraded = input(false);
  /** No source answered with anything at all — distinct from no match. */
  readonly empty = input(false);
  readonly hostUniverse = input(false);
  readonly gate = input<CoverageGateState | null>(null);

  readonly pick = output<PickerSymbol>();
  readonly retryLake = output();
  readonly retryVendor = output();
  readonly retryGate = output();
  readonly dismiss = output();

  trackBySymbol(_: number, t: PickerSymbol): string {
    return t.symbol;
  }
}
