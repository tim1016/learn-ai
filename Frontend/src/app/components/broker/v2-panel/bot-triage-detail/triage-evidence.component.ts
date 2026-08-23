import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { JournalTailComponent } from '../operator-lens/journal-tail.component';
import type { EvidencePage, ReadinessCheckView } from '../lib/broker-v2-panel.types';

/**
 * The proof half of the triage pane: which commands the bot will currently
 * accept, and the custody journal that records what it did.
 *
 * These are per-command availability rows, not pass/fail admission gates —
 * the lifecycle commands are mutually exclusive, so a healthy running bot
 * reports Resume and Continue as unavailable. Unavailable rows arrive first
 * and carry their reason and cure; available ones stay single lines.
 */
@Component({
  selector: 'app-triage-evidence',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JournalTailComponent],
  templateUrl: './triage-evidence.component.html',
  styleUrl: './triage-evidence.component.scss',
  host: { class: 'triage-column' },
})
export class TriageEvidenceComponent {
  readonly commands = input.required<readonly ReadinessCheckView[]>();
  readonly journal = input<EvidencePage | null>(null);
  readonly journalLoading = input(false);
  /** A failed custody read must not be shown as a genuinely empty journal. */
  readonly journalFailed = input(false);
  readonly journalRetry = output();
}
