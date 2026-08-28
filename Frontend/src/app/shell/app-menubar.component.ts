import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { Menubar } from 'primeng/menubar';

import { CurrentUrlService } from './current-url.service';
import { menuItemsFor } from './app-menu';

/**
 * Primary navigation for every route.
 *
 * Groups are triggers, never destinations — no group label navigates. The
 * ``MenuItem`` model is rebuilt per URL because PrimeNG carries no URL-driven
 * active concept: it applies its own ``routerLinkActive`` marking with
 * ``{ exact: false }``, which would light up ``/edge`` while you are on
 * ``/edge/regimes`` and both Accounts and Deploy on ``/brokers/alpaca``.
 * ``menuItemsFor`` owns the marking instead, via the same longest-match
 * resolver the rest of the shell uses. PrimeNG's class is emitted but
 * unstyled — leave it that way.
 */
@Component({
  selector: 'app-menubar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Menubar],
  template: `
    <nav aria-label="Primary">
      <p-menubar [model]="items()" [breakpoint]="collapseBelow" />
    </nav>
  `,
  styles: [':host { display: block; min-width: 0; }'],
})
export class AppMenubarComponent {
  /**
   * Collapses to the overflow trigger below the width the row needs.
   * Seven triggers plus the wordmark and connection control measure ~1130px;
   * PrimeNG's 960px default would let the row overflow before collapsing.
   */
  protected readonly collapseBelow = '1150px';

  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly items = computed(() => menuItemsFor(this.currentUrl()));
}
