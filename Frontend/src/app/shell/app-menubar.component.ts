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
   * Collapses to the overflow trigger below the width the header holds the
   * menu in. The six group names (Signal strategies and Options setups are
   * the long ones) need more than the half of the header beside the centered
   * brand until ~1750px, so above this width they wrap onto two lines; the
   * header's height holds both. PrimeNG's 960px default would let that
   * wrapped row overflow the header before collapsing.
   */
  protected readonly collapseBelow = '1150px';

  private readonly pageUrl = inject(CurrentUrlService).pageUrl;

  protected readonly items = computed(() => menuItemsFor(this.pageUrl()));
}
