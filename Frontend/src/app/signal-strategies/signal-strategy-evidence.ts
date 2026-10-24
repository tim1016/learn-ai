import {
  BOT_HISTORY_WORLDS,
  type BotHistoryService,
  type BotHistoryWorld,
  type FleetBotHistoryPage,
  type FleetBotHistoryRow,
} from '../components/brokers/alpaca-history/bot-history.service';
import type { GoldenQualificationSummary } from '../components/golden-search/golden-search.types';
import type { ParamProperty } from '../components/strategy-lab/strategy-lab.models';
import type { components } from '../api/broker.types';
import type { StrategyProofDossier, StrategyValidationSummary } from '../services/strategy-validation.types';

/** How a status chip is drawn; the words carry the state, the tone only helps the eye. */
export type ChipTone = 'ready' | 'degraded' | 'blocked' | 'unknown';

export type QualificationStatus = GoldenQualificationSummary['status'];
export type ProofState = NonNullable<StrategyProofDossier['state']>;
export type FleetGap = FleetBotHistoryPage['gaps'][number];
export type StrategyRegistryEntry = components['schemas']['StrategyRegistryEntry'];

export const QUALIFICATION_TONES: Readonly<Record<QualificationStatus, ChipTone>> = {
  ready: 'ready',
  stale: 'degraded',
  revoked: 'blocked',
  unverifiable: 'unknown',
};

export const PROOF_TONES: Readonly<Record<ProofState, ChipTone>> = {
  current: 'ready',
  stale: 'degraded',
  missing: 'unknown',
  blocked: 'blocked',
  rejected: 'blocked',
  unreadable: 'unknown',
};

// ── Program proof ──────────────────────────────────────────────────────────

export interface ProofSummary {
  readonly state: ProofState;
  readonly completedStages: number;
  readonly totalStages: number;
  readonly blockingSummary: string | null;
}

/**
 * A record's proof as the screen reads it. The wire leaves off a field that
 * holds its declared default, so an absent dossier or state reads as Python's
 * own default: a proof that is missing, with no stages counted.
 */
export function proofSummaryOf(record: StrategyValidationSummary): ProofSummary {
  const proof = record.proof;
  return {
    state: proof?.state ?? 'missing',
    completedStages: proof?.completed_stages ?? 0,
    totalStages: proof?.total_stages ?? 0,
    blockingSummary: proof?.blocking_summary ?? null,
  };
}

// ── Qualified versions, one symbol at a time ───────────────────────────────

export interface SymbolVersions {
  readonly symbol: string;
  /** Newest first. */
  readonly versions: readonly GoldenQualificationSummary[];
  readonly statusCounts: readonly { readonly status: QualificationStatus; readonly count: number }[];
}

const STATUS_ORDER: readonly QualificationStatus[] = ['ready', 'stale', 'revoked', 'unverifiable'];

/**
 * One strategy's qualified versions, grouped by the symbol each was approved
 * for. A version approves one symbol and one exact configuration, so nothing
 * here adds a symbol's versions into a strategy-wide verdict.
 */
export function versionsBySymbol(
  strategyKey: string,
  versions: readonly GoldenQualificationSummary[],
): readonly SymbolVersions[] {
  const bySymbol = new Map<string, GoldenQualificationSummary[]>();
  for (const version of versions) {
    if (version.program_key !== strategyKey) continue;
    bySymbol.set(version.symbol, [...(bySymbol.get(version.symbol) ?? []), version]);
  }
  return [...bySymbol.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([symbol, group]) => ({
      symbol,
      versions: [...group].sort((left, right) => right.created_at_ms - left.created_at_ms),
      statusCounts: STATUS_ORDER.flatMap((status) => {
        const count = group.filter((version) => version.status === status).length;
        return count === 0 ? [] : [{ status, count }];
      }),
    }));
}

// ── Running bots, and what an incomplete read may say ──────────────────────

/** Most pages of running bots one read follows before it calls itself incomplete. */
export const MAX_RUNNING_PAGES = 8;

/** Every running bot the fleet read could see, and whether that was every account. */
export interface FleetRunningRead {
  /** The oldest page's `observed_at_ms`: no row is newer than what this says. */
  readonly observedAtMs: number;
  readonly rows: readonly FleetBotHistoryRow[];
  /** Accounts (or Dry Runs) the read could not see, by name. */
  readonly gaps: readonly FleetGap[];
  /** Every page was read and no account was unreadable. */
  readonly complete: boolean;
}

/**
 * Reads every page of running bots through `readPage`. A read that fails
 * rejects: a page missing from the middle is not a smaller fleet.
 *
 * Pages are read at different moments, so a bot that starts or stops between
 * two of them can be listed twice or missed. A bot is counted once, and the
 * read calls itself complete only when the pages agreed on the total and the
 * bots counted are exactly that total.
 */
