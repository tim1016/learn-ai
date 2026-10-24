import { computed, inject, resource, type Signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { inputsFromSavedRun } from '../../../components/strategy-lab/strategy-lab.models';
import { BacktestRunsService } from '../../../services/backtest-runs.service';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { readStateOf } from '../../read-state';
import { ExploreSession } from '../explore-session';
import { savedRunRoute } from '../../signal-strategy-tabs';
import { EXPLORE_RUN_PARAM, parseRunId } from '../explore-tabs';
import { handoffFromBacktest, type ExploreHandoff, type HandoffDestination } from './explore-handoff';

/** Where a handoff stands: reading its source, ready to fill a form, or refused with the reason. */
export type HandoffState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly handoff: ExploreHandoff }
  | { readonly kind: 'refused'; readonly state: WorkspaceStateContent };

const NO_RANGE = { symbol: '', from: '', to: '', resolution: 'minute' } as const;

function refused(happened: string, why: string, next: string): HandoffState {
  return { kind: 'refused', state: { kind: 'unavailable', happened, why, next } };
}

/**
 * The handoff a `?run=` names for this strategy's next tool, read from the saved
 * run's own record. It is `null` when the address names no run, and otherwise it
 * stands at loading, ready or refused: a run another strategy produced, a run
 * that does not exist and a read that failed are each refused in words, never
 * turned into a form for a different strategy. Call it from a field initializer.
 */
export function injectBacktestHandoff(
  destination: HandoffDestination,
  runParam: Signal<string | undefined>,
): Signal<HandoffState | null> {
  const session = inject(ExploreSession);
  const runs = inject(BacktestRunsService);
  const runId = computed(() => parseRunId(runParam()));
  const read = resource({
    params: () => runId() ?? undefined,
    loader: ({ params }) => firstValueFrom(runs.get(params)),
  });

  return computed<HandoffState | null>(() => {
    const raw = runParam();
    if (raw === undefined || raw === '') return null;
    const id = runId();
    if (id === null) {
      return refused(
        `"${raw}" is not a saved run's number.`,
        `${EXPLORE_RUN_PARAM}= names the backtest run to start from, and a run's number is a whole number above zero.`,
        'The form below starts empty.',
      );
    }
    const loaded = readStateOf(read);
    if (loaded.kind === 'loading') return { kind: 'loading' };
    if (loaded.kind === 'failed') {
      return refused(`Saved run #${id} could not be read.`, loaded.message, 'The form below starts empty; reload to try the run again.');
    }
    const run = loaded.value;
    if (run === null) {
      return refused(`Saved run #${id} was not found.`, 'The server holds no backtest run with that number.', 'The form below starts empty.');
    }
    const key = session.key();
    if (run.strategyName !== key) {
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: `Saved run #${id} belongs to ${run.strategyName}, not ${key}.`,
          why: 'A handoff carries a run only into the strategy that produced it.',
          next: 'The form below starts empty. The run opens where its record places it.',
          actions: [{ id: 'owner', label: `Open run #${id} under ${run.strategyName}`, route: savedRunRoute(id) }],
        },
      };
    }
    const catalog = session.catalog();
    const entry = session.entry();
    if (catalog.kind === 'loading' || entry === null) return { kind: 'loading' };
    if (catalog.kind === 'failed') {
      return refused(`Run #${id}'s settings cannot be matched to this strategy's declared settings.`, catalog.message, 'The form below starts empty.');
    }
    if (catalog.value === null) {
      return refused(
        `Strategy Lab's catalog does not offer ${key}.`,
        'Python declares no settings for it, so a run of it cannot be turned into a grid.',
        'The form below starts empty.',
      );
    }
    try {
      const inputs = inputsFromSavedRun(run, NO_RANGE);
      return { kind: 'ready', handoff: handoffFromBacktest({ run, inputs, strategy: catalog.value, entry, destination }) };
    } catch (error) {
      return refused(
        `Saved run #${id}'s settings could not be read.`,
        error instanceof Error ? error.message : 'The run recorded settings this page cannot read.',
        'The form below starts empty.',
      );
    }
  });
}
