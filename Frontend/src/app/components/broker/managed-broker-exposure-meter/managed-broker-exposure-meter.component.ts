import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { FleetContamination } from '../../../api/live-instances.types';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

interface ManagedBrokerExposureRow {
  symbol: string;
  broker: number;
  managed: number;
  residual: number;
}

@Component({
  selector: 'app-managed-broker-exposure-meter',
  imports: [DecimalPipe, ReceiptLabelPipe, SectionErrorComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './managed-broker-exposure-meter.component.html',
  styleUrl: './managed-broker-exposure-meter.component.scss',
})
export class ManagedBrokerExposureMeterComponent {
  readonly fleet = input.required<FleetContamination | null>();
  readonly error = input<unknown>(null);
  readonly retry = output();

  readonly rows = computed<ManagedBrokerExposureRow[]>(() => {
    const fleet = this.fleet();
    const netPositions = fleet?.net_positions;
    if (fleet === null || netPositions === null || netPositions === undefined) return [];
    const symbols = new Set([
      ...Object.keys(netPositions),
      ...Object.keys(fleet.explained_total),
      ...Object.keys(fleet.residual),
    ]);
    return [...symbols]
      .sort((left, right) => left.localeCompare(right))
      .map((symbol) => ({
        symbol,
        broker: netPositions[symbol] ?? 0,
        managed: fleet.explained_total[symbol] ?? 0,
        residual: fleet.residual[symbol] ?? 0,
      }));
  });
}
