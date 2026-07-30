/**
 * Card-to-section mapping for S3/S4 components to wire their "?" button.
 *
 * This is the canonical mapping — S3/S4 components import from here rather
 * than hardcoding anchor strings. The anchors correspond to heading IDs in
 * `docs/broker-v2-operator-manual.md` (and its Frontend asset copy).
 */
export const BROKER_V2_CARD_ANCHOR_MAP = {
  signal: 'station-1-signal',
  intent: 'station-2-intent',
  'submit-gate': 'station-3-submit-gate',
  'broker-ack': 'station-4-broker-ack',
  fill: 'station-5-fill',
  reconciled: 'station-6-reconciled',
  'bot-lifecycle': 'button-reference',
  holds: 'hold-actions',
  reconciliation: 'reconcile-actions',
} as const satisfies Record<string, string>;

export type BrokerV2CardName = keyof typeof BROKER_V2_CARD_ANCHOR_MAP;
