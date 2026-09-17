import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceOriginTabRoute,
  accountWorkspaceTabRoute,
  type AccountWorkspaceOriginTab,
} from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import {
  laneConfirmedAccount,
  laneIsReady,
} from '../../../fleet/fleet-directory.types';
import type { FleetCapability } from '../../../fleet/resource-target';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

/** The capability a lane must declare before one of these two tabs is served
 * from it. Provider-declared evidence, never inferred (FR-097). */
const SURFACE_CAPABILITY: Record<AccountWorkspaceOriginTab, FleetCapability> = {
  bots: 'bot_panel_read',
  gallery: 'gallery_read',
};

/** How this tab names itself while it is explaining that it cannot open. The
 * tab strip above names the tab (`accountWorkspaceTabLabel`); this is the
 * longer form the refusal prose reads with. */
const SURFACE_LABEL: Record<AccountWorkspaceOriginTab, string> = {
  bots: 'Bots roster',
  gallery: 'Gallery',
};

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
  readonly surface = input.required<AccountWorkspaceOriginTab>();

  protected readonly surfaceName = computed(() => SURFACE_LABEL[this.surface()]);
  protected readonly SURFACE_CAPABILITY = SURFACE_CAPABILITY;

  private readonly lane = computed(() => this.fleet.lane('alpaca', this.clerkId()) ?? null);

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
    return accountWorkspaceOriginTabRoute(
      { broker: lane.broker, clerkId: lane.clerk_id, accountId: laneConfirmedAccount(lane) },
      this.surface(),
    );
  });

  protected readonly configurationRoute = computed(() => {
    const lane = this.lane();
    return lane !== null && lane.capabilities.includes('configuration_manage')
      ? accountWorkspaceTabRoute(
          { broker: lane.broker, clerkId: lane.clerk_id, accountId: null },
          'configuration',
        )
      : null;
  });

  protected readonly laneKnown = computed(() => this.lane() !== null);
}
