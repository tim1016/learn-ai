import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { Drawer } from 'primeng/drawer';

import type { CoverageCellSelection } from '../coverage-heatmap/coverage-heatmap.component';
import { ArtifactInspectorComponent } from './artifact-inspector.component';

/**
 * Drawer chrome around the artifact receipt.
 *
 * Owns only the open/close contract, so the page stays a layout and the
 * inspector stays a receipt. The drawer body is created and destroyed with
 * the selection, which is what makes each open a fresh load rather than a
 * previously-fetched receipt under a new heading.
 */
@Component({
  selector: 'app-artifact-inspector-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, ArtifactInspectorComponent],
  template: `
    <p-drawer
      [visible]="selection() !== null"
      (visibleChange)="onVisibleChange($event)"
      position="right"
      header="Artifact receipt"
      [modal]="true"
      [dismissible]="true"
      [style]="{ width: 'min(720px, 94vw)' }"
    >
      @if (selection(); as selected) {
        @if (selected.cell.artifactId !== null) {
          <app-artifact-inspector
            [artifactId]="selected.cell.artifactId"
            [symbol]="selected.symbol"
          />
        }
      }
    </p-drawer>
  `,
})
export class ArtifactInspectorDrawerComponent {
  readonly selection = input<CoverageCellSelection | null>(null);
  readonly closed = output();

  protected onVisibleChange(visible: boolean): void {
    if (!visible) this.closed.emit();
  }
}
