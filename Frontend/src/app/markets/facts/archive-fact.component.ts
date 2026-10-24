import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { OptionsArchiveFact } from './instrument-facts.service';

type ArchiveKind = OptionsArchiveFact['kind'];

const LABEL: Readonly<Record<ArchiveKind, string>> = {
  complete: 'Options archive: every required day stored',
  some_days: 'Options archive: some days stored',
  none_stored: 'Options archive: no days stored',
  unavailable: 'Options archive: unavailable',
};

/**
 * An underlying's options-archive status, said as what is stored and no more
 * (PRD #3490 R08): "some days stored" is not coverage, and "every required day
 * stored" is the archive's own span, not a claim about a narrower window a page
 * asks about. An underlying the archive does not track has no fact and draws
 * nothing, since there is nothing sourced to say.
 */
@Component({
  selector: 'app-archive-fact',
  imports: [TimestampDisplayComponent],
  templateUrl: './archive-fact.component.html',
  styleUrl: './archive-fact.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ArchiveFactComponent {
  readonly fact = input<OptionsArchiveFact | null | undefined>(null);

  protected readonly view = computed(() => {
    const fact = this.fact();
    if (fact === null || fact === undefined) return null;
    const from = fact.earliest_required_session_ms ?? null;
    const through = fact.complete_through_session_ms ?? null;
    return {
      kind: fact.kind,
      label: LABEL[fact.kind],
      span: fact.kind === 'complete' && from !== null && through !== null ? { from, through } : null,
      latest: fact.kind === 'some_days' ? (fact.last_archived_session_ms ?? null) : null,
      explanation: explain(fact),
    };
  });
}

function explain(fact: OptionsArchiveFact): string {
  switch (fact.kind) {
    case 'complete':
      return "Every session the archive requires is stored. That is the archive's own span, not a claim about a narrower window.";
    case 'some_days':
      return `Some sessions are stored; ${fact.missing_sessions ?? 'some'} required sessions are not. A window is covered only where every session in it is stored.`;
    case 'none_stored':
      return 'The archive tracks this underlying and has stored no session of it yet.';
    case 'unavailable':
      return fact.reason ?? 'The archive could not be read.';
  }
}
