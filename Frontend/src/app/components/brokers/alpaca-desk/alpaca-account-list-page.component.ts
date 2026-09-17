import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';

import { AlpacaLaneDirectoryComponent } from './lane-directory/alpaca-lane-directory.component';

/**
 * The Alpaca account list at `/brokers/alpaca` — the only multi-account page
 * (ADR 0064 Decision 2) and the entry to every account workspace.
 *
 * It was the account-less half of the broker desk until the workspace landed:
 * the desk rendered every account card above whichever account the operator
 * had opened. The desk is now one account's Overview tab and this page is the
 * list, so the cards appear exactly once, on the page whose job is choosing.
 * No account is ever selected automatically — a deploy intent that arrives
 * without a lane says so above the list rather than picking one (FR-096).
 */
@Component({
  selector: 'app-alpaca-account-list-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaLaneDirectoryComponent],
  styleUrl: './alpaca-account-list-page.component.scss',
  template: `
    <main class="account-list">
      <header class="account-list__titlebar"><h1>Alpaca</h1></header>
      <app-alpaca-lane-directory [deployIntent]="deployIntent()" />
    </main>
  `,
})
export class AlpacaAccountListPageComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  /** A broker-wide `?deploy` intent has no lane yet: the list itself is the
   * deploy entry point's lane-selection step. */
  protected readonly deployIntent = computed(() => this.queryParams().has('deploy'));
}
