import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { EngineRunDockSource } from '../../components/lean-engine/engine-run-dock-source';
import { StrategyLabConfigStore, STRATEGY_LAB_PINNED_STRATEGY } from '../../components/strategy-lab/strategy-lab-config.store';
import { StrategyLabRunReport } from '../../components/strategy-lab/strategy-lab-run-report.service';
import { StrategyLabRunner } from '../../components/strategy-lab/strategy-lab-runner.service';
import { RUN_DOCK_SOURCE, RUN_DOCK_STORAGE_KEY } from '../../shared/run-dock/run-dock-source';
import { ExploreSession } from './explore-session';

/**
 * Everything Explore keeps for one strategy, held above its tabs: the Strategy
 * Lab workbench's configuration, runner and report, the run dock's source, and
 * the session Define reads. A tab page asks for them and never owns them, so a
 * backtest in flight is the same backtest whichever tab is open, and moving
 * between Backtest, Sweep, Walk-forward or Define never builds a second runner.
 *
 * The workspace puts one of these around its tab pages per registry key, so
 * opening another strategy's address starts that strategy's state afresh and
 * never shows the last strategy's. The stores are created when a page first
 * injects them; a strategy whose Explore is never opened reads nothing.
 */
@Component({
  selector: 'app-explore-scope',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet],
  providers: [
    ExploreSession,
    StrategyLabConfigStore,
    StrategyLabRunner,
    StrategyLabRunReport,
    EngineRunDockSource,
    { provide: RUN_DOCK_SOURCE, useExisting: EngineRunDockSource },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: 'run-dock-expanded:strategy-lab' },
    { provide: STRATEGY_LAB_PINNED_STRATEGY, useFactory: () => inject(ExploreSession).key },
  ],
  template: '<router-outlet />',
  styles: ':host { display: block; min-width: 0; }',
})
export class ExploreScopeComponent {}
