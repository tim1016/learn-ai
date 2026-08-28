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

  readonly url = toSignal(
    this.router.events.pipe(
      filter((event): event is NavigationEnd => event instanceof NavigationEnd),
      map((event) => event.urlAfterRedirects),
      startWith(this.router.url),
    ),
    { initialValue: this.router.url },
  );
}
