import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { OptionArchiveCardComponent } from '../../shared/option-archive/option-archive-card.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { CurrentUrlService } from '../../shell/current-url.service';

/**
 * What is left of the Data Lab shell: the option-archive card above the one
 * page still under `/data-lab` (Validate). Everything else it hosted — the
 * scope bar, the chart, the Returns study, Build dataset and the saved
 * setups — lives under the instrument it is for (ADR 0084), and this goes
 * when Operations takes the card and Validate (#3498, #3499).
 */
@Component({
  selector: 'app-data-lab',
  imports: [OptionArchiveCardComponent, RouterOutlet, PageHeaderComponent],
  template: `
    <app-page-header [title]="pageTitle()" icon="pi-database" />
    <app-option-archive-card />
    <section class="dl-shell__content" aria-label="Data Lab">
      <router-outlet />
    </section>
  `,
  styleUrl: './data-lab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabComponent {
  /** The page is titled by the menu entry that leads to it (PRD #3490 R01). */
  private readonly menuNode = inject(CurrentUrlService).menuNode;
  protected readonly pageTitle = computed(() => this.menuNode()?.item.title ?? 'Validate');
}
