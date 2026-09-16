import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import {
  laneConfirmedAccount,
  laneIsReady,
} from '../../../../fleet/fleet-directory.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import {
  clerkSurfaceCanonicalRoute,
  SURFACE_CAPABILITY,
  SURFACE_LABEL,
  type LaneSurface,
} from './alpaca-lane-directory.component';

/** Why one lane cannot serve one surface right now, in the order an operator
 * can act on them. Rendered as prose; the codes inside go through
 * `receiptLabel` at the template boundary. */
type SurfaceRefusal =
  | { readonly kind: 'lifecycle'; readonly lifecycleState: string }
  | { readonly kind: 'unbound' }
  | { readonly kind: 'capability' };

/**
 * The clerk-only in-place route for a lane surface that cannot open
 * (`/brokers/alpaca/clerks/:clerkId/bots|gallery`). The lane keeps a
 * selectable destination for every surface — the chooser never hides a lane,
 * and this page says exactly why the surface is closed: the lane's lifecycle,
 * a missing account binding, or a missing capability.
 *
 * It renders in place and never retargets: no redirect to another lane, no
 * redirect to the broker directory (FR-096). Configuration stays reachable —
 * configuration access needs no confirmed binding — and a lane that has
 * become servable while the operator sat here links straight to its
 * canonical URL.
 */
@Component({
  selector: 'app-alpaca-clerk-surface-unavailable',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReceiptLabelPipe],
  templateUrl: './alpaca-clerk-surface-unavailable.component.html',
  styleUrl: './alpaca-clerk-surface-unavailable.component.scss',
  host: { class: 'block' },
})
export class AlpacaClerkSurfaceUnavailableComponent {
  private readonly fleet = inject(FleetDirectoryService);

  readonly clerkId = input.required<string>();
  readonly surface = input.required<LaneSurface>();

  protected readonly surfaceName = computed(() => SURFACE_LABEL[this.surface()].long);
  protected readonly chooserLabel = computed(() => SURFACE_LABEL[this.surface()].chooser);
  protected readonly SURFACE_CAPABILITY = SURFACE_CAPABILITY;

  private readonly lane = computed(() =>
    this.fleet.value()?.clerks.find(
      (lane) => lane.broker === 'alpaca' && lane.clerk_id === this.clerkId(),
    ) ?? null,
  );

  protected readonly loading = computed(
    () => this.fleet.isLoading() && this.fleet.value() === undefined,
  );
  protected readonly directoryFailed = computed(() => this.fleet.error() !== undefined);

  protected readonly refusal = computed<SurfaceRefusal | null>(() => {
    const lane = this.lane();
    if (lane === null) return null;
    if (!laneIsReady(lane)) {
      return { kind: 'lifecycle', lifecycleState: lane.lifecycle_state };
    }
    if (laneConfirmedAccount(lane) === null) return { kind: 'unbound' };
    if (!lane.capabilities.includes(SURFACE_CAPABILITY[this.surface()])) {
      return { kind: 'capability' };
    }
    return null;
  });

  /** The lane serves this surface now (the operator arrived on a stale link):
   * offer its canonical URL rather than a refusal. */
  protected readonly canonicalRoute = computed(() => {
    const lane = this.lane();
    if (lane === null || this.refusal() !== null) return null;
    return clerkSurfaceCanonicalRoute(lane.clerk_id, laneConfirmedAccount(lane), this.surface());
  });

  protected readonly configurationRoute = computed(() => {
    const lane = this.lane();
    return lane !== null && lane.capabilities.includes('configuration_manage')
      ? ['/brokers', 'alpaca', 'clerks', lane.clerk_id, 'configuration']
      : null;
  });

  protected readonly laneLabel = computed(() => this.lane()?.display_label ?? null);
}
