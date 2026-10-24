import { Injectable, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRouteSnapshot, NavigationEnd, Router } from '@angular/router';
import { filter, map, startWith } from 'rxjs/operators';

import { activeMenuNodeFor } from './app-menu';

/** Whether the active route or any route beneath it declares `key` in its data. */
export function routeTreeHasData(route: ActivatedRouteSnapshot, key: string): boolean {
  return Boolean(route.data[key]) || route.children.some((child) => routeTreeHasData(child, key));
}

/**
 * The post-redirect URL, as a signal, for shell surfaces that project from it.
 *
 * Every shell consumer reads the same subscription rather than rebuilding the
 * router pipeline, so the menubar, the document title, and the full-bleed
 * layout decision cannot disagree about which route is current.
 */
@Injectable({ providedIn: 'root' })
export class CurrentUrlService {
  private readonly router = inject(Router);
  private readonly navigationEnds = this.router.events.pipe(
    filter((event): event is NavigationEnd => event instanceof NavigationEnd),
  );

  readonly url = toSignal(
    this.navigationEnds.pipe(
      map((event) => event.urlAfterRedirects),
      startWith(this.router.url),
    ),
    { initialValue: this.router.url },
  );

  /**
   * The active route tree, renewed after every completed navigation.
   *
   * `url` cannot stand in for this. The app boots with `url` already at the
   * root address, so a first navigation that ends there leaves `url`
   * unchanged, and anything derived from it would keep the answer it computed
   * before any route was active.
   */
  readonly activeRoute = toSignal(
    this.navigationEnds.pipe(map(() => this.router.routerState.snapshot.root)),
    { initialValue: this.router.routerState.snapshot.root, equal: () => false },
  );

  /**
   * The URL of the page the router found, or `null` once it fell through to
   * the not-found page. A lost address such as `/research-lab/nothing` starts
   * with a real page's prefix, so the URL alone would name that page's group.
   */
  readonly pageUrl = computed(() => (routeTreeHasData(this.activeRoute(), 'notFound') ? null : this.url()));

  /** The menu node the found page belongs to, or `null` outside every group. */
  readonly menuNode = computed(() => {
    const url = this.pageUrl();
    return url === null ? null : activeMenuNodeFor(url);
  });
}
