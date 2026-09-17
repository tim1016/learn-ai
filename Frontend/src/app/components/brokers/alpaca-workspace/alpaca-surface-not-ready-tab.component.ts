import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import {
  laneConfirmedAccount,
  laneIsReady,
} from '../../../fleet/fleet-directory.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import {
  clerkSurfaceCanonicalRoute,
  SURFACE_CAPABILITY,
  SURFACE_LABEL,
  type LaneSurface,
} from '../alpaca-desk/lane-directory/alpaca-lane-directory.component';

/** Why one lane cannot serve one surface right now, in the order an operator
 * can act on them. Rendered as prose; the codes inside go through
 * `receiptLabel` at the template boundary. */
type SurfaceRefusal =
  | { readonly kind: 'lifecycle'; readonly lifecycleState: string }
  | { readonly kind: 'unbound' }
  | { readonly kind: 'capability' };

/**
 * The Bots or Gallery tab of a lane that cannot serve it
 * (`/brokers/alpaca/clerks/:clerkId/bots|gallery` — the lane-scoped tab URLs,
 * which name no account).
 *
 * A not-ready account keeps its workspace (ADR 0064, FR-096): the header and
 * the tab strip stay, and the tab itself says exactly why the surface is
 * closed — the lane's lifecycle, a missing account binding, or a missing
 * capability — and points at Configuration, the one tab a lane can serve
 * before it has an account. It renders in place and never retargets: no
 * redirect to another lane, no redirect to the account list. A lane that has
 * become servable while the operator sat here links straight to its canonical
 * account-scoped URL rather than refusing.
 *
 * It is the tab's body only: the account name, the mode and the tab strip
 * above it belong to `AlpacaAccountWorkspaceComponent`, which is what replaced
 * the standalone clerk-only explanation pages this grew out of.
 */
@Component({
  selector: 'app-alpaca-surface-not-ready-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReceiptLabelPipe],
  templateUrl: './alpaca-surface-not-ready-tab.component.html',
  styleUrl: './alpaca-surface-not-ready-tab.component.scss',
  host: { class: 'block' },
})
export class AlpacaSurfaceNotReadyTabComponent {
  private readonly fleet = inject(FleetDirectoryService);

  readonly clerkId = input.required<string>();
  readonly surface = input.required<LaneSurface>();

  protected readonly surfaceName = computed(() => SURFACE_LABEL[this.surface()].long);
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

  protected readonly laneKnown = computed(() => this.lane() !== null);
}
