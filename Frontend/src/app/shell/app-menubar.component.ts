import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router } from '@angular/router';
import { filter, map, startWith } from 'rxjs/operators';
import { Menubar } from 'primeng/menubar';

import { menuItemsFor } from './app-menu';

/**
 * Primary navigation for every route.
 *
 * Groups are triggers, never destinations — no group label navigates. The
 * ``MenuItem`` model is rebuilt per URL because PrimeNG carries no
 * URL-driven active concept; ``menuItemsFor`` owns that marking.
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
  styleUrl: './app-menubar.component.scss',
})
export class AppMenubarComponent {
  /**
   * Collapses to the overflow trigger below the width the row needs.
   * Seven triggers plus the wordmark and connection control measure ~1110px;
   * PrimeNG's 960px default would let the row overflow before collapsing.
   */
  protected readonly collapseBelow = '1150px';

  private readonly router = inject(Router);
  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((event): event is NavigationEnd => event instanceof NavigationEnd),
      map((event) => event.urlAfterRedirects),
      startWith(this.router.url),
    ),
    { initialValue: this.router.url },
  );

  protected readonly items = computed(() => menuItemsFor(this.currentUrl()));
}
