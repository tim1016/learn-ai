import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { JournalTailComponent } from '../operator-lens/journal-tail.component';
import type { EvidencePage, ReadinessCheckView } from '../lib/broker-v2-panel.types';

/**
 * The proof half of the triage pane: the gates that admit the bot, and the
 * custody journal that records what it did.
 *
 * Gates arrive blocked-first — the pane exists to answer "why is this bot
 * stuck?", so the blocked one carries its explanation and cure while satisfied
 * gates stay single lines.
 */
@Component({
  selector: 'app-triage-evidence',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JournalTailComponent],
  templateUrl: './triage-evidence.component.html',
  host: { class: 'triage-column' },
})
export class TriageEvidenceComponent {
  readonly gates = input.required<readonly ReadinessCheckView[]>();
  readonly journal = input<EvidencePage | null>(null);
  readonly journalLoading = input(false);
}
