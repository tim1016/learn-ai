import type { OrderSide, OrderType, TimeInForce } from '../../../api/alpaca.types';

/** A draft equity leg the operator is assembling before submission. */
export interface AlpacaOrderDraftLeg {
  readonly id: number;
  symbol: string;
  side: OrderSide;
  quantity: string;
  orderType: OrderType;
  limitPrice: string;
  timeInForce: TimeInForce;
}
