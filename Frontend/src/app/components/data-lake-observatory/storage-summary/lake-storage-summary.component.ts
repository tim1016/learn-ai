import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { formatBytes } from '../lib/artifact-receipt';
import type { StorageSummaryResponse } from '../lib/data-lake.types';

/**
 * What the lake actually holds: artifact counts and bytes by kind, and each
 * symbol's day-keyed coverage span.
 *
 * Renders honestly on an empty catalog rather than showing zeroed tables —
 * a lake with nothing in it is a real answer to "what data do we own?", not
 * a loading state.
 */
@Component({
  selector: 'app-lake-storage-summary',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lake-storage-summary.component.html',
  styleUrl: './lake-storage-summary.component.scss',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
})
export class LakeStorageSummaryComponent {
  readonly summary = input.required<StorageSummaryResponse>();

  protected readonly totalArtifacts = computed(() =>
    this.summary().kinds.reduce((total, kind) => total + kind.artifact_count, 0),
  );

  protected readonly totalBytes = computed(() =>
    this.summary().kinds.reduce((total, kind) => total + kind.total_bytes, 0),
  );

  protected readonly isEmpty = computed(
    () => this.summary().kinds.length === 0 && this.summary().symbols.length === 0,
  );

  protected bytes(value: number): string {
    return formatBytes(value);
  }
}
