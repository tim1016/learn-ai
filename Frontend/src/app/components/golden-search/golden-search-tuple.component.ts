import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { fullPointEntries } from './golden-search-display';
import type { EvidenceCandidate, Point, StrategyCapability, StudyDetail } from './golden-search.types';

/**
 * The exact settings being approved — or, once approved, the golden settings
 * (#2696): strategy, instrument and program version, the server's settings
 * sentence, every parameter including those at their defaults and the fixed
 * controls, and what it replaces. Approval starts no bot.
 */
@Component({
  selector: 'app-golden-search-tuple',
  imports: [AssetIdentityComponent, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-tuple.component.html',
  styleUrl: './golden-search-tuple.component.scss',
})
export class GoldenSearchTupleComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);
  /** The examined candidate's evidence (settings sentence), when recorded. */
  readonly candidate = input<EvidenceCandidate | null>(null);
  /** The frozen incumbent's evidence: what approval replaces. */
  readonly incumbent = input<EvidenceCandidate | null>(null);

  protected readonly approved = computed(() => this.study().state === 'approved');
  protected readonly qualificationId = computed(() => this.study().results.qualification?.qualification_id ?? this.study().qualification_id);
  protected readonly programVersion = computed(() => this.study().results.qualification?.deploy?.program_version ?? this.study().receipt.program_version);
  /** The approved parameters once published; before that, the examined candidate's point. */
  private readonly point = computed<Point | null>(() => {
    const study = this.study();
    return study.results.qualification?.deploy?.parameters ?? study.results.exam?.candidate_point ?? this.candidate()?.point ?? null;
  });
  protected readonly entries = computed(() => {
    const point = this.point();
    return point === null ? [] : fullPointEntries(point, this.capability());
  });
  protected readonly fixedChips = computed(() => {
    const sentence = this.candidate()?.fixed_sentence.trim() ?? '';
    const fixed = (this.capability()?.fixed ?? []).map((control) => `${control.label} ${control.value}`);
    return sentence === '' ? fixed : [sentence, ...fixed];
  });
}
