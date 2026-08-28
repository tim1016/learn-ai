import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { CopyButtonComponent } from '../../../shared/copy-button/copy-button.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { artifactReceiptSections } from '../lib/artifact-receipt';
import { DataLakeService } from '../lib/data-lake.service';

/**
 * Full receipt for one catalog row: hashes, byte metadata, provider
 * parameters, lifecycle timestamps and failure diagnostics.
 *
 * Loads its own detail keyed on the artifact id so the caller only has to
 * say which cell was clicked. Opening a different cell intentionally drops
 * the previous receipt while the new one loads — a stale hash under a new
 * heading would be the one thing this panel must never show.
 */
@Component({
  selector: 'app-artifact-inspector',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './artifact-inspector.component.html',
  styleUrl: './artifact-inspector.component.scss',
  imports: [CopyButtonComponent, ReceiptLabelPipe, TimestampDisplayComponent],
})
export class ArtifactInspectorComponent {
  private readonly lake = inject(DataLakeService);

  readonly artifactId = input.required<number>();
  /** The heatmap row the cell came from, so the header reads before the load lands. */
  readonly symbol = input<string | null>(null);

  protected readonly detail = resource({
    params: () => ({ artifactId: this.artifactId() }),
    loader: ({ params }) => this.lake.artifact(params.artifactId),
  });

  protected readonly sections = computed(() => {
    const read = this.detail.value();
    return read?.kind === 'ok' ? artifactReceiptSections(read.value) : [];
  });

  /**
   * A 404 from `GET /artifacts/{id}` means the same thing either way: there
   * is no receipt to show. It is an id the catalog does not hold, or the
   * whole router is unmounted because the lake is dark — the panel names
   * both rather than guessing which.
   */
  protected readonly receiptAbsent = computed(() => this.detail.value()?.kind === 'not_enabled');

  protected readonly problem = computed(() => {
    const read = this.detail.value();
    if (read?.kind === 'rejected') return { reason: read.reason, message: read.message };
    if (read?.kind === 'unavailable') return { reason: 'unavailable', message: read.message };
    return null;
  });
}
