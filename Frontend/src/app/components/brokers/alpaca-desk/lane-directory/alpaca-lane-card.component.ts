import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import type { FleetCapability } from '../../../../fleet/resource-target';
import {
  type LaneDescriptor,
  laneConfirmedAccount,
  laneDisplayName,
  laneDisplayNameText,
  laneIsReady,
} from '../../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService, verdictModeChip } from '../../../../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { AlpacaLaneModeChipComponent } from '../alpaca-lane-mode-chip.component';
import {
  clerkSurfaceCanonicalRoute,
  clerkSurfaceRoute,
  SURFACE_CAPABILITY,
  type LaneSurface,
} from './alpaca-lane-directory.component';

/**
 * One lane's directory card: its stable label, lifecycle, mode chip (from
 * the same server-owned verdict the shell's pills render), authority as a
 * separate fact, and its deep links. A surface link renders for EVERY lane —
 * the canonical operational URL when the lane serves the surface, otherwise
 * the lane's own clerk-only route — so a link never vanishes, it explains
 * itself (FR-096: one lane's failure never masks or substitutes another).
 *
 * The host renders `display: contents` (no box of its own) so its `<li>`
 * root is the list's effective direct child for layout and the
 * accessibility tree, without an attribute selector — this repo's
 * `component-selector` lint rule requires `app-` kebab-case element
 * selectors.
 */
@Component({
  selector: 'app-alpaca-lane-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReceiptLabelPipe, AlpacaLaneModeChipComponent],
  templateUrl: './alpaca-lane-card.component.html',
  styleUrl: './alpaca-lane-card.component.scss',
})
export class AlpacaLaneCardComponent {
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);
  private readonly fleetDirectory = inject(FleetDirectoryService);

  readonly lane = input.required<LaneDescriptor>();
  /** The surface being chosen, or null on the desk's full directory. */
  readonly surface = input<LaneSurface | null>(null);

  protected readonly isReady = computed(() => laneIsReady(this.lane()));
  protected readonly account = computed(() => laneConfirmedAccount(this.lane()));
  /** This lane's display name: its account nickname, or its lane label
   * until one is set. Siblings come from injecting `FleetDirectoryService`
   * directly (`lanesOf(lane.broker)`), not a prop the parent must remember
   * to pass — one lane card can't render disambiguated from another again. */
  protected readonly displayName = computed(() =>
    laneDisplayName(this.lane(), this.fleetDirectory.lanesOf(this.lane().broker)),
  );
  /** `displayName` as one accessible-name-safe string — carries the
   * disambiguator into `aria-label`, not just the visible text (a shared
   * name is a supported state under ADR 0064 Decision 5, not an edge case
   * that can skip the accessible name). */
  protected readonly displayNameText = computed(() => laneDisplayNameText(this.displayName()));

  /** True when this lane can serve `surface` at its canonical URL right now. */
  protected servesSurface(surface: LaneSurface): boolean {
    const lane = this.lane();
    return (
      laneIsReady(lane)
      && laneConfirmedAccount(lane) !== null
      && lane.capabilities.includes(SURFACE_CAPABILITY[surface])
    );
  }

  /**
   * The route one lane's surface link points at: the canonical operational
   * URL when the lane serves the surface, otherwise the clerk-only route
   * that explains why it cannot — never another lane's URL.
   */
  protected surfaceRoute(surface: LaneSurface): readonly string[] {
    if (!this.servesSurface(surface)) {
      return clerkSurfaceRoute(this.lane().clerk_id, surface);
    }
    return (
      clerkSurfaceCanonicalRoute(this.lane().clerk_id, this.account(), surface)
      ?? clerkSurfaceRoute(this.lane().clerk_id, surface)
    );
  }

  protected supports(capability: FleetCapability): boolean {
    return this.lane().capabilities.includes(capability);
  }

  /** This lane's account-mode chip, from the same server-owned verdict the
   * shell's pills render — never composed on the client (ADR 0011 §7). */
  protected readonly modeChip = computed(() =>
    verdictModeChip(this.liveVerdicts.stateFor(this.lane().clerk_id)),
  );
}
