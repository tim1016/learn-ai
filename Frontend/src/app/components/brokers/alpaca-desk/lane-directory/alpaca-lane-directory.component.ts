import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import type { FleetCapability } from '../../../../fleet/resource-target';
import { AlpacaLaneCardComponent } from './alpaca-lane-card.component';

/** The deep-linkable lane surfaces a chooser (or the desk directory) offers. */
export type LaneSurface = 'bots' | 'gallery';

/** The capability a lane must declare before a surface is served from it. */
export const SURFACE_CAPABILITY: Record<LaneSurface, FleetCapability> = {
  bots: 'bot_panel_read',
  gallery: 'gallery_read',
};

/** One surface's display vocabulary, shared by every surface-aware page so
 * the same surface never picks up a second name. */
export const SURFACE_LABEL: Record<LaneSurface, { chooser: string; long: string }> = {
  bots: { chooser: 'Bots', long: 'Bots roster' },
  gallery: { chooser: 'Gallery', long: 'Gallery' },
};

/**
 * The clerk-only in-place route a lane lands on when it cannot serve a
 * surface yet (not ready, unbound, or without the capability). Configuration
 * access needs no binding, so the lane keeps a selectable destination for
 * every surface — a link never vanishes, it explains itself (FR-096).
 */
export function clerkSurfaceRoute(clerkId: string, surface: LaneSurface): readonly string[] {
  return ['/brokers', 'alpaca', 'clerks', clerkId, surface];
}

/** The canonical operational URL for one lane's surface, or null when the
 * lane has no confirmed account to serve it from. */
export function clerkSurfaceCanonicalRoute(
  clerkId: string,
  accountId: string | null,
  surface: LaneSurface,
): readonly string[] | null {
  return accountId === null
    ? null
    : ['/brokers', 'alpaca', 'clerks', clerkId, 'accounts', accountId, surface];
}

/**
 * The broker desk's lane directory (PRD §13): every Alpaca lane — Paper and
 * Live side by side — rendered from the fleet registry projection. Each lane
 * card carries its own lifecycle and provider summary: one failed or
 * starting lane never masks another (FR-093/096), and every entry deep-links
 * to that lane's canonical clerk-scoped surfaces.
 *
 * `surface` narrows the directory into a read-only chooser for one surface
 * (`/brokers/alpaca/bots` / `/gallery`): no lane is ever selected
 * automatically, and unavailable lanes stay selectable through their
 * clerk-only route. `null` renders the desk's full directory.
 */
@Component({
  selector: 'app-alpaca-lane-directory',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaLaneCardComponent],
  templateUrl: './alpaca-lane-directory.component.html',
  styleUrl: './alpaca-lane-directory.component.scss',
  host: { class: 'block' },
})
export class AlpacaLaneDirectoryComponent {
  private readonly fleet = inject(FleetDirectoryService);
  readonly surface = input<LaneSurface | null>(null);
  /** A broker-wide `?deploy` intent has no lane yet: say so above the
   * directory, so the deploy entry point stays an explicit lane choice. */
  readonly deployIntent = input(false);
  /** The surface vocabulary the chooser hint names surfaces through. */
  protected readonly SURFACE_LABEL = SURFACE_LABEL;

  protected readonly lanes = computed(
    () => this.fleet.value()?.clerks.filter((lane) => lane.broker === 'alpaca') ?? [],
  );
  protected readonly loading = this.fleet.isLoading;
  protected readonly failed = computed(() => this.fleet.error() !== undefined);
  protected readonly heading = computed(() => {
    const surface = this.surface();
    return surface === null ? 'Choose an account' : `Choose an account — ${SURFACE_LABEL[surface].long}`;
  });
}
