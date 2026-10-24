import type { ResolveFn } from '@angular/router';

import { lazyResolver } from '../../explore/entry/explore-entry.lazy';
import type { GoldenEntry, GoldenEntryResolver } from './golden-entry';

/** A resolver from `golden-entry.resolvers`, loaded when its address is first visited. */
export function lazyGoldenEntryResolver(
  pick: (resolvers: typeof import('./golden-entry.resolvers')) => GoldenEntryResolver,
): ResolveFn<GoldenEntry> {
  return lazyResolver(() => import('./golden-entry.resolvers'), pick);
}
