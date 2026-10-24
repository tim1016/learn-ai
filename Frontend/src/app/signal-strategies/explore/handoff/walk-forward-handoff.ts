import { HttpErrorResponse } from '@angular/common/http';
import { computed, inject, resource, type Signal } from '@angular/core';

import { WalkForwardStudyService } from '../../../components/walk-forward-study/walk-forward-study.service';
import { readStateOf } from '../../read-state';
import { ExploreSession } from '../explore-session';
import { exploreTabRoute } from '../explore-tabs';
import type { HandoffState } from './backtest-handoff';
import { handoffFromWalkForward } from './explore-handoff';

function refused(happened: string, why: string, next: string): HandoffState {
  return { kind: 'refused', state: { kind: 'unavailable', happened, why, next } };
}

/**
 * The handoff a `?walkForward=` names for a golden search plan, read from the
 * saved walk-forward study's own request. `null` when the address names no
 * study; otherwise loading, ready or refused in words (a study another strategy
 * ran, one that does not exist, a read that failed). Call it from a field
 * initializer.
 */
export function injectWalkForwardHandoff(studyParam: Signal<string | undefined>): Signal<HandoffState | null> {
  const session = inject(ExploreSession);
  const studies = inject(WalkForwardStudyService);
  const read = resource({
    params: () => studyParam() || undefined,
    loader: ({ params }) => studies.get(params),
  });

  return computed<HandoffState | null>(() => {
    const id = studyParam();
    if (!id) return null;
    const loaded = readStateOf(read);
    if (loaded.kind === 'loading') return { kind: 'loading' };
    if (loaded.kind === 'failed') {
      const failure: unknown = read.error();
      const gone = failure instanceof HttpErrorResponse && failure.status === 404;
      return refused(
        gone ? `Walk-forward study ${id} was not found.` : `Walk-forward study ${id} could not be read.`,
        gone ? 'The server holds no study with that id.' : loaded.message,
        'The plan below starts from the declared defaults.',
      );
    }
    const study = loaded.value;
    const key = session.key();
    if (study.strategy_key !== key) {
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: `Walk-forward study ${id.slice(0, 8)} belongs to ${study.strategy_key}, not ${key}.`,
          why: 'A handoff carries a study only into the strategy that ran it.',
          next: 'The plan below starts from the declared defaults. The study opens under its own strategy’s Walk-forward history.',
          actions: [{ id: 'owner', label: `Open ${study.strategy_key} Walk-forward`, route: exploreTabRoute(study.strategy_key, 'walk-forward') }],
        },
      };
    }
    const catalog = session.catalog();
    const entry = session.entry();
    if (catalog.kind === 'loading' || entry === null) return { kind: 'loading' };
    if (catalog.kind === 'failed') {
      return refused("This strategy's declared settings could not be read.", catalog.message, 'The plan below starts from the declared defaults.');
    }
    return { kind: 'ready', handoff: handoffFromWalkForward({ study, strategy: catalog.value, entry }) };
  });
}
