import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { PickerSymbol } from '../../symbol-catalog/symbol-catalog.types';
import type { CoverageGateState } from '../../symbol-catalog/ensure-coverage.service';
import type { SymbolCatalogStatus } from '../../symbol-catalog/symbol-catalog.service';
import { CoverageGateStripComponent } from '../../symbol-catalog/coverage-gate-strip.component';
import { InstrumentOptionComponent } from './instrument-option.component';

/**
 * The instrument card's open dropdown: the scrolled option list, the
 * degraded banner, the gate strip and the empty states.
 *
 * Pure presentation over values the card computes — every row, banner and
 * button either renders an input or reports intent through an output. The
 * option button lives in `app-instrument-option`, whose cells come from the
 * shared `app-picker-option-row`; the gate strip lives in
 * `app-coverage-gate-strip`.
 */
@Component({
  selector: 'app-instrument-dropdown',
  imports: [RouterLink, InstrumentOptionComponent, CoverageGateStripComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-dropdown.component.html',
  styleUrls: ['./instrument-dropdown.component.scss'],
})
export class InstrumentDropdownComponent {
  readonly view = input.required<InstrumentDropdownView>();
  readonly gate = input<CoverageGateState | null>(null);

  readonly pick = output<PickerSymbol>();
  readonly retryLake = output();
  readonly retryVendor = output();
  readonly retryGate = output();
  readonly dismiss = output();
}

export interface InstrumentDropdownView {
  readonly visible: readonly PickerSymbol[];
  readonly recent: readonly PickerSymbol[];
  readonly matchCount: number;
  readonly activeSymbol: string | null;
  readonly query: string;
  readonly catalogStatus: SymbolCatalogStatus;
  readonly hostUniverse: boolean;
}
