/**
 * Convenience aliases for the Broker System v2 contract models, sourced from
 * the auto-generated OpenAPI types (`broker.types.ts`). Regenerate the source
 * with `npm run codegen:openapi` whenever the Python contract changes; this
 * file only re-exports, so it never needs hand-editing beyond adding an alias.
 */
import type { components } from './broker.types';

export type BrokerAccountSnapshot = components['schemas']['BrokerAccountSnapshot'];
export type BrokerActivity = components['schemas']['BrokerActivity'];
export type BrokerPosition = components['schemas']['BrokerPosition'];
export type BrokerOrder = components['schemas']['BrokerOrder'];
export type BrokerOrderGroup = components['schemas']['BrokerOrderGroup'];
export type BrokerOrderEvent = components['schemas']['BrokerOrderEvent'];
export type BrokerPortfolioHistory = components['schemas']['BrokerPortfolioHistory'];
export type PortfolioHistoryRange = components['schemas']['PortfolioHistoryRange'];

// C1 + C2 + C3 Trader-lens proof. Values are server-owned and generated from
// the Python OpenAPI contract; the browser only presents them.
export type AccountFifoAttributionRow = components['schemas']['FifoAttributionRowResponse'];
export type AccountPnlAttribution = components['schemas']['AccountPnlAttributionResponse'];
export type AccountPnlDivergence = components['schemas']['AccountPnlDivergenceResponse'];
export type AccountPnlReconciliation = components['schemas']['AccountPnlReconciliationResponse'];
export type PortfolioHistoryProof = components['schemas']['PortfolioHistoryProofResponse'];

// Phase-2 order submission (write path). S2 adds limit orders + time-in-force.
export type BrokerOrderRequest = components['schemas']['BrokerOrderRequest'];
export type BrokerOrderLeg = components['schemas']['BrokerOrderLeg'];
export type OrderSide = components['schemas']['OrderSide'];
// ``order_type`` is inlined into the leg schema (a Literal union), not a named
// OpenAPI schema, so derive the alias from the leg field.
export type OrderType = NonNullable<BrokerOrderLeg['order_type']>;
export type TimeInForce = components['schemas']['TimeInForce'];
export type OrderSubmitResult = components['schemas']['OrderSubmitResult'];
export type OrderLegResult = components['schemas']['OrderLegResult'];
export type OrderLegError = components['schemas']['OrderLegError'];
// Phase-2 S3 order cancellation (write path).
export type OrderCancelResult = components['schemas']['OrderCancelResult'];

// SQLite-owned manual market-order tracer. The browser submits a stable ticket
// and leg UUID; the server supplies custody attribution and broker identity.
export type ManualOrderCapability = components['schemas']['ManualOrderCapabilityResponse'];
export type ManualOrderPreviewRequest = components['schemas']['ManualOrderPreviewRequest'];
export type ManualOrderPreview = components['schemas']['ManualOrderPreviewResponse'];
export type ManualOrderSubmitRequest = components['schemas']['ManualOrderSubmitRequest'];
export type ManualOrderTicket = components['schemas']['ManualOrderTicketResponse'];
export type ManualOrderCancelRequest = components['schemas']['ManualOrderCancelRequest'];
export type ManualOrderCancellation = components['schemas']['ManualOrderCancellationResponse'];

// Phase-2 S6 reconciliation + flag-and-hold (clerk status + clear-hold).
export type ClerkStatus = components['schemas']['ClerkStatus'];
export type HoldState = components['schemas']['HoldState'];
export type ReconciliationSummary = components['schemas']['ReconciliationSummary'];

// Custody resolution (Clerk↔broker reconciliation on the Accounts page).
export type CustodyDiagnosis = components['schemas']['CustodyDiagnosis'];
export type CustodyDivergence = components['schemas']['CustodyDivergence'];
export type CustodyPositionDelta = components['schemas']['CustodyPositionDelta'];
// POST .../clerk/resolve request/response (Task 2.2, landed).
export type CustodyResolutionRequest = components['schemas']['CustodyResolutionRequest'];
export type CustodyResolutionReceipt = components['schemas']['CustodyResolutionReceipt'];
export type CustodyResolutionStepResult = components['schemas']['CustodyResolutionStepResult'];

// Activated SQLite Account Clerk projection and evidence-bound recovery.
export type SqliteClerkProjection = components['schemas']['ClerkProjectionResponse'];
export type SqliteRecoveryAction = components['schemas']['RecoveryCapabilityResponse'];
export type SqliteRecoveryActionCheck = components['schemas']['RecoveryActionCheckResponse'];
export type SqliteRecoveryResult = components['schemas']['RecoveryActionExecuteResponse'];
export type SqliteSafeFlattenPlan = components['schemas']['SafeFlattenPlanResponse'];
export type SqliteTimelineEntry = components['schemas']['TimelineEntryResponse'];
export type SqliteTimelinePage = components['schemas']['TimelinePageResponse'];
