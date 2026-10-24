import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { GoldenValidationWorkbenchComponent } from '../../components/golden-validation-workbench/golden-validation-workbench.component';
import { BacktestRunsService } from '../../services/backtest-runs.service';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { exploreTabRoute, parseRunId } from '../explore/explore-tabs';
import { readStateOf } from '../read-state';
import { parameterPairs } from '../signal-strategy-evidence';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { GOLDEN_RUN_PARAM, goldenTabRoute } from './golden-tabs';

/** What `?run=` is: not asked, being read, refused in words, or one of this strategy's own saved runs. */
type RunState =
  | { readonly kind: 'none' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'refused'; readonly state: WorkspaceStateContent }
  | { readonly kind: 'own'; readonly runId: number };

/**
 * Golden → Configuration review (PRD #3490 R18, ADR 0061, ADR 0085): the exact-
 * configuration human review of a selected baseline run. It is the workbench
 * Strategy Validation and Backtest → History used to embed, now the only place a
 * configuration is reviewed, so Explore never approves settings.
 *
 * A baseline run is selected for one strategy: a `?run=` that another strategy
 * produced, that does not exist or that cannot be read is refused in words and
 * never offered for selection under this strategy's header. The registry's own
 * corpus point is shown apart, because a corpus point is neither a selected
 * baseline run nor a Golden Search version. The classifications (Engine
 * agreement, Reviewed deviations, Manual override), their required notes and the
 * non-overridable evidence rules are the workbench's own, unchanged.
 */
@Component({
  selector: 'app-golden-configuration',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [GoldenValidationWorkbenchComponent, RouterLink, WorkspaceStateBlockComponent],
  templateUrl: './golden-configuration.component.html',
  styleUrl: './golden-configuration.component.scss',
})
export class GoldenConfigurationComponent {
  private readonly store = inject(SignalStrategyWorkspaceStore);
  private readonly runs = inject(BacktestRunsService);

  /** The saved backtest to select as the baseline run (`?run=`). */
  readonly run = input<string | undefined>(undefined);
  /** The selected baseline run's review to open first (`?case=`). */
  readonly case = input<string | undefined>(undefined);

  protected readonly key = this.store.key;
  protected readonly backtestRoute = computed(() => exploreTabRoute(this.key(), 'backtest'));
  protected readonly historyQuery = { tab: 'history' } as const;

  /** The registry's own declaration of the point its program was validated at. */
  protected readonly corpusPoint = computed(() => {
    const state = this.store.entry();
    if (state.kind !== 'found') return null;
    return {
      symbols: state.entry.validated_symbols,
      pairs: parameterPairs(state.entry.validated_settings),
    };
  });

  protected readonly caseId = computed(() => parseRunId(this.case()));

  private readonly runId = computed(() => parseRunId(this.run()));
  private readonly runRead = resource({
    params: () => this.runId() ?? undefined,
    loader: ({ params }) => firstValueFrom(this.runs.get(params)),
  });

  protected readonly runState = computed<RunState>(() => {
    const raw = this.run();
    if (raw === undefined || raw === '') return { kind: 'none' };
    const id = this.runId();
    if (id === null) {
      return {
        kind: 'refused',
        state: {
          kind: 'invalid',
          happened: `"${raw}" is not a saved run's number.`,
          why: `${GOLDEN_RUN_PARAM}= names the backtest run to select as the baseline run, and a run's number is a whole number above zero.`,
          next: 'Open the strategy’s Backtest history and select a run from there.',
          actions: [{ id: 'history', label: 'Open Backtest history', route: [...exploreTabRoute(this.key(), 'backtest')], queryParams: { tab: 'history' } }],
        },
      };
    }
    const read = readStateOf(this.runRead);
    if (read.kind === 'loading') return { kind: 'loading' };
    if (read.kind === 'failed') {
      return {
        kind: 'refused',
        state: {
          kind: 'failed',
          happened: `Saved run #${id} could not be read.`,
          why: read.message,
          next: 'A run can be selected as the baseline run only once its strategy is known. Reload to try again.',
        },
      };
    }
    const found = read.value;
    if (found === null) {
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: `Saved run #${id} was not found.`,
          why: 'The server holds no backtest run with that number.',
          next: 'Open the strategy’s Backtest history and select a run from there.',
        },
      };
    }
    if (found.strategyName !== this.key()) {
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: `Saved run #${id} belongs to ${found.strategyName}, not ${this.key()}.`,
          why: 'A run is selected as a baseline only for the strategy that produced it, so this page offers none of its configuration.',
          next: `Review it under ${found.strategyName}.`,
          actions: [
            {
              id: 'owner',
              label: `Open run #${id} under ${found.strategyName}`,
              route: [...goldenTabRoute(found.strategyName, 'configuration')],
              queryParams: { [GOLDEN_RUN_PARAM]: String(id) },
            },
          ],
        },
      };
    }
    return { kind: 'own', runId: id };
  });

  protected readonly sourceRunId = computed(() => {
    const state = this.runState();
    return state.kind === 'own' ? state.runId : null;
  });
}
