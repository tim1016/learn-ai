import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { ChainFactsComponent } from './chain-facts.component';
import { ChainGridComponent } from './chain-grid.component';
import type { ChainPresentation } from './chain-presentation';

/**
 * The one chain presentation (PRD #3490 R11): what the chain says about itself, then its grid,
 * or the reason there is no grid. Every source adapter hands it a `ChainPresentation`, so a
 * Now · Snapshot chain and a Past · Archive chain are the same page over different data.
 */
@Component({
  selector: 'app-chain-view',
  imports: [ChainFactsComponent, ChainGridComponent, WorkspaceStateBlockComponent],
  templateUrl: './chain-view.component.html',
  styleUrl: './chain-view.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChainViewComponent {
  readonly chain = input.required<ChainPresentation>();
  /** Why the grid is not drawn, when it is not. */
  readonly state = input<WorkspaceStateContent | null>(null);
  /** The chain on screen is the last answer, while the same question is read again. */
  readonly again = input(false);

  /** The `id` of a routeless action of the state the operator clicked. */
  readonly actionTriggered = output<string>();
}
