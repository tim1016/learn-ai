import { HttpErrorResponse } from '@angular/common/http';
import { computed, inject, resource, type Signal } from '@angular/core';

import { GridSearchService } from '../../../components/grid-search/grid-search.service';
import { readStateOf } from '../../read-state';
import { ExploreSession } from '../explore-session';
import { EXPLORE_SEARCH_PARAM, exploreTabRoute } from '../explore-tabs';
import { handoffFromSweep, type HandoffDestination } from './explore-handoff';
import type { HandoffState } from './backtest-handoff';

function refused(happened: string, why: string, next: string): HandoffState {
  return { kind: 'refused', state: { kind: 'unavailable', happened, why, next } };
}

/**
 * The handoff a `?search=` names for Walk-forward or a golden search plan, read
 * from the saved Grid Search's own request. `null` when the address names no
 * search; otherwise loading, ready or refused in words (a search another
 * strategy ran, one that does not exist, a read that failed). Call it from a
 * field initializer.
 */
export function injectSweepHandoff(
  searchParam: Signal<string | undefined>,
  destination: HandoffDestination = 'walk-forward',
): Signal<HandoffState | null> {
  const session = inject(ExploreSession);
  const searches = inject(GridSearchService);
  const read = resource({
    params: () => searchParam() || undefined,
    loader: ({ params }) => searches.get(params),
  });

  return computed<HandoffState | null>(() => {
    const id = searchParam();
    if (!id) return null;
    const loaded = readStateOf(read);
    if (loaded.kind === 'loading') return { kind: 'loading' };
    if (loaded.kind === 'failed') {
      const failure: unknown = read.error();
      const gone = failure instanceof HttpErrorResponse && failure.status === 404;
      return refused(
        gone ? `Grid search ${id} was not found.` : `Grid search ${id} could not be read.`,
        gone ? 'The server holds no search with that id.' : loaded.message,
        'The form below starts empty.',
      );
    }
    const search = loaded.value;
    const key = session.key();
    if (search.strategy_key !== key) {
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: `Grid search ${id.slice(0, 8)} belongs to ${search.strategy_key}, not ${key}.`,
          why: 'A handoff carries a search only into the strategy that ran it.',
          next: 'The form below starts empty. The search opens under its own strategy.',
          actions: [
            {
              id: 'owner',
              label: `Open it under ${search.strategy_key}`,
              route: exploreTabRoute(search.strategy_key, 'sweep'),
              queryParams: { [EXPLORE_SEARCH_PARAM]: id },
            },
          ],
        },
      };
    }
    const catalog = session.catalog();
    const entry = session.entry();
    if (catalog.kind === 'loading' || entry === null) return { kind: 'loading' };
    if (catalog.kind === 'failed') {
      return refused("This strategy's declared settings could not be read.", catalog.message, 'The form below starts empty.');
    }
    return { kind: 'ready', handoff: handoffFromSweep({ search, strategy: catalog.value, entry, destination }) };
  });
}
