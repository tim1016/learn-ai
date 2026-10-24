import { LowerCasePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { BotHistoryService } from '../components/brokers/alpaca-history/bot-history.service';
import { GoldenSearchService, QUALIFICATION_LIST_LIMIT } from '../components/golden-search/golden-search.service';
import { StrategyValidationService } from '../services/strategy-validation.service';
import type { StrategyValidationSummary } from '../services/strategy-validation.types';
import { AssetIdentityComponent } from '../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../shared/timestamp/timestamp-display.component';
import type { WorkspaceAction } from '../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent } from '../shared/workspace/workspace-header.component';
import type { WorkspaceStateContent } from '../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../shared/workspace/workspace-state-block.component';
import { menuLabelFor } from '../shell/app-menu';
import { readStateOf, type ReadState } from './read-state';
import { SignalStrategyCapabilitiesComponent } from './signal-strategy-capabilities.component';
import { SignalStrategyChipComponent } from './signal-strategy-chip.component';
import {
  PROOF_TONES,
  proofSummaryOf,
  readRunningFleet,
  runningBotsFor,
  versionsBySymbol,
  type FleetRunningRead,
} from './signal-strategy-evidence';
import { SignalStrategyRegistryService } from './signal-strategy-registry.service';
import { SIGNAL_STRATEGIES_RECENCY_ROUTE, signalStrategyTabRoute } from './signal-strategy-tabs';

type ReadName = 'proof' | 'versions' | 'running';

const READ_NAMES: Readonly<Record<ReadName, string>> = {
  proof: 'Program proof',
  versions: 'Qualified versions',
  running: 'Running bots',
};

/**
 * Every strategy in the engine registry, each with what the registry allows
 * for it and the evidence that exists, one fact per column. Registered is only
 * registered: a row never says a strategy is deployable or approved, and no
 * column sums a symbol's approval into the strategy. Each evidence read stands
 * alone, so one that failed turns only its own column to unknown.
 */
@Component({
  selector: 'app-signal-strategies-list-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    LowerCasePipe,
    ReceiptLabelPipe,
    RouterLink,
    SignalStrategyCapabilitiesComponent,
    SignalStrategyChipComponent,
    TimestampDisplayComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './signal-strategies-list-page.component.html',
  styleUrl: './signal-strategies-list-page.component.scss',
})
export class SignalStrategiesListPageComponent {
  private readonly registry = inject(SignalStrategyRegistryService);
  private readonly validation = inject(StrategyValidationService);
  private readonly goldenSearch = inject(GoldenSearchService);
  private readonly botHistory = inject(BotHistoryService);

  private readonly registryRead = resource({ loader: () => this.registry.entries() });
  private readonly proofRead = resource({ loader: () => this.validation.getCatalog() });
  private readonly qualificationsRead = resource({ loader: () => this.goldenSearch.listQualifications() });
  private readonly fleetRead = resource({
    loader: () =>
      readRunningFleet((page) =>
        this.botHistory.read({ account: null, status: 'running', world: null, symbol: null, bot: null, page }),
      ),
  });

  protected readonly title = menuLabelFor('/signal-strategies');
  protected readonly purpose =
    'Every strategy in the engine registry. Registered does not mean proven, approved or deployable: each row says what the registry allows and what evidence exists.';
  protected readonly headerActions: readonly WorkspaceAction[] = [
    { id: 'recency', label: 'Recency chart', route: [SIGNAL_STRATEGIES_RECENCY_ROUTE] },
  ];
  protected readonly proofTones = PROOF_TONES;
  protected readonly listLimit = QUALIFICATION_LIST_LIMIT;

  private readonly registryState = computed(() => readStateOf(this.registryRead));
  private readonly proofState = computed(() => readStateOf(this.proofRead));
  private readonly versionsState = computed(() => readStateOf(this.qualificationsRead));
  protected readonly fleetState = computed<ReadState<FleetRunningRead>>(() => readStateOf(this.fleetRead));

  /** The list itself, or what is in its place while the registry is read or if it cannot be. */
  protected readonly registryNotice = computed<WorkspaceStateContent | null>(() => {
    const state = this.registryState();
    if (state.kind === 'ready') return null;
    if (state.kind === 'loading') return { kind: 'checking', happened: 'Reading the strategy registry.' };
    return {
      kind: 'failed',
      happened: 'The strategy registry could not be read.',
      why: state.message,
      next: 'No strategy is listed until it can be.',
      actions: [{ id: 'registry', label: 'Read it again' }],
    };
  });

  /** One notice for each evidence read that failed, so its column's "unknown" has a reason. */
  protected readonly failedReads = computed(() => {
    const reads: readonly (readonly [ReadName, ReadState<unknown>])[] = [
      ['proof', this.proofState()],
      ['versions', this.versionsState()],
      ['running', this.fleetState()],
    ];
    return reads.flatMap(([name, state]) =>
      state.kind === 'failed'
        ? [
            {
              name,
              content: {
                kind: 'failed',
                happened: `${READ_NAMES[name]} could not be read.`,
                why: state.message,
                next: 'Its column says unknown until the read succeeds; no count is shown as none.',
                actions: [{ id: name, label: 'Read it again' }],
              } satisfies WorkspaceStateContent,
            },
          ]
        : [],
    );
  });

  protected readonly rows = computed(() => {
    const registry = this.registryState();
    if (registry.kind !== 'ready') return [];
    const proof = this.proofState();
    const versions = this.versionsState();
    const fleet = this.fleetState();
    return registry.value.map((entry) => ({
      entry,
      overview: signalStrategyTabRoute(entry.strategy_key, 'overview'),
      proof: this.proofCell(proof, entry.strategy_key),
      versions:
        versions.kind === 'ready'
          ? { kind: 'known' as const, bySymbol: versionsBySymbol(entry.strategy_key, versions.value) }
          : { kind: versions.kind },
      running: fleet.kind === 'ready' ? runningBotsFor(entry.strategy_key, fleet.value) : runningBotsFor(entry.strategy_key, null),
      runningRead: fleet.kind,
    }));
  });

  /** The versions read returns the newest 500; one that long may have left a strategy's versions out. */
  protected readonly versionsMayBeCut = computed(() => {
    const state = this.versionsState();
    return state.kind === 'ready' && state.value.length >= QUALIFICATION_LIST_LIMIT;
  });

  protected readonly fleetGaps = computed(() => {
    const state = this.fleetState();
    return state.kind === 'ready' ? state.value.gaps.length : 0;
  });

  protected readonly fleetObservedAtMs = computed(() => {
    const state = this.fleetState();
    return state.kind === 'ready' ? state.value.observedAtMs : null;
  });

  protected retry(target: 'registry' | ReadName): void {
    switch (target) {
      case 'registry':
        this.registryRead.reload();
        break;
      case 'proof':
        this.proofRead.reload();
        break;
      case 'versions':
        this.qualificationsRead.reload();
        break;
      case 'running':
        this.fleetRead.reload();
        break;
    }
  }

  protected onRetry(actionId: string): void {
    if (actionId === 'registry' || actionId === 'proof' || actionId === 'versions' || actionId === 'running') {
      this.retry(actionId);
    }
  }

  private proofCell(state: ReadState<{ readonly strategies: readonly StrategyValidationSummary[] }>, strategyKey: string) {
    if (state.kind !== 'ready') return { kind: state.kind } as const;
    const record = state.value.strategies.find((strategy) => strategy.strategy_key === strategyKey);
    return record === undefined ? ({ kind: 'norecord' } as const) : ({ kind: 'known', state: proofSummaryOf(record).state } as const);
  }
}
