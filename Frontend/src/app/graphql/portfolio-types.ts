// Portfolio Management System Types

export interface Account {
  id: string;
  name: string;
  type: string;
  baseCurrency: string;
  initialCash: number;
  cash: number;
  createdAt: string;
}

export interface Position {
  id: string;
  accountId: string;
  tickerId: number;
  assetType: string;
  netQuantity: number;
  avgCostBasis: number;
  realizedPnL: number;
  status: string;
  openedAt: string;
  closedAt?: string;
  ticker?: { symbol: string; name: string };
  lots?: PositionLot[];
}

export interface PositionLot {
  id: string;
  positionId: string;
  tradeId: string;
  quantity: number;
  entryPrice: number;
  remainingQuantity: number;
  realizedPnL: number;
  openedAt: string;
  closedAt?: string;
}

export interface PortfolioTrade {
  id: string;
  accountId: string;
  tickerId: number;
  side: string;
  quantity: number;
  price: number;
  fees: number;
  multiplier: number;
  executionTimestamp: string;
  ticker?: { symbol: string; name: string };
}

export interface PortfolioState {
  account: Account;
  positions: Position[];
  recentTrades: PortfolioTrade[];
}

// Valuation

export interface PositionValuation {
  symbol: string;
  currentPrice: number;
  quantity: number;
  multiplier: number;
  marketValue: number;
  unrealizedPnL: number;
  costBasis: number;
}

export interface PortfolioValuation {
  cash: number;
  marketValue: number;
  equity: number;
  unrealizedPnL: number;
  realizedPnL: number;
  netDelta: number;
  netGamma: number;
  netTheta: number;
  netVega: number;
  positions: PositionValuation[];
}

// Snapshots & Metrics

export interface PortfolioSnapshot {
  id: string;
  accountId: string;
  timestamp: string;
  equity: number;
  cash: number;
  marketValue: number;
  unrealizedPnL: number;
  realizedPnL: number;
  netDelta: number;
  netGamma: number;
  netTheta: number;
  netVega: number;
}

export interface DrawdownPoint {
  timestamp: string;
  equity: number;
  peakEquity: number;
  drawdown: number;
  drawdownPercent: number;
}

export interface PortfolioMetrics {
  totalReturnPercent: number;
  sharpeRatio: number;
  sortinoRatio: number;
  calmarRatio: number;
  maxDrawdown: number;
  maxDrawdownPercent: number;
  winRate: number;
  profitFactor: number;
  snapshotCount: number;
}

// Risk

export interface RiskRule {
  id: string;
  accountId: string;
  ruleType: string;
  threshold: number;
  action: string;
  severity: string;
  enabled: boolean;
  lastTriggered?: string;
}

export interface DollarDeltaResult {
  positionId: string;
  symbol: string;
  delta: number;
  price: number;
  quantity: number;
  multiplier: number;
  dollarDelta: number;
}

export interface RiskViolation {
  ruleId: string;
  ruleType: string;
  action: string;
  severity: string;
  threshold: number;
  actualValue: number;
  message: string;
}

export interface ScenarioResult {
  currentEquity: number;
  scenarioEquity: number;
  pnLImpact: number;
  pnLImpactPercent: number;
  positions: PositionScenario[];
}

export interface PositionScenario {
  symbol: string;
  currentValue: number;
  scenarioValue: number;
  pnLImpact: number;
}

// Reconciliation

export interface ReconciliationReport {
  accountId: string;
  hasDrift: boolean;
  drifts: PositionDrift[];
  cachedPositionCount: number;
  rebuiltPositionCount: number;
}

export interface PositionDrift {
  tickerId: number;
  symbol: string;
  cachedQuantity: number;
  rebuiltQuantity: number;
  cachedRealizedPnL: number;
  rebuiltRealizedPnL: number;
  driftType: string;
}

// Strategy Attribution

export interface StrategyPnLResult {
  strategyExecutionId: number;
  strategyName: string;
  totalPnL: number;
  tradeCount: number;
  winRate: number;
}

export interface AlphaAttribution {
  strategyExecutionId: number;
  strategyName: string;
  pnL: number;
  tradeCount: number;
  contributionPercent: number;
}

export interface StrategyAllocation {
  id: string;
  accountId: string;
  strategyExecutionId: number;
  capitalAllocated: number;
  startDate: string;
  endDate: string;
  strategyExecution?: { strategyName: string };
}

// Mutation Results

export interface MutationResult<T = unknown> {
  success: boolean;
  error?: string;
  message?: string;
  data?: T;
}

export interface AccountResult {
  success: boolean;
  account?: Account;
  error?: string;
}

export interface TradeResult {
  success: boolean;
  trade?: PortfolioTrade;
  error?: string;
}

export interface SnapshotResultGql {
  success: boolean;
  snapshot?: PortfolioSnapshot;
  message?: string;
  error?: string;
}

export interface RiskRuleResult {
  success: boolean;
  rule?: RiskRule;
  error?: string;
}

export interface ImportResult {
  success: boolean;
  tradeCount: number;
  message?: string;
  error?: string;
}

export interface RebuildResult {
  success: boolean;
  positionCount?: number;
  message?: string;
  error?: string;
}

// Validation

export interface ValidationSuiteResult {
  accountId: string;
  startedAt: string;
  completedAt: string;
  durationMs: number;
  totalTests: number;
  passed: number;
  failed: number;
  tests: ValidationTestResult[];
}

export interface ValidationTestResult {
  testNumber: number;
  name: string;
  category: string;
  objective: string;
  passed: boolean;
  durationMs: number;
  error?: string;
  assertions: ValidationAssertion[];
}

export interface ValidationAssertion {
  label: string;
  expected: string;
  actual: string;
  passed: boolean;
  tolerance?: number;
}
