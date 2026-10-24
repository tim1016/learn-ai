import { Injectable, computed, inject, resource, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';
import { firstValueFrom, map } from 'rxjs';

import { BotHistoryService } from '../components/brokers/alpaca-history/bot-history.service';
import { GoldenSearchService, QUALIFICATION_LIST_LIMIT } from '../components/golden-search/golden-search.service';
import type { StrategyCapability } from '../components/golden-search/golden-search.types';
import type { StrategyInfo } from '../components/strategy-lab/strategy-lab.models';
import { GoldenValidationService } from '../services/golden-validation.service';
import type { GoldenValidation } from '../services/golden-validation.types';
import { StrategyValidationService } from '../services/strategy-validation.service';
import type { StrategyValidationSummary } from '../services/strategy-validation.types';
import { combineReads, readStateOf, type ReadState } from './read-state';
import {
  readRunningFleet,
  runningBotsFor,
  versionsBySymbol,
  type FleetRunningRead,
  type RunningBotsAnswer,
  type StrategyRegistryEntry,
  type SymbolVersions,
} from './signal-strategy-evidence';
import { SignalStrategyRegistryService } from './signal-strategy-registry.service';

/** What the registry says about the key the address names. */
export type StrategyEntryState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed'; readonly message: string }
  | { readonly kind: 'unknown'; readonly registered: number }
  | { readonly kind: 'found'; readonly entry: StrategyRegistryEntry };

/** A strategy's qualified versions: by symbol, and whether the read may have been cut short. */
export interface QualifiedVersions {
  readonly bySymbol: readonly SymbolVersions[];
  readonly mayBeCut: boolean;
}

/**
 * What Python declares about a strategy's parameters. `schema` is `null` for an
 * entry Strategy Lab's picker does not offer; `golden` is `null` when no
 * Golden Search declaration exists for it.
 */
export interface StrategyDeclarations {
  readonly schema: StrategyInfo | null;
  readonly golden: StrategyCapability | null;
}

/** The reads a tab asks for on first need. They load once and stay for the workspace. */
export type EvidencePart = 'approvals' | 'fleet' | 'declarations';

/** What a section can retry: a part, or the two reads every tab shows. */
export type RetryTarget = EvidencePart | 'registry' | 'proof';

function mapRead<A, B>(state: ReadState<A>, convert: (value: A) => B): ReadState<B> {
  return state.kind === 'ready' ? { kind: 'ready', value: convert(state.value) } : state;
}

/**
 * One strategy's workspace: the registry entry its address names, and the
 * evidence each tab reads about it. Provided by the workspace shell, so the
 * reads survive moving between tabs and end with the workspace. A tab asks for
 * its reads with `want`; nothing is read for a tab nobody opened.
 *
 * Every read keeps its own state. A failed one says so where its section
 * stands and never stands in for an empty answer.
 */
@Injectable()
export class SignalStrategyWorkspaceStore {
  private readonly route = inject(ActivatedRoute);
  private readonly registry = inject(SignalStrategyRegistryService);
  private readonly validation = inject(StrategyValidationService);
  private readonly goldenSearch = inject(GoldenSearchService);
  private readonly goldenValidations = inject(GoldenValidationService);
  private readonly botHistory = inject(BotHistoryService);

  /** The registry key in the address. */
  readonly key = toSignal(this.route.paramMap.pipe(map((params) => params.get('key') ?? '')), {
    initialValue: this.route.snapshot.paramMap.get('key') ?? '',
  });

  private readonly wanted = signal<ReadonlySet<EvidencePart>>(new Set());
  // One boolean each, so a read's params change when its own part is first asked
  // for and not when another part is: a resource reloads whenever a signal its
  // params read changes, even to the same value.
  private readonly approvalsWanted = computed(() => this.wanted().has('approvals'));
  private readonly fleetWanted = computed(() => this.wanted().has('fleet'));
  private readonly declarationsWanted = computed(() => this.wanted().has('declarations'));

  private readonly registryRead = resource({ loader: () => this.registry.entries() });
  private readonly proofRead = resource({ loader: () => this.validation.getCatalog() });
  private readonly qualificationsRead = resource({
    params: () => (this.approvalsWanted() ? this.key() : undefined),
    loader: ({ params }) => this.goldenSearch.listQualifications(params),
  });
  private readonly reviewsRead = resource({
    params: () => (this.approvalsWanted() ? this.key() : undefined),
    loader: ({ params }) => firstValueFrom(this.goldenValidations.list({ strategyName: params })),
  });
  private readonly fleetRead = resource({
    params: () => (this.fleetWanted() ? {} : undefined),
    loader: () =>
      readRunningFleet((page) =>
        this.botHistory.read({ account: null, status: 'running', world: null, symbol: null, bot: null, page }),
      ),
  });
  private readonly schemasRead = resource({
    params: () => (this.declarationsWanted() ? {} : undefined),
    loader: () => this.registry.parameterSchemas(),
  });
  private readonly goldenCapabilitiesRead = resource({
    params: () => (this.declarationsWanted() ? {} : undefined),
    loader: () => this.goldenSearch.capabilities(),
  });

  /** A tab asks for the reads it shows. Asking twice reads once. */
  want(part: EvidencePart): void {
    this.wanted.update((current) => (current.has(part) ? current : new Set([...current, part])));
  }

  readonly entry = computed<StrategyEntryState>(() => {
    const state = readStateOf(this.registryRead);
    if (state.kind !== 'ready') return state;
    const entry = state.value.find((candidate) => candidate.strategy_key === this.key());
    return entry === undefined ? { kind: 'unknown', registered: state.value.length } : { kind: 'found', entry };
  });

  /** Every key the registry lists, once the registry has been read. */
  readonly registeredKeys = computed<ReadonlySet<string> | null>(() => {
    const state = readStateOf(this.registryRead);
    return state.kind === 'ready' ? new Set(state.value.map((candidate) => candidate.strategy_key)) : null;
  });

  /** The program-level proof record, or `null` when the proof catalog has none for this key. */
  readonly proof = computed<ReadState<StrategyValidationSummary | null>>(() =>
    mapRead(
      readStateOf(this.proofRead),
      (catalog) => catalog.strategies.find((strategy) => strategy.strategy_key === this.key()) ?? null,
    ),
  );

  readonly qualifiedVersions = computed<ReadState<QualifiedVersions>>(() =>
    mapRead(readStateOf(this.qualificationsRead), (versions) => ({
      bySymbol: versionsBySymbol(this.key(), versions),
      mayBeCut: versions.length >= QUALIFICATION_LIST_LIMIT,
    })),
  );

  /** The exact-configuration reviews recorded for this strategy, newest first. */
  readonly configurationReviews = computed<ReadState<readonly GoldenValidation[]>>(() =>
    readStateOf(this.reviewsRead),
  );

  readonly fleet = computed<ReadState<FleetRunningRead>>(() => readStateOf(this.fleetRead));

  /** This strategy's running bots. A failed or incomplete fleet read says unknown, never none. */
  readonly runningBots = computed<RunningBotsAnswer>(() => {
    const state = this.fleet();
    return runningBotsFor(this.key(), state.kind === 'ready' ? state.value : null);
  });

  /** The declared parameter schema and Golden Search declaration, once both are read. */
  readonly declarations = computed<ReadState<StrategyDeclarations>>(() =>
    mapRead(
      combineReads(
        mapRead(
          readStateOf(this.schemasRead),
          (strategies) => strategies.find((strategy) => strategy.name === this.key()) ?? null,
        ),
        mapRead(
          readStateOf(this.goldenCapabilitiesRead),
          (capabilities) => capabilities.find((capability) => capability.strategy_key === this.key()) ?? null,
        ),
      ),
      ([schema, golden]) => ({ schema, golden }),
    ),
  );

  retry(target: RetryTarget): void {
    switch (target) {
      case 'registry':
        this.registryRead.reload();
        break;
      case 'proof':
        this.proofRead.reload();
        break;
      case 'approvals':
        this.qualificationsRead.reload();
        this.reviewsRead.reload();
        break;
      case 'fleet':
        this.fleetRead.reload();
        break;
      case 'declarations':
        this.schemasRead.reload();
        this.goldenCapabilitiesRead.reload();
        break;
    }
  }
}
