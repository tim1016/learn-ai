import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, map, tap } from 'rxjs';
import { environment } from '../../environments/environment';
import {
  Account, AccountResult, PortfolioState, PortfolioValuation,
  PortfolioSnapshot, DrawdownPoint, PortfolioMetrics, Position,
  PortfolioTrade, RiskRule, RiskRuleResult, DollarDeltaResult,
  RiskViolation, ScenarioResult, ReconciliationReport,
  StrategyPnLResult, AlphaAttribution, StrategyAllocation,
  SnapshotResultGql, TradeResult, RebuildResult, ImportResult,
  ValidationSuiteResult,
} from '../graphql/portfolio-types';

const GRAPHQL_URL = environment.backendUrl;

interface GraphQLResponse<T = Record<string, unknown>> {
  data: T;
  errors?: { message: string }[];
}

function gql<T>(http: HttpClient, query: string, variables: Record<string, unknown> = {}): Observable<T> {
  return http.post<GraphQLResponse>(GRAPHQL_URL, { query, variables }).pipe(
    tap(res => { if (res.errors?.length) throw new Error(res.errors[0].message); }),
    map(res => res.data as T),
  );
}

@Injectable({ providedIn: 'root' })
export class PortfolioService {
  private http = inject(HttpClient);

  // ── Accounts ──

  getAccounts(): Observable<Account[]> {
    return gql<{ getAccounts: Account[] }>(this.http, `
      query { getAccounts { id name type baseCurrency initialCash cash createdAt } }
    `).pipe(map(d => d.getAccounts));
  }

  createAccount(name: string, type: string, initialCash: number): Observable<AccountResult> {
    return gql<{ createAccount: AccountResult }>(this.http, `
      mutation CreateAccount($name: String!, $type: String!, $initialCash: Decimal!) {
        createAccount(name: $name, type: $type, initialCash: $initialCash) {
          success error account { id name type cash initialCash createdAt }
        }
      }
    `, { name, type, initialCash }).pipe(map(d => d.createAccount));
  }

  // ── Portfolio State ──

  getPortfolioState(accountId: string): Observable<PortfolioState> {
    return gql<{ getPortfolioState: PortfolioState }>(this.http, `
      query GetPortfolioState($accountId: UUID!) {
        getPortfolioState(accountId: $accountId) {
          account { id name type cash initialCash createdAt }
          positions {
            id tickerId assetType netQuantity avgCostBasis realizedPnL status openedAt closedAt
            ticker { symbol name }
          }
          recentTrades {
            id tickerId side quantity price fees multiplier executionTimestamp
            ticker { symbol name }
          }
        }
      }
    `, { accountId }).pipe(map(d => d.getPortfolioState));
  }

  // ── Positions ──

  getPositions(accountId: string): Observable<Position[]> {
    return gql<{ getPositions: Position[] }>(this.http, `
      query GetPositions($accountId: UUID!) {
        getPositions(accountId: $accountId) {
          id tickerId assetType netQuantity avgCostBasis realizedPnL status openedAt closedAt
          ticker { symbol name }
          lots { id quantity entryPrice remainingQuantity realizedPnL openedAt closedAt }
        }
      }
    `, { accountId }).pipe(map(d => d.getPositions));
  }

  // ── Trades ──

  recordTrade(accountId: string, symbol: string, side: string, quantity: number,
    price: number, fees = 0, assetType = 'Stock', multiplier = 1): Observable<TradeResult> {
    return gql<{ recordTrade: TradeResult }>(this.http, `
      mutation RecordTrade($accountId: UUID!, $symbol: String!, $side: String!,
        $quantity: Decimal!, $price: Decimal!, $fees: Decimal!,
        $assetType: String!, $multiplier: Int!) {
        recordTrade(accountId: $accountId, symbol: $symbol, side: $side,
          quantity: $quantity, price: $price, fees: $fees,
          assetType: $assetType, multiplier: $multiplier) {
          success error trade { id side quantity price executionTimestamp ticker { symbol } }
        }
      }
    `, { accountId, symbol, side, quantity, price, fees, assetType, multiplier })
      .pipe(map(d => d.recordTrade));
  }

  // ── Valuation ──

  getValuation(accountId: string): Observable<PortfolioValuation> {
    return gql<{ getPortfolioValuation: PortfolioValuation }>(this.http, `
      query GetValuation($accountId: UUID!) {
        getPortfolioValuation(accountId: $accountId) {
          cash marketValue equity unrealizedPnL realizedPnL
          netDelta netGamma netTheta netVega
          positions { symbol currentPrice quantity multiplier marketValue unrealizedPnL costBasis }
        }
      }
    `, { accountId }).pipe(map(d => d.getPortfolioValuation));
  }

  // ── Snapshots & Metrics ──

