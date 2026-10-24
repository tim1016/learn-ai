import { EnvironmentInjector, inject, runInInjectionContext } from '@angular/core';
import type { ResolveFn } from '@angular/router';

import type { ExploreEntry, ExploreEntryResolver } from './explore-entry';

/**
 * A resolver from `explore-entry.resolvers`, loaded when its address is first
 * visited. The resolvers read services the app's first screen never needs, so
 * the route table keeps only this thin wrapper in the main bundle. The injector
 * is taken before the import: `inject()` works only until the first `await`.
 */
export function lazyEntryResolver(
  pick: (resolvers: typeof import('./explore-entry.resolvers')) => ExploreEntryResolver,
): ResolveFn<ExploreEntry> {
  return (route) => {
    const injector = inject(EnvironmentInjector);
    return import('./explore-entry.resolvers').then((resolvers) =>
      runInInjectionContext(injector, () => pick(resolvers)(route)),
    );
  };
}
