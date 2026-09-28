import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { accountWorkspaceHomeRoute, accountWorkspaceTabRoute } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import {
  laneConfirmedAccount,
  laneIsReady,
} from '../../../fleet/fleet-directory.types';
import type { FleetCapability } from '../../../fleet/resource-target';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

/** The capability a lane must declare before its Home is served from it.
 * Provider-declared evidence, never inferred (FR-097). */
const HOME_CAPABILITY: FleetCapability = 'bot_panel_read';

/** Why one lane cannot serve its Home right now, in the order an operator
 * can act on them. Rendered as prose; the codes inside go through
 * `receiptLabel` at the template boundary. */
type SurfaceRefusal =
  | { readonly kind: 'lifecycle'; readonly lifecycleState: string }
  | { readonly kind: 'unbound' }
  | { readonly kind: 'capability' };

/**
 * The Home of a lane that cannot serve it
 * (`/brokers/alpaca/clerks/:clerkId/home` — the lane-scoped Home URL, which
 * names no account).
 *
 * A not-ready account keeps its workspace (ADR 0064, FR-096): the header and
 * the tab strip stay, and Home itself says exactly why it is closed — the
 * lane's lifecycle, a missing account binding, or a missing capability — and
 * points at Settings, the one tab a lane can serve before it has an
 * account. It renders in place and never retargets: no redirect to another
 * lane, no redirect to the account list. A lane that has become servable
 * while the operator sat here links straight to its account's Home rather
 * than refusing.
 *
 * It is the page's body only: the account name, the mode and the tab strip
 * above it belong to `AlpacaAccountWorkspaceComponent`.
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

  protected readonly HOME_CAPABILITY = HOME_CAPABILITY;

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
    if (!lane.capabilities.includes(HOME_CAPABILITY)) {
      return { kind: 'capability' };
    }
    return null;
  });

  /** The lane serves its Home now (the operator arrived on a stale link):
   * offer the account's Home rather than a refusal. */
  protected readonly canonicalRoute = computed(() => {
    const lane = this.lane();
    if (lane === null || this.refusal() !== null) return null;
    return accountWorkspaceHomeRoute(
      { broker: lane.broker, clerkId: lane.clerk_id, accountId: laneConfirmedAccount(lane) },
    );
  });

  protected readonly settingsRoute = computed(() => {
    const lane = this.lane();
    return lane !== null && lane.capabilities.includes('configuration_manage')
      ? accountWorkspaceTabRoute(
          { broker: lane.broker, clerkId: lane.clerk_id, accountId: null },
          'settings',
        )
      : null;
  });

  protected readonly laneKnown = computed(() => this.lane() !== null);
}
