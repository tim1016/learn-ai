import { Injectable, computed, inject, signal } from '@angular/core';

import type { EngineChoice, EngineResolution, StrategyInfo } from '../../components/strategy-lab/strategy-lab.models';
import type { ReadState } from '../read-state';
import type { StrategyRegistryEntry } from '../signal-strategy-evidence';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';

/** One setting as the workbench holds it now, and whether it differs from the schema's default. */
export interface ExploratoryParameter {
  readonly name: string;
  readonly value: unknown;
  readonly changedFromDefault: boolean;
}

/**
 * What the Backtest workbench is trying out right now, as Define reads it. These are
 * exploratory values: in-sample, never approved, and not a default or a corpus value.
 */
export interface ExploratoryValues {
  readonly symbol: string;
  readonly from: string;
  readonly to: string;
  readonly resolution: EngineResolution;
  readonly engine: EngineChoice;
  readonly parameters: readonly ExploratoryParameter[];
  /** The saved run these were restored from, when the workbench is showing one. */
  readonly restoredFromRunId: number | null;
}

/**
 * What Explore keeps for one strategy while the owner is in its place: provided
 * with the Strategy Lab stores by the strategy's Explore scope, so it lives and
 * ends with that strategy's key.
 */
@Injectable()
export class ExploreSession {
  private readonly workspace = inject(SignalStrategyWorkspaceStore);

  /** The registry key in the address. */
  readonly key = this.workspace.key;

  readonly entry = computed<StrategyRegistryEntry | null>(() => {
    const state = this.workspace.entry();
    return state.kind === 'found' ? state.entry : null;
  });

  /** The registry's answer on whether this strategy may be swept, which Sweep and Walk-forward share. */
  readonly sweepCapability = computed(() => this.entry()?.capabilities.sweep ?? null);

  /** The registry's answer on whether a golden search may be planned for this strategy, which the handoff links into Golden share. */
  readonly goldenCapability = computed(() => this.entry()?.capabilities.golden ?? null);

  /** Strategy Lab's catalog entry for this strategy: its declared parameters and sweep eligibility. */
  readonly catalog = computed<ReadState<StrategyInfo | null>>(() => {
    const state = this.workspace.declarations();
    return state.kind === 'ready' ? { kind: 'ready', value: state.value.schema } : state;
  });

  /** The saved backtest the Backtest tool last showed or produced, so its tab leads back to it. */
  readonly backtestRunId = signal<number | null>(null);

  /**
   * The saved backtest whose inputs the workbench last took over. The workbench
   * takes a run's inputs once, when the run is opened: coming back to the same
   * run from another tool must not overwrite what was edited since.
   */
  readonly adoptedBacktestRunId = signal<number | null>(null);

  /** The resumed backtest whose inputs the workbench last described on its rail, for the same reason. */
  readonly describedJobId = signal<string | null>(null);

  /** What the workbench is trying out, or `null` while nothing has been tried in this place. */
  readonly exploratory = signal<ExploratoryValues | null>(null);

  /** Asks for the catalog read. Asking twice reads once. */
  wantCatalog(): void {
    this.workspace.want('declarations');
  }

  retryCatalog(): void {
    this.workspace.retry('declarations');
  }
}
