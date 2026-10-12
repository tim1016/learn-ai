import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { missingParts, reasonGroups, sessionsWith, skippedSessions } from '../data-check-view';
import type { SessionCheck } from '../options-backtest.types';

/**
 * The sessions of a Check data answer that need a word: those that cannot be
 * tested (their reasons grouped by code, then the sessions by date), those
 * that need a download (with what is missing), and the ready ones that will be
 * skipped. A date is the session's open, shown in New York dates.
 */
@Component({
  selector: 'app-options-backtest-data-check-sessions',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './data-check-sessions.component.html',
  styleUrl: './data-check-sessions.component.scss',
})
export class DataCheckSessionsComponent {
  readonly sessions = input.required<readonly SessionCheck[]>();

  protected readonly groups = computed(() => reasonGroups(this.sessions()));
  protected readonly cannot = computed(() => sessionsWith(this.sessions(), 'cannot_be_tested'));
  protected readonly downloads = computed(() => sessionsWith(this.sessions(), 'needs_download'));
  protected readonly skipped = computed(() => skippedSessions(this.sessions()));
  protected readonly missingParts = missingParts;
}