  takeSnapshot(accountId: string): Observable<SnapshotResultGql> {
    return gql<{ takePortfolioSnapshot: SnapshotResultGql }>(this.http, `
      mutation TakeSnapshot($accountId: UUID!) {
        takePortfolioSnapshot(accountId: $accountId) {
          success error message
          snapshot { id timestamp equity cash marketValue unrealizedPnL realizedPnL }
        }
      }
    `, { accountId }).pipe(map(d => d.takePortfolioSnapshot));
  }

  getEquityCurve(accountId: string, from?: string, to?: string): Observable<PortfolioSnapshot[]> {
    return gql<{ getEquityCurve: PortfolioSnapshot[] }>(this.http, `
      query GetEquityCurve($accountId: UUID!, $from: DateTime, $to: DateTime) {
        getEquityCurve(accountId: $accountId, from: $from, to: $to) {
          id timestamp equity cash marketValue
        }
      }
    `, { accountId, from, to }).pipe(map(d => d.getEquityCurve));
  }

  getDrawdownSeries(accountId: string): Observable<DrawdownPoint[]> {
    return gql<{ getDrawdownSeries: DrawdownPoint[] }>(this.http, `
      query GetDrawdown($accountId: UUID!) {
        getDrawdownSeries(accountId: $accountId) {
          timestamp equity peakEquity drawdown drawdownPercent
        }
      }
    `, { accountId }).pipe(map(d => d.getDrawdownSeries));
  }

  getMetrics(accountId: string): Observable<PortfolioMetrics> {
    return gql<{ getPortfolioMetrics: PortfolioMetrics }>(this.http, `
      query GetMetrics($accountId: UUID!) {
        getPortfolioMetrics(accountId: $accountId) {
          totalReturnPercent sharpeRatio sortinoRatio calmarRatio
          maxDrawdown maxDrawdownPercent winRate profitFactor snapshotCount
        }
      }
    `, { accountId }).pipe(map(d => d.getPortfolioMetrics));
  }

  // ── Risk ──

  getRiskRules(accountId: string): Observable<RiskRule[]> {
    return gql<{ getRiskRules: RiskRule[] }>(this.http, `
      query GetRiskRules($accountId: UUID!) {
        getRiskRules(accountId: $accountId) {
          id ruleType threshold action severity enabled lastTriggered
        }
      }
    `, { accountId }).pipe(map(d => d.getRiskRules));
  }

  createRiskRule(accountId: string, ruleType: string, threshold: number,
    action = 'Warn', severity = 'Medium'): Observable<RiskRuleResult> {
    return gql<{ createRiskRule: RiskRuleResult }>(this.http, `
      mutation CreateRiskRule($accountId: UUID!, $ruleType: String!, $threshold: Decimal!,
        $action: String!, $severity: String!) {
        createRiskRule(accountId: $accountId, ruleType: $ruleType, threshold: $threshold,
          action: $action, severity: $severity) {
          success error rule { id ruleType threshold action severity enabled }
        }
      }
    `, { accountId, ruleType, threshold, action, severity }).pipe(map(d => d.createRiskRule));
  }

  updateRiskRule(ruleId: string, updates: { threshold?: number; enabled?: boolean;
    action?: string; severity?: string }): Observable<RiskRuleResult> {
    return gql<{ updateRiskRule: RiskRuleResult }>(this.http, `
      mutation UpdateRiskRule($ruleId: UUID!, $threshold: Decimal, $enabled: Boolean,
        $action: String, $severity: String) {
        updateRiskRule(ruleId: $ruleId, threshold: $threshold, enabled: $enabled,
          action: $action, severity: $severity) {
          success error rule { id ruleType threshold action severity enabled }
        }
      }
    `, { ruleId, ...updates }).pipe(map(d => d.updateRiskRule));
  }

  getDollarDelta(accountId: string, prices: { symbol: string; price: number }[]): Observable<DollarDeltaResult[]> {
    return gql<{ getDollarDelta: DollarDeltaResult[] }>(this.http, `
      query GetDollarDelta($accountId: UUID!, $prices: [PriceInputInput!]!) {
        getDollarDelta(accountId: $accountId, prices: $prices) {
          positionId symbol delta price quantity multiplier dollarDelta
        }
      }
    `, { accountId, prices }).pipe(map(d => d.getDollarDelta));
  }

  evaluateRiskRules(accountId: string, prices: { symbol: string; price: number }[]): Observable<RiskViolation[]> {
    return gql<{ evaluateRiskRules: RiskViolation[] }>(this.http, `
      query EvaluateRules($accountId: UUID!, $prices: [PriceInputInput!]!) {
        evaluateRiskRules(accountId: $accountId, prices: $prices) {
          ruleId ruleType action severity threshold actualValue message
        }
      }
    `, { accountId, prices }).pipe(map(d => d.evaluateRiskRules));
  }

