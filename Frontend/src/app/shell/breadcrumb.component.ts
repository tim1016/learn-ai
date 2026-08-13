import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink } from '@angular/router';
import { filter, map, startWith } from 'rxjs/operators';

import { breadcrumbTrailFor } from './app-menu';

/** Current-location projection of the canonical application menu. */
@Component({
  selector: 'app-breadcrumb',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (crumbs().length > 0) {
      <nav class="breadcrumb" aria-label="Breadcrumb">
        @for (crumb of crumbs(); track crumb.route + crumb.title; let last = $last) {
          @if (!$first) {
            <span class="breadcrumb__separator" [class.breadcrumb__separator--before-last]="last" aria-hidden="true">›</span>
          }
          <a
            class="breadcrumb__crumb"
            [routerLink]="crumb.route"
            [queryParams]="crumb.queryParams"
          >{{ crumb.title }}</a>
        }
      </nav>
    }
  `,
  styleUrl: './breadcrumb.component.scss',
})
export class BreadcrumbComponent {
  private readonly router = inject(Router);
  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((event): event is NavigationEnd => event instanceof NavigationEnd),
      map((event) => event.urlAfterRedirects),
      startWith(this.router.url),
    ),
    { initialValue: this.router.url },
  );

  readonly crumbs = computed(() => breadcrumbTrailFor(this.currentUrl()));
}
