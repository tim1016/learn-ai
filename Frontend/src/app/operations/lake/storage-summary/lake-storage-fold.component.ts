import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { DataLakeProblem, StorageSummaryResponse } from '../../../shared/data-lake';
import { LakeStorageSummaryComponent } from './lake-storage-summary.component';

/**
 * The storage overview, folded shut by default: it answers "how much is on
 * disk", which matters after "can a run read it" and never before.
 */
@Component({
  selector: 'app-lake-storage-fold',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './lake-storage-fold.component.scss',
  imports: [LakeStorageSummaryComponent, ReceiptLabelPipe],
  template: `
    <details class="storage">
      <summary class="storage__summary">
        <span class="storage__title">Storage</span>
        <span class="storage__hint">Files by kind and size on disk</span>
      </summary>
      @if (summary(); as value) {
        <app-lake-storage-summary [summary]="value" />
      } @else if (problem(); as issue) {
        <p class="storage__state" role="alert">{{ issue.reason | receiptLabel }}: {{ issue.message }}</p>
      } @else {
        <p class="storage__state" aria-live="polite" aria-busy="true">Reading the catalog…</p>
      }
    </details>
  `,
})
export class LakeStorageFoldComponent {
  /** Null while the read is in flight or after it failed. */
  readonly summary = input<StorageSummaryResponse | null>(null);
  readonly problem = input<DataLakeProblem | null>(null);
}