  runScenario(accountId: string, prices: { symbol: string; price: number }[],
    priceChangePercent?: number, ivChangePercent?: number, timeDaysForward?: number): Observable<ScenarioResult> {
    return gql<{ runScenario: ScenarioResult }>(this.http, `
      mutation RunScenario($accountId: UUID!, $prices: [PriceInputInput!]!,
        $priceChangePercent: Decimal, $ivChangePercent: Decimal, $timeDaysForward: Int) {
        runScenario(accountId: $accountId, prices: $prices,
          priceChangePercent: $priceChangePercent, ivChangePercent: $ivChangePercent,
          timeDaysForward: $timeDaysForward) {
          currentEquity scenarioEquity pnLImpact pnLImpactPercent
          positions { symbol currentValue scenarioValue pnLImpact }
        }
      }
    `, { accountId, prices, priceChangePercent, ivChangePercent, timeDaysForward })
      .pipe(map(d => d.runScenario));
  }

  // ── Reconciliation ──

  reconcile(accountId: string): Observable<ReconciliationReport> {
    return gql<{ reconcilePortfolio: ReconciliationReport }>(this.http, `
      query Reconcile($accountId: UUID!) {
        reconcilePortfolio(accountId: $accountId) {
          accountId hasDrift cachedPositionCount rebuiltPositionCount
          drifts { tickerId symbol cachedQuantity rebuiltQuantity cachedRealizedPnL rebuiltRealizedPnL driftType }
        }
      }
    `, { accountId }).pipe(map(d => d.reconcilePortfolio));
  }

  autoFix(accountId: string): Observable<RebuildResult> {
    return gql<{ autoFixPortfolio: RebuildResult }>(this.http, `
      mutation AutoFix($accountId: UUID!) {
        autoFixPortfolio(accountId: $accountId) { success error message }
      }
    `, { accountId }).pipe(map(d => d.autoFixPortfolio));
  }

  rebuildPositions(accountId: string): Observable<RebuildResult> {
    return gql<{ rebuildPositions: RebuildResult }>(this.http, `
      mutation RebuildPositions($accountId: UUID!) {
        rebuildPositions(accountId: $accountId) { success error positionCount message }
      }
    `, { accountId }).pipe(map(d => d.rebuildPositions));
  }

  // ── Strategy Attribution ──

  getStrategyAllocations(accountId: string): Observable<StrategyAllocation[]> {
    return gql<{ getStrategyAllocations: StrategyAllocation[] }>(this.http, `
      query GetAllocations($accountId: UUID!) {
        getStrategyAllocations(accountId: $accountId) {
          id strategyExecutionId capitalAllocated startDate endDate
          strategyExecution { strategyName }
        }
      }
    `, { accountId }).pipe(map(d => d.getStrategyAllocations));
  }

  importBacktestTrades(strategyExecutionId: number, accountId: string): Observable<ImportResult> {
    return gql<{ importBacktestTrades: ImportResult }>(this.http, `
      mutation ImportTrades($strategyExecutionId: Int!, $accountId: UUID!) {
        importBacktestTrades(strategyExecutionId: $strategyExecutionId, accountId: $accountId) {
          success error tradeCount message
        }
      }
    `, { strategyExecutionId, accountId }).pipe(map(d => d.importBacktestTrades));
  }

  getStrategyPnL(strategyExecutionId: number): Observable<StrategyPnLResult> {
    return gql<{ getStrategyPnL: StrategyPnLResult }>(this.http, `
      query GetStrategyPnL($strategyExecutionId: Int!) {
        getStrategyPnL(strategyExecutionId: $strategyExecutionId) {
          strategyExecutionId strategyName totalPnL tradeCount winRate
        }
      }
    `, { strategyExecutionId }).pipe(map(d => d.getStrategyPnL));
  }

  // ── Validation ──

  runValidation(): Observable<ValidationSuiteResult> {
    return gql<{ runPortfolioValidation: ValidationSuiteResult }>(this.http, `
      mutation RunValidation {
        runPortfolioValidation {
          accountId startedAt completedAt durationMs
          totalTests passed failed
          tests {
            testNumber name category objective passed durationMs error
            assertions { label expected actual passed tolerance }
          }
        }
      }
    `).pipe(map(d => d.runPortfolioValidation));
  }

  getAlphaAttribution(accountId: string): Observable<AlphaAttribution[]> {
    return gql<{ getAlphaAttribution: AlphaAttribution[] }>(this.http, `
      query GetAttribution($accountId: UUID!) {
        getAlphaAttribution(accountId: $accountId) {
          strategyExecutionId strategyName pnL tradeCount contributionPercent
        }
      }
    `, { accountId }).pipe(map(d => d.getAlphaAttribution));
  }
}
