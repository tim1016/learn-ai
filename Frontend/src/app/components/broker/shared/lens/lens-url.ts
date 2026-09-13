import type { NavigationExtras } from '@angular/router';

import { LENS_QUERY_PARAM, type DeskLens } from './lens';

/**
 * Typed URL helpers for lens switches on routed hosts.
 *
 * A lens switch replaces the current URL entry (`replaceUrl: true`) while
 * merging, so it neither pollutes browser history nor drops unrelated query
 * parameters — a deep link into a specific timeline, ticket, or deploy drawer
 * keeps working when the operator changes perspective. The `lens` parameter
 * itself is always overwritten with the chosen value.
 */
export function lensNavigationExtras(lens: DeskLens): NavigationExtras {
  return {
    queryParams: { [LENS_QUERY_PARAM]: lens },
    queryParamsHandling: 'merge',
    replaceUrl: true,
  };
}
