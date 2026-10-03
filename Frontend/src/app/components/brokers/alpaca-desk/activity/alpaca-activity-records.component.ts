import { ChangeDetectionStrategy, Component, effect, input, output, signal } from '@angular/core';

import type { LaneFence } from '../../../../fleet/lane-fence';
import type { ResourceTarget } from '../../../../fleet/resource-target';
import type { SqliteTimelineQuery } from '../../../../services/brokers.service';
import { AccountDeskTransactionHistoryComponent } from '../../../broker/account-desk/account-desk-transaction-history.component';
import { AccountDeskTransactionHistoryStore } from '../../../broker/account-desk/account-desk-transaction-history-store.service';
import { AlpacaCustodyResolutionComponent } from '../alpaca-custody-resolution.component';
import { AlpacaSqliteCustodyComponent } from '../alpaca-sqlite-custody.component';

/** The instants one Activity period's order records span. */
export interface ActivityRecordsWindow {
  readonly fromMs: number;
  readonly toMs: number;
}

/**
 * The folded "Order records and recovery" section of Activity: the account's
 * order records for the period, whether the Clerk's records agree with
 * Alpaca, and the recovery actions for an order whose outcome is unknown.
 *
 * Closed by default, and nothing inside is read until it is first opened —
 * these are the heaviest reads on the page and most visits never need them.
 * A recovery link that names an order (`?timelineBot=…`) or a recovery action
 * (`?recover=reconcile_now`) opens it on arrival.
 */
@Component({
  selector: 'app-alpaca-activity-records',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskTransactionHistoryComponent,
    AlpacaCustodyResolutionComponent,
    AlpacaSqliteCustodyComponent,
  ],
  providers: [AccountDeskTransactionHistoryStore],
  templateUrl: './alpaca-activity-records.component.html',
  styleUrl: './alpaca-activity.scss',
})
export class AlpacaActivityRecordsComponent {
  readonly target = input.required<ResourceTarget>();
  /** The frozen command fence every recovery action mints against (#2106). */
  readonly fence = input.required<LaneFence>();
  readonly accountId = input.required<string>();
  /** `null` until the period's window has been read. */
  readonly window = input<ActivityRecordsWindow | null>(null);
  /** Whether that window is still being read, as opposed to unreadable. */
  readonly windowReading = input(false);
  /** Re-read the period's window after it could not be read. */
  readonly retry = output();
  readonly timelineQuery = input<SqliteTimelineQuery | null>(null);
  /** The recovery action a link asked for; its button is brought into view. */
  readonly focusAction = input<string | null>(null);

  protected readonly opened = signal(false);
  /** Bumped after a recovery action so every record here re-reads. */
  protected readonly refreshVersion = signal(0);

  constructor() {
    effect(() => {
      if (this.timelineQuery() !== null || this.focusAction() !== null) this.opened.set(true);
    });
  }

  protected onToggle(event: Event): void {
    if (event.target instanceof HTMLDetailsElement && event.target.open) this.opened.set(true);
  }

  protected refresh(): void {
    this.refreshVersion.update((version) => version + 1);
  }
}
