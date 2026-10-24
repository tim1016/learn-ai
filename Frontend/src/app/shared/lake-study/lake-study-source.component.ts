import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../timestamp/timestamp-display.component';
import { WorkspaceDataSourceComponent } from '../workspace/workspace-data-source.component';
import { WorkspaceStateBlockComponent } from '../workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../workspace/workspace-state';
import {
  toDataSourceProvenance,
  type LakeCoverageGroup,
  type LakeSessionOutcome,
  type LakeStudyDataSource,
  type LakeStudyRefusal,
} from './lake-study';

/** How admission's per-session outcomes are worded for the reader. */
const OUTCOME_LABELS: Readonly<Record<LakeSessionOutcome, string>> = {
  missing: 'not in the lake',
  building: 'still being captured',
  corrupt: 'cannot be read',
  unavailable_from_provider: 'unavailable from the provider',
};

/**
 * Where a lake-sourced study's data came from, and, when the lake could not
 * supply it, why not (#3521).
 *
 * With a source and no refusal it is the shared data-source line: the lake and
 * its provider, the window read, the resolution, the adjustment and the session,
 * with the added, reused and missing counts one disclosure away. With a refusal
 * it first says the study did not run and gives the server's reason, then the
 * session counts admission reached and each session it could not make ready.
 * A refusal that came before anything was read has no source and shows none.
 *
 * Distinct from the line itself: this maps a lake study's server blocks onto it
 * (`toDataSourceProvenance`) so no study's host does.
 */
@Component({
  selector: 'app-lake-study-source',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent, WorkspaceDataSourceComponent, WorkspaceStateBlockComponent],
  templateUrl: './lake-study-source.component.html',
  styleUrl: './lake-study-source.component.scss',
})
export class LakeStudySourceComponent {
  /** The study's `data_source` block, mapped; `null` when it has none to show. */
  readonly source = input<LakeStudyDataSource | null>(null);
  /** Present when the study did not run because the lake could not supply its inputs. */
  readonly refusal = input<LakeStudyRefusal | null>(null);

  protected readonly provenance = computed(() => {
    const source = this.source();
    return source === null ? null : toDataSourceProvenance(source);
  });

  protected readonly refusedState = computed<WorkspaceStateContent | null>(() => {
    const refusal = this.refusal();
    return refusal === null
      ? null
      : { kind: 'failed', happened: 'No result: the lake could not supply this study its data.', why: refusal.reason };
  });

  /** The counts admission reached before it stopped, window first. */
  protected readonly countLines = computed(() => {
    const coverage = this.refusal()?.coverage ?? null;
    if (coverage === null) return [];
    const lines = [{ label: 'Window', group: coverage.window }];
    if (coverage.warmup.expectedSessions > 0) lines.push({ label: 'Warmup', group: coverage.warmup });
    return lines.map(({ label, group }) => ({ label, text: this.countText(group) }));
  });

  protected readonly omitted = computed(() => {
    const refusal = this.refusal();
    return refusal === null ? 0 : Math.max(0, refusal.sessionsTotal - refusal.sessions.length);
  });

  protected outcomeLabel(outcome: LakeSessionOutcome): string {
    return OUTCOME_LABELS[outcome];
  }

  private countText(group: LakeCoverageGroup): string {
    const parts = [`${group.expectedSessions} sessions`, `${group.reused} reused`, `${group.added} added`, `${group.missing} missing`];
    if (group.failed > 0) {
      const why = [
        group.building > 0 ? `${group.building} still being captured` : '',
        group.corrupt > 0 ? `${group.corrupt} cannot be read` : '',
        group.unavailableFromProvider > 0 ? `${group.unavailableFromProvider} unavailable from the provider` : '',
      ].filter((part) => part !== '');
      parts.push(`${group.failed} failed (${why.join(', ')})`);
    }
    return parts.join(' · ');
  }
}
