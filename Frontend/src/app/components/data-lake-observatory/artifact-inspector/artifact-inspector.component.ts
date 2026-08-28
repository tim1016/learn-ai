import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { CopyButtonComponent } from '../../../shared/copy-button/copy-button.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { artifactReceiptSections } from '../lib/artifact-receipt';
import { DataLakeService, describeFailure } from '../lib/data-lake.service';

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
   * One failure branch for every way there is no receipt, including an id
   * the catalog does not hold — the endpoint sends that as a typed
   * `artifact_not_found`, so the panel names it rather than hedging about
   * which of two 404s it was looking at.
   */
  protected readonly problem = computed(() => describeFailure(this.detail.value()));
}
