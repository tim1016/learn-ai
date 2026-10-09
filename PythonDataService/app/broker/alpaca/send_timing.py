"""Tie Alpaca's request budget to the Clerk's initial send-claim lifetime."""

from __future__ import annotations

DEFAULT_REQUEST_TIMEOUT_S = 15.0
MAX_RATE_LIMIT_RETRIES = 2
RATE_LIMIT_RETRY_CAP_S = 1.0

# Each retry follows a definitive 429. Other failures, including timeouts,
# end the send without another attempt. A synchronous SDK worker can exceed
# this request budget; ClaimedBrokerIO renews the claim until it finishes.
REQUESTED_ORDER_SEND_BUDGET_MS = int(
    ((MAX_RATE_LIMIT_RETRIES + 1) * DEFAULT_REQUEST_TIMEOUT_S
     + MAX_RATE_LIMIT_RETRIES * RATE_LIMIT_RETRY_CAP_S) * 1000
)
ORDER_SEND_CLAIM_MARGIN_MS = 13_000
ORDER_SEND_CLAIM_TTL_MS = REQUESTED_ORDER_SEND_BUDGET_MS + ORDER_SEND_CLAIM_MARGIN_MS
