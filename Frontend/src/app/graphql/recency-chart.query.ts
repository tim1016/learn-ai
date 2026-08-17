import { gql } from "apollo-angular";

export const RECENCY_TRADES_QUERY = gql`
  query RecencyTrades($fromMs: Long!, $toMs: Long!, $symbols: [String!], $strategies: [String!]) {
    recencyTrades(fromMs: $fromMs, toMs: $toMs, symbols: $symbols, strategies: $strategies) {
      symbol
      strategyKey
      paramsHash
      paramsJson
      fingerprint
      entryMs
      exitMs
      pnlPts
      pnlPct
      quantity
      pnl
      holdingSessions
      sharpe
      studyId
      recencyRunId
      isSyntheticExit
      signalReason
      memberships {
        recencyRunId
        studyId
        createdAtMs
      }
    }
  }
`;

export const RECENCY_HERO_QUERY = gql`
  query RecencyHero($fromMs: Long!, $toMs: Long!, $symbols: [String!], $strategies: [String!]) {
    recencyHero(fromMs: $fromMs, toMs: $toMs, symbols: $symbols, strategies: $strategies) {
      symbol
      strategyKey
      paramsHash
      totalPnl
      recencyRunId
    }
  }
`;

export const SOFT_DELETE_RECENCY_RUN_MUTATION = gql`
  mutation SoftDeleteRecencyRun($runId: Int!) {
    softDeleteRecencyRun(runId: $runId) {
      ... on RecencyRunMutationSuccess {
        recencyRunId
      }
      ... on RecencyRunNotFoundError {
        code
        message
      }
    }
  }
`;

export interface SoftDeleteRecencyRunMutationResult {
  softDeleteRecencyRun:
    | { recencyRunId: number; code?: never; message?: never }
    | { recencyRunId?: never; code: string; message: string };
}

export interface RecencyTradeQueryResultItem {
  symbol: string;
  strategyKey: string;
  paramsHash: string;
  paramsJson: string;
  fingerprint: string;
  entryMs: number;
  exitMs: number;
  pnlPts: number;
  pnlPct: number;
  quantity: number;
  pnl: number;
  holdingSessions: number;
  sharpe: number | null;
  studyId: number | null;
  recencyRunId: number;
  isSyntheticExit: boolean;
  signalReason: string;
  memberships: RecencyTradeMembershipQueryResultItem[];
}

export interface RecencyTradeMembershipQueryResultItem {
  recencyRunId: number;
  studyId: number | null;
  createdAtMs: number;
}

export interface RecencyTradesQueryResult {
  recencyTrades: RecencyTradeQueryResultItem[];
}

export interface RecencyHeroQueryResultItem {
  symbol: string;
  strategyKey: string;
  paramsHash: string;
  totalPnl: number;
  recencyRunId: number;
}

export interface RecencyHeroQueryResult {
  recencyHero: RecencyHeroQueryResultItem[];
}
