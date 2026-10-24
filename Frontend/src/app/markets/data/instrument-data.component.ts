import { ChangeDetectionStrategy, Component, computed, effect, inject, untracked } from '@angular/core';

import type { InstrumentDataRequest, LakeTree } from '../../shared/data-lake';
import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { TimestampDisplayPipe } from '../../shared/timestamp';
import { InstrumentContextService } from '../instrument-context.service';
import { DataFillComponent } from './data-fill.component';
import { DataGapsComponent } from './data-gaps.component';
import { DataSummaryComponent } from './data-summary.component';
import { InstrumentDataStore } from './instrument-data.store';
import { QualityEvidenceComponent } from './quality-evidence/quality-evidence.component';

/** The lake trees the place can read. The choice is the context's, so the Chart and this tab read the same one. */
const TREE_CHOICES: readonly { readonly value: LakeTree; readonly label: string }[] = [
  { value: 'polygon_split_adjusted', label: 'Split-adjusted' },
  { value: 'raw', label: 'Raw' },
];

/**
 * An instrument's Data tab: what the lake holds for the committed window and
 * adjustment, per scheduled session, read-only (PRD #3490 R09, R10, R12, R13).
 *
 * Opening it reads Python's coverage and readiness for the request and fills
 * nothing, so a gap can be looked at before anything repairs it. Coming back
 * with the request unchanged shows the reading the place holds. The price
 * adjustment is part of the place's context, in the address: the Chart reads
 * the same tree this tab inspects, a tab change carries it, and a reload keeps
 * it. Returns reads its own (split and dividend adjusted from the raw bars). Check reads
 * again and Fill repairs the gaps through the lake's own admission; both are
 * the person's explicit action. The lake stores whole-day minute bars, so the
 * timeframe and the session of the context do not change what it holds, and
 * the tab says so rather than ignoring them silently.
 */
@Component({
  selector: 'app-instrument-data',
  imports: [
    WorkspaceStateBlockComponent,
    DataSummaryComponent,
    DataGapsComponent,
    DataFillComponent,
    QualityEvidenceComponent,
    TimestampDisplayPipe,
  ],
  templateUrl: './instrument-data.component.html',
  styleUrl: './instrument-data.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InstrumentDataComponent {
  private readonly context = inject(InstrumentContextService);
  private readonly store = inject(InstrumentDataStore);

  protected readonly treeChoices = TREE_CHOICES;
  protected readonly inspection = this.store.inspection;

  protected readonly tree = computed<LakeTree>(() => this.context.shown().adjustment);

  /** What the lake is asked about: the committed window of the address and this tab's tree. */
  protected readonly request = computed<InstrumentDataRequest | null>(() => {
    const context = this.context.context();
    return context === null
      ? null
      : { symbol: this.context.symbol(), from: context.window.from, to: context.window.to, tree: context.adjustment };
  });

  /** Said when the context names a setting the lake's whole-day files do not depend on. */
  protected readonly contextNote = computed<string | null>(() => {
    const { timeframe, session } = this.context.shown();
    const settings: string[] = [];
    if (timeframe !== '1d') settings.push(`the ${timeframe} timeframe`);
    if (session === 'extended') settings.push('the Extended hours session');
    return settings.length === 0
      ? null
      : `The lake stores whole-day minute bars, so ${settings.join(' and ')} above does not change what is held.`;
  });

  protected readonly state = computed<WorkspaceStateContent | null>(() => {
    const inspection = this.inspection();
    switch (inspection.kind) {
      case 'idle':
        return null;
      case 'checking':
        return {
          kind: 'checking',
          happened: `Reading what the lake holds of ${inspection.request.symbol}, ${inspection.request.from} → ${inspection.request.to}.`,
          next: 'This only reads the catalog and the files already on disk. Nothing is fetched.',
        };
      case 'refused':
        return {
          kind: 'invalid',
          happened: 'This window cannot be read, so nothing is shown for it.',
          why: `${formatReceiptLabel(inspection.reason)}: ${inspection.message}`,
          next: 'Change the window above. The request is refused whole; it is never cut short.',
        };
      case 'unavailable':
        return {
          kind: 'unavailable',
          happened: 'What the lake holds could not be read.',
          why: inspection.message,
          next: 'Check again when the data plane answers.',
          actions: [{ id: 'check', label: 'Check again' }],
        };
      case 'read':
        return null;
    }
  });

  constructor() {
    // The one trigger: the committed request. The same request again is held, not read.
    effect(() => {
      const request = this.request();
      if (request !== null) untracked(() => this.store.show(request));
    });
  }

  protected onTreePicked(event: Event): void {
    if (event.target instanceof HTMLSelectElement) this.context.commitAdjustment(event.target.value);
  }

  protected check(): void {
    const request = this.request();
    if (request !== null) this.store.check(request);
  }

  protected onAction(id: string): void {
    if (id === 'check') this.check();
  }
}
