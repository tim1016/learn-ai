import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';

import { GOLDEN_QUALIFICATION_QUERY_PARAM } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { AlpacaLaneCardComponent } from './alpaca-lane-card.component';

/** The runbooks each dead-end state points at: how to add an account, and
 * how to bring the account list back. They live in the repository, where
 * the served architecture manual links too. */
const RUNBOOKS = 'https://github.com/tim1016/learn-ai/blob/master/docs/runbooks';
export const ADD_ACCOUNT_RUNBOOK_URL = `${RUNBOOKS}/add-an-alpaca-account.md`;
export const DIRECTORY_RUNBOOK_URL = `${RUNBOOKS}/fleet-directory-unavailable.md`;

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
 * Directory-level loading and failure are stated once here, each with its
 * next step: try the read again, or the runbook that fixes it. Each card owns
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

  /** A golden configuration on its way to Deploy (#2696): "Use in Deploy"
   * names no account, so choosing one here is the step it still needs. */
  protected readonly goldenHandoff = computed(() => this.queryParams().has(GOLDEN_QUALIFICATION_QUERY_PARAM));

  /** A broker-wide `?deploy` intent has no lane yet: the list itself is the
   * deploy entry point's lane-selection step. A golden handoff is one too. */
  protected readonly deployIntent = computed(() => this.queryParams().has('deploy') || this.goldenHandoff());

  protected readonly accounts = computed(() => this.fleet.lanesOf('alpaca'));
  protected readonly loading = this.fleet.isLoading;
  protected readonly failed = computed(() => this.fleet.error() !== undefined);
  protected readonly addAccountRunbook = ADD_ACCOUNT_RUNBOOK_URL;
  protected readonly directoryRunbook = DIRECTORY_RUNBOOK_URL;

  /** A retry the owner asked for that failed again, said in the alert so a
   * click never looks like it did nothing. */
  protected readonly retryFailed = signal(false);

  protected retry(): void {
    this.retryFailed.set(false);
    this.fleet.refresh().catch(() => this.retryFailed.set(true));
  }
}
