import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  AlpacaLaneDirectoryComponent,
  SURFACE_LABEL,
  type LaneSurface,
} from './lane-directory/alpaca-lane-directory.component';

/**
 * The read-only lane chooser at `/brokers/alpaca/bots|gallery`. It lists every
 * Alpaca clerk lane from the fleet directory and links each one to that
 * lane's canonical operational URL — or, when the lane cannot serve the
 * surface yet, to its clerk-only route that explains why. Nothing is ever
 * selected automatically: the operator's click is the only lane choice
 * (FR-096 — a lane failure never redirects to another lane).
 */
@Component({
  selector: 'app-alpaca-surface-chooser',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaLaneDirectoryComponent, RouterLink],
  templateUrl: './alpaca-surface-chooser.component.html',
  styleUrl: './alpaca-surface-chooser.component.scss',
  host: { class: 'block' },
})
export class AlpacaSurfaceChooserComponent {
  /** Bound from the route's `data.surface` — 'bots' or 'gallery'. */
  readonly surface = input.required<LaneSurface>();

  protected readonly title = computed(
    () => `Alpaca ${SURFACE_LABEL[this.surface()].chooser.toLowerCase()}`,
  );
  protected readonly siblingSurface = computed<LaneSurface>(() =>
    this.surface() === 'bots' ? 'gallery' : 'bots',
  );
  protected readonly siblingLabel = computed(() => SURFACE_LABEL[this.siblingSurface()].chooser);
}
