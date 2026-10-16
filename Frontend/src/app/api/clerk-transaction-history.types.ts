import type { components } from './broker.types';

/** Clerk transaction history wire shapes come from the generated OpenAPI contract. */
export type ClerkTransactionOrigin = NonNullable<components['schemas']['ClerkTransactionSummaryRow']['transaction_origin']>;
export type ClerkFeeFidelity = NonNullable<components['schemas']['ClerkTransactionSummaryRow']['fee_fidelity']>;
export type ClerkOrderInstruction = components['schemas']['ClerkOrderInstruction'];
export type ClerkTransactionEvent = components['schemas']['ClerkTransactionEventRow'];
export type ClerkCustodyWindowSummary = components['schemas']['ClerkCustodyWindowSummary'];
export type ClerkCustodyTimeline = components['schemas']['ClerkCustodyTimeline'];
export type ClerkTransactionSummary = components['schemas']['ClerkTransactionSummaryRow'];
export type ClerkTransactionDetail = components['schemas']['ClerkTransactionRow'];
export type ClerkTransactionHistoryResponse = components['schemas']['ClerkTransactionHistoryResponse'];
export type ExternalOrderAcknowledgement = components['schemas']['ExternalOrderAcknowledgementResponse'];

/** Desk query state is local UI input, not a response schema. */
export interface ClerkTransactionFilters {
  readonly origin?: ClerkTransactionOrigin | null;
  readonly lifecycleState?: string | null;
  readonly strategyInstanceId?: string | null;
  readonly runId?: string | null;
  readonly fromMs?: number | null;
  readonly toMs?: number | null;
}