export async function readRunningFleet(
  readPage: (page: number) => Promise<FleetBotHistoryPage>,
): Promise<FleetRunningRead> {
  const first = await readPage(1);
  const bots = new Map<string, FleetBotHistoryRow>();
  const take = (rows: readonly FleetBotHistoryRow[]): void => {
    for (const row of rows) bots.set(`${row.clerk_id}/${row.strategy_instance_id}`, row);
  };
  take(first.rows);
  const gaps = new Map<string, FleetGap>(first.gaps.map((gap) => [gapKey(gap), gap]));
  let observedAtMs = first.observed_at_ms;
  let totalsAgree = true;
  const pageCount = Math.ceil(first.total / first.page_size);
  for (let page = 2; page <= Math.min(pageCount, MAX_RUNNING_PAGES); page += 1) {
    const next = await readPage(page);
    take(next.rows);
    for (const gap of next.gaps) gaps.set(gapKey(gap), gap);
    observedAtMs = Math.min(observedAtMs, next.observed_at_ms);
    if (next.total !== first.total) totalsAgree = false;
  }
  return {
    observedAtMs,
    rows: [...bots.values()],
    gaps: [...gaps.values()],
    complete: gaps.size === 0 && totalsAgree && pageCount <= MAX_RUNNING_PAGES && bots.size === first.total,
  };
}

/**
 * Every running bot of one strategy, or of every strategy when `strategyKey` is
 * `null`: the one read the strategies list and a strategy's own pages share.
 */
export function readRunningBots(
  botHistory: Pick<BotHistoryService, 'readRunning'>,
  strategyKey: string | null,
): Promise<FleetRunningRead> {
  return readRunningFleet((page) => botHistory.readRunning(strategyKey, page));
}

function gapKey(gap: FleetGap): string {
  return `${gap.broker}/${gap.clerk_id}/${gap.account_id ?? ''}/${gap.strategy_instance_id ?? ''}`;
}

export interface RunningBotsAnswer {
  /** `unknown`: nothing can be said (the read failed, or it was incomplete and saw none). */
  readonly state: 'none' | 'some' | 'unknown';
  readonly bots: readonly FleetBotHistoryRow[];
  /** The read was incomplete, so `bots` may be only some of them. */
  readonly atLeast: boolean;
  readonly observedAtMs: number | null;
  /** One entry per world with a bot, in the History filter's own order and words. */
  readonly worlds: readonly { readonly world: BotHistoryWorld; readonly label: string; readonly count: number }[];
}

const UNKNOWN_BOTS: RunningBotsAnswer = { state: 'unknown', bots: [], atLeast: false, observedAtMs: null, worlds: [] };

/**
 * What the fleet read says about one strategy's running bots. A read that
 * failed, or that could not see every account and found none, says unknown:
 * zero is only ever the answer of a complete read.
 */
export function runningBotsFor(strategyKey: string, fleet: FleetRunningRead | null): RunningBotsAnswer {
  if (fleet === null) return UNKNOWN_BOTS;
  const bots = fleet.rows.filter((row) => row.strategy_key === strategyKey);
  if (bots.length === 0) {
    return fleet.complete
      ? { state: 'none', bots, atLeast: false, observedAtMs: fleet.observedAtMs, worlds: [] }
      : UNKNOWN_BOTS;
  }
  return {
    state: 'some',
    bots,
    atLeast: !fleet.complete,
    observedAtMs: fleet.observedAtMs,
    worlds: BOT_HISTORY_WORLDS.flatMap(({ id, label }) => {
      const count = bots.filter((bot) => bot.world === id).length;
      return count === 0 ? [] : [{ world: id, label, count }];
    }),
  };
}

// ── Declared parameters ────────────────────────────────────────────────────

/** A declared bound, in words: "≥ 2 and ≤ 500", "> 0 and < 100", or that none is declared. */
export function allowedRangeText(property: ParamProperty): string {
  if (property.type === 'string') {
    return property.maxLength === undefined ? 'any text' : `text up to ${property.maxLength} characters`;
  }
  const lower = property.minimum !== undefined ? `≥ ${property.minimum}` : property.exclusiveMinimum !== undefined ? `> ${property.exclusiveMinimum}` : null;
  const upper = property.maximum !== undefined ? `≤ ${property.maximum}` : property.exclusiveMaximum !== undefined ? `< ${property.exclusiveMaximum}` : null;
  const bounds = [lower, upper].filter((bound): bound is string => bound !== null);
  return bounds.length === 0 ? 'no limit declared' : bounds.join(' and ');
}

/** A parameter value as the table shows it. */
export function valueText(value: unknown): string {
  if (value === undefined || value === null) return '—';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/** A configuration's values as `name = value` pairs in name order, or `null` when it holds none. */
export function parameterPairs(parameters: Readonly<Record<string, unknown>> | null | undefined): readonly string[] | null {
  const entries = Object.entries(parameters ?? {}).sort(([left], [right]) => left.localeCompare(right));
  return entries.length === 0 ? null : entries.map(([name, value]) => `${name} = ${valueText(value)}`);
}
