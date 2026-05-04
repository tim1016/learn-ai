import { GraphqlError, type GraphQLErrorPayload } from '../graphql/graphql-error';

/**
 * One canonical entry for an error code surfaced to the UI.
 *
 * The backend assigns ``extensions.code`` on every domain error;
 * the catalog turns that code into the *what / try / details* trio
 * the three error components render. Adding a new code is a
 * one-line edit here — the components don't need to change.
 *
 * ``mathRef`` is the optional deep-link the error drawer renders
 * when a numeric divergence is the culprit. It's filled only when
 * the backend explicitly supplies it (see resolver guidance in
 * ``docs/math-sources-of-truth.md``); the frontend never pattern-
 * matches messages to guess one.
 */
export interface ErrorCatalogEntry {
  /** Single-sentence statement of what failed, in user-facing prose. */
  what: string;
  /** Single-sentence next-step instruction. */
  tryCopy: string;
  /** Optional deep-link to the math sources-of-truth doc. */
  mathRef?: string;
}

const FALLBACK: ErrorCatalogEntry = {
  what: 'Something went wrong.',
  tryCopy: 'Try the action again. If it keeps failing, capture the technical details below and share them.',
};

/**
 * Catalog keyed by ``extensions.code``. Codes are uppercase
 * underscore-separated; new codes added by the backend should be
 * mirrored here in the same PR. The list below covers the IBKR
 * surface (broker pages) and the broader GraphQL boundary.
 */
const CATALOG: Readonly<Record<string, ErrorCatalogEntry>> = {
  BROKER_DISCONNECTED: {
    what: 'IB Gateway is not responding.',
    tryCopy: 'Open Gateway, log into your paper account, then click Retry.',
  },
  SENTINEL_FAILED: {
    what: 'IBKR connected, but the account ID does not start with DU.',
    tryCopy: 'The service refused to start to protect a live account. Re-launch Gateway against the paper account and retry.',
  },
  PACING_LIMIT: {
    what: 'IBKR rejected the request because we are over the pacing limit.',
    tryCopy: 'Wait five seconds and click Retry. Repeated 502s usually mean IB Gateway needs a restart.',
  },
  SUBSCRIPTION_LIMIT: {
    what: 'IBKR rejected the request: too many simultaneous market-data lines.',
    tryCopy: 'IBKR caps lines at ~100. Close another streaming page or wait for the subscription to free up, then retry.',
  },
  OPRA_MISSING: {
    what: 'No OPRA subscription detected.',
    tryCopy: 'Quotes will be 15-min delayed until the OPRA subscription is added in IBKR Account Management.',
  },
  ORDER_REJECTED: {
    what: 'IBKR rejected the order.',
    tryCopy: 'See the rejection reason in the technical details below; common causes are price outside the day’s range, contract not found, or the market is closed.',
  },
  PAPER_CONFIRM_REQUIRED: {
    what: 'The order needs an explicit paper-mode confirmation.',
    tryCopy: 'Tick the confirm-paper checkbox in the order form and resubmit.',
  },
  GATEWAY_TIMEOUT: {
    what: 'The request timed out before IBKR responded.',
    tryCopy: 'Retry in a few seconds. If timeouts persist, check the broker status page.',
  },
};

/**
 * Look up a catalog entry by ``extensions.code``. Falls back to a
 * generic entry whose ``what`` is overridden by the caller from
 * the actual error message — this keeps unmapped codes legible
 * without the catalog needing to be exhaustive.
 */
export function lookupErrorEntry(code: string | undefined | null): ErrorCatalogEntry {
  if (!code) return FALLBACK;
  return CATALOG[code] ?? FALLBACK;
}

/**
 * Resolve the first usable ``extensions.code`` out of any error
 * shape the UI might encounter (``GraphqlError`` with one or many
 * payloads, plain ``Error``, or arbitrary thrown value).
 */
export function resolveErrorCode(err: unknown): string | undefined {
  if (err instanceof GraphqlError) {
    for (const e of err.errors) {
      const code = readCode(e);
      if (code) return code;
    }
    return undefined;
  }
  return undefined;
}

/**
 * Resolve a math-sources deep-link from the error if the backend
 * attached one via ``extensions.mathRef``. Returns ``undefined``
 * when the error did not originate from a math-divergence path —
 * the frontend never invents a link.
 */
export function resolveMathRef(err: unknown): string | undefined {
  if (err instanceof GraphqlError) {
    for (const e of err.errors) {
      const ref = readMathRef(e);
      if (ref) return ref;
    }
  }
  return undefined;
}

/**
 * Build the *what* + *try* pair for a thrown value, applying the
 * catalog when the value carries a known ``extensions.code`` and
 * otherwise echoing the message verbatim as the *what* so that
 * unmapped codes still render meaningfully.
 */
export function describeError(err: unknown, contextWhat?: string): ErrorCatalogEntry & { mathRef?: string } {
  const code = resolveErrorCode(err);
  const fromCatalog = lookupErrorEntry(code);
  const message = err instanceof Error ? err.message : (typeof err === 'string' ? err : '');
  const what = code ? fromCatalog.what : (contextWhat ?? message ?? FALLBACK.what);
  const tryCopy = fromCatalog.tryCopy;
  const mathRef = resolveMathRef(err) ?? fromCatalog.mathRef;
  return { what, tryCopy, mathRef };
}

function readCode(payload: GraphQLErrorPayload): string | undefined {
  const ext = payload.extensions;
  if (!ext) return undefined;
  const code = (ext as { code?: unknown }).code;
  return typeof code === 'string' ? code : undefined;
}

function readMathRef(payload: GraphQLErrorPayload): string | undefined {
  const ext = payload.extensions;
  if (!ext) return undefined;
  const ref = (ext as { mathRef?: unknown }).mathRef;
  return typeof ref === 'string' ? ref : undefined;
}
