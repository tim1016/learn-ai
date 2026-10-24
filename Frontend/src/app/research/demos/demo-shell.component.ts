import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { routePathOf } from '../../fleet/account-workspace';
import { menuLabelFor } from '../../shell/app-menu';
import { CurrentUrlService } from '../../shell/current-url.service';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import { RESEARCH_DEMOS } from '../research-studies';

/**
 * The header over the four Edge demonstrations (PRD #3490 R28): the page's own
 * name and what it shows. Each demonstration draws its own sample-data label
 * from the provenance of the data it renders, so this adds none and no
 * demonstration can borrow the label of its neighbour.
 *
 * Presentation only; it reads no data.
 */
@Component({
  selector: 'app-research-demo-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet, WorkspaceHeaderComponent],
  template: `
    <app-workspace-header [title]="title()" [purpose]="purpose()" />
    <div class="demo-body"><router-outlet /></div>
  `,
  styles: `
    :host { display: block; }
    .demo-body { padding: 0 var(--space-4) var(--space-8); }
  `,
})
export class ResearchDemoShellComponent {
  private readonly currentUrl = inject(CurrentUrlService).url;

  private readonly demo = computed(
    () => RESEARCH_DEMOS.find((candidate) => candidate.route === routePathOf(this.currentUrl())) ?? null,
  );
  protected readonly title = computed(() => {
    const demo = this.demo();
    return demo === null ? 'Demonstration' : menuLabelFor(demo.route);
  });
  protected readonly purpose = computed(() => this.demo()?.summary ?? null);
}
