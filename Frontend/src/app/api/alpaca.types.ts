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

// Phase-2 S1 order submission (write path).
export type BrokerOrderRequest = components['schemas']['BrokerOrderRequest'];
export type BrokerOrderLeg = components['schemas']['BrokerOrderLeg'];
export type OrderSide = components['schemas']['OrderSide'];
export type OrderSubmitResult = components['schemas']['OrderSubmitResult'];
export type OrderLegResult = components['schemas']['OrderLegResult'];
export type OrderLegError = components['schemas']['OrderLegError'];
