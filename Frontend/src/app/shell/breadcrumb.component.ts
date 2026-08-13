import { ChangeDetectionStrategy, Component, computed, inject, viewChild } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink } from '@angular/router';
import { filter, map, startWith } from 'rxjs/operators';

import { breadcrumbTrailFor, type Crumb } from './app-menu';

/** Current-location projection of the canonical application menu. */
@Component({
  selector: 'app-breadcrumb',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (crumbs().length > 0) {
      <nav class="breadcrumb" aria-label="Breadcrumb">
        @if (ancestorCrumbs().length > 0) {
          <details #overflow class="breadcrumb__overflow">
            <summary aria-label="Show earlier breadcrumbs"><span aria-hidden="true">…</span></summary>
            <div class="breadcrumb__overflow-menu">
              @for (crumb of ancestorCrumbs(); track crumb.route + crumb.label) {
                <a
                  [routerLink]="crumb.route"
                  [queryParams]="crumb.queryParams"
                  [queryParamsHandling]="queryParamsHandlingFor(crumb)"
                  (click)="closeOverflow()"
                >{{ crumb.label }}</a>
              }
            </div>
          </details>
        }
        @for (crumb of crumbs(); track crumb.route + crumb.label; let last = $last) {
          @if (!$first) {
            <span class="breadcrumb__separator" [class.breadcrumb__separator--before-last]="last" aria-hidden="true">›</span>
          }
          <a
            class="breadcrumb__crumb"
            [class.breadcrumb__crumb--ancestor]="!last"
            [routerLink]="crumb.route"
            [queryParams]="crumb.queryParams"
            [queryParamsHandling]="queryParamsHandlingFor(crumb)"
            [attr.aria-current]="isCurrentPath(crumb) ? 'page' : null"
            (click)="closeOverflow()"
          >{{ crumb.label }}</a>
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
  readonly ancestorCrumbs = computed(() => this.crumbs().slice(0, -1));
  private readonly overflow = viewChild<HTMLDetailsElement>('overflow');

  protected isCurrentPath(crumb: Crumb): boolean {
    return pathOf(this.currentUrl()) === crumb.route;
  }

  protected queryParamsHandlingFor(crumb: Crumb): 'merge' | undefined {
    return this.isCurrentPath(crumb) ? 'merge' : undefined;
  }

  protected closeOverflow(): void {
    const overflow = this.overflow();
    if (overflow !== undefined) overflow.open = false;
  }
}

function pathOf(url: string): string {
  return url.split(/[?#]/, 1)[0];
}
