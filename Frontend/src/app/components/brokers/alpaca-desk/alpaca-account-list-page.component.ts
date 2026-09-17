import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';

import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { AlpacaLaneCardComponent } from './alpaca-lane-card.component';

/**
 * The Alpaca account list at `/brokers/alpaca` — the only multi-account page
 * (ADR 0064 Decision 2) and the one way into every account workspace.
 *
 * It was the account-less half of the broker desk until the workspace landed:
 * the desk rendered every account card above whichever account the operator
 * had opened. The desk is now one account's Overview tab and this page is the
 * list, so the cards appear exactly once, on the page whose job is choosing.
 * No account is ever selected automatically — a deploy intent that arrives
 * without a lane says so above the list rather than picking one (FR-096).
 *
 * The page *is* the list: it renders the directory's Alpaca lanes itself
 * rather than through a second component, so it has one heading and the term
 * "lane directory" (CONTEXT.md's own vocabulary lists it under Avoid for this
 * page) names only the fleet read underneath, never the page.
 *
 * Directory-level loading and failure are stated once here; each card owns
 * its own reads, so a failed read on one account never blanks another
 * (FR-093).
 */
@Component({
  selector: 'app-alpaca-account-list-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaLaneCardComponent],
  templateUrl: './alpaca-account-list-page.component.html',
  styleUrl: './alpaca-account-list-page.component.scss',
})
export class AlpacaAccountListPageComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly fleet = inject(FleetDirectoryService);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  /** A broker-wide `?deploy` intent has no lane yet: the list itself is the
   * deploy entry point's lane-selection step. */
  protected readonly deployIntent = computed(() => this.queryParams().has('deploy'));

  protected readonly accounts = computed(() => this.fleet.lanesOf('alpaca'));
  protected readonly loading = this.fleet.isLoading;
  protected readonly failed = computed(() => this.fleet.error() !== undefined);
}
