import { provideZonelessChangeDetection } from '@angular/core';
import { provideRouter, withComponentInputBinding, withRouterConfig, type Routes } from '@angular/router';
import { from } from 'rxjs';

import {
  BotHistoryService,
  type BotHistoryQuery,
  type FleetBotHistoryPage,
} from '../../components/brokers/alpaca-history/bot-history.service';
import { GoldenSearchService } from '../../components/golden-search/golden-search.service';
import type { GoldenQualificationSummary, StrategyCapability } from '../../components/golden-search/golden-search.types';
import type { StrategyInfo } from '../../components/strategy-lab/strategy-lab.models';
import { provideFleetDirectory } from '../../fleet/fleet-directory-testing';
import { GoldenValidationService } from '../../services/golden-validation.service';
import type { GoldenValidation } from '../../services/golden-validation.types';
import { LeanSourceService } from '../../services/lean-source.service';
import { StrategyValidationService } from '../../services/strategy-validation.service';
import type { StrategyValidationSummary } from '../../services/strategy-validation.types';
import { NotFoundComponent } from '../../shell/not-found.component';
import { SIGNAL_STRATEGIES_ROUTES } from '../signal-strategies.routes';
import { SignalStrategyRegistryService } from '../signal-strategy-registry.service';
import type { StrategyRegistryEntry } from '../signal-strategy-evidence';
import { fleetPage } from './fixtures';

/** What each read answers: its data, or the failure it rejects with. */
export type Answer<T> = T | Error;

export interface SignalStrategyWorld {
  entries: Answer<readonly StrategyRegistryEntry[]>;
  proof: Answer<readonly StrategyValidationSummary[]>;
  qualifications: Answer<readonly GoldenQualificationSummary[]>;
  reviews: Answer<readonly GoldenValidation[]>;
  fleet: Answer<FleetBotHistoryPage>;
  schemas: Answer<readonly StrategyInfo[]>;
  capabilities: Answer<readonly StrategyCapability[]>;
}

/** A world where every read answers with nothing. */
export function emptyWorld(overrides: Partial<SignalStrategyWorld> = {}): SignalStrategyWorld {
  return {
    entries: [],
    proof: [],
    qualifications: [],
    reviews: [],
    fleet: fleetPage({ rows: [] }),
    schemas: [],
    capabilities: [],
    ...overrides,
  };
}

function answer<T>(value: Answer<T>): Promise<T> {
  return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
}

/** The reads the Signal strategies pages make, each counted so a spec can see what was asked. */
export interface SignalStrategyReads {
  readonly entries: () => void;
  readonly qualifications: () => void;
  readonly fleet: (query: BotHistoryQuery) => void;
}

/**
 * Reads and verbs a page beyond Overview and Define makes, each replacing the service's
 * method of that name for one spec: Golden Search's studies and plan, the proof's
 * detail, recheck and flag, and the baseline-run reviews. A method a spec does not
 * give is not there to call, so a page that reaches for one fails loudly.
 */
export interface SignalStrategyServices {
  readonly golden: Record<string, unknown>;
  readonly validation: Record<string, unknown>;
  readonly goldenValidation: Record<string, unknown>;
}

/** The place's routes, then any a spec adds for the pages it walks to, then the not-found page. */
function routesWith(extra: Routes): Routes {
  return [
    { path: 'signal-strategies', children: SIGNAL_STRATEGIES_ROUTES },
    ...extra,
    { path: '**', data: { notFound: true }, component: NotFoundComponent },
  ];
}

/**
 * Providers that put the Signal strategies place in front of a world, behind
 * the real routes. The reads are the services' own methods, replaced one for
 * one, so what a page asks and what it does with the answer is the page's.
 */
export function provideSignalStrategyWorld(
  world: SignalStrategyWorld,
  reads: Partial<SignalStrategyReads> = {},
  services: Partial<SignalStrategyServices> = {},
  extraRoutes: Routes = [],
) {
  const fleet = provideFleetDirectory();
  return [
    provideZonelessChangeDetection(),
    provideRouter(routesWith(extraRoutes), withComponentInputBinding(), withRouterConfig({ paramsInheritanceStrategy: 'always' })),
    { provide: fleet.provide, useValue: fleet.useValue },
    {
      provide: SignalStrategyRegistryService,
      useValue: {
        entries: () => {
          reads.entries?.();
          return answer(world.entries);
        },
        parameterSchemas: () => answer(world.schemas),
      },
    },
    {
      provide: StrategyValidationService,
      useValue: { getCatalog: async () => ({ strategies: [...(await answer(world.proof))] }), ...services.validation },
    },
    {
      provide: GoldenSearchService,
      useValue: {
        listQualifications: () => {
          reads.qualifications?.();
          return answer(world.qualifications);
        },
        capabilities: () => answer(world.capabilities),
        ...services.golden,
      },
    },
    {
      provide: GoldenValidationService,
      useValue: { list: () => from(answer(world.reviews)), ...services.goldenValidation },
    },
    {
      provide: BotHistoryService,
      useValue: {
        read: (query: BotHistoryQuery) => {
          reads.fleet?.(query);
          return answer(world.fleet);
        },
      },
    },
    {
      provide: LeanSourceService,
      useValue: {
        getStrategySource: async () => ({ kind: 'unregistered', detail: 'This strategy has no registered LEAN validation source.' }),
      },
    },
  ];
}
