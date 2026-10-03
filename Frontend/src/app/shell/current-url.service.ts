import { Injectable, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router } from '@angular/router';
import { filter, map, startWith } from 'rxjs/operators';

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
}
