/**
 * Convenience aliases for the Broker System v2 contract models, sourced from
 * the auto-generated OpenAPI types (`broker.types.ts`). Regenerate the source
 * with `npm run codegen:openapi` whenever the Python contract changes; this
 * file only re-exports, so it never needs hand-editing beyond adding an alias.
 */
import type { components } from './broker.types';

export type BrokerAccountSnapshot = components['schemas']['BrokerAccountSnapshot'];
export type BrokerPosition = components['schemas']['BrokerPosition'];
export type BrokerOrder = components['schemas']['BrokerOrder'];
export type BrokerOrderEvent = components['schemas']['BrokerOrderEvent'];

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

// Phase-2 S6 reconciliation + flag-and-hold (clerk status + clear-hold).
export type ClerkStatus = components['schemas']['ClerkStatus'];
export type HoldState = components['schemas']['HoldState'];
export type ReconciliationSummary = components['schemas']['ReconciliationSummary'];
