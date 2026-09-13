import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import type { FleetCapability } from '../../../../fleet/resource-target';
import {
  type LaneDescriptor,
  laneConfirmedAccount,
  laneIsReady,
} from '../../../../fleet/fleet-directory.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

/** The broker desk's lane directory (PRD §13): every Alpaca lane — Paper and
 * Live side by side — rendered from the fleet registry projection. Each lane
 * carries its own lifecycle and provider summary: one failed or starting
 * lane never masks another (FR-093/096), and every entry deep-links to that
 * lane's canonical clerk-scoped surfaces. */
@Component({
  selector: 'app-alpaca-lane-directory',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReceiptLabelPipe],
  templateUrl: './alpaca-lane-directory.component.html',
  styleUrl: './alpaca-lane-directory.component.scss',
  host: { class: 'block' },
})
export class AlpacaLaneDirectoryComponent {
  private readonly fleet = inject(FleetDirectoryService);
  readonly requestedSurface = input<'deploy' | 'bots' | 'gallery' | null>(null);

  protected readonly lanes = computed(
    () => this.fleet.value()?.clerks.filter((lane) => lane.broker === 'alpaca') ?? [],
  );
  protected readonly loading = this.fleet.isLoading;
  protected readonly failed = computed(() => this.fleet.error() !== undefined);
  protected readonly requestedSurfaceHint = computed(() => {
    switch (this.requestedSurface()) {
      case 'deploy':
        return 'Choose a ready clerk lane below to deploy a strategy.';
      case 'bots':
        return 'Choose a ready clerk lane below to open its Bots roster.';
      case 'gallery':
        return 'Choose a ready clerk lane below to open its Gallery.';
      default:
        return null;
    }
  });

  protected accountOf(lane: LaneDescriptor): string | null {
    return laneConfirmedAccount(lane);
  }

  protected isReady(lane: LaneDescriptor): boolean {
    return laneIsReady(lane);
  }

  protected supports(lane: LaneDescriptor, capability: FleetCapability): boolean {
    return lane.capabilities.includes(capability);
  }
}
