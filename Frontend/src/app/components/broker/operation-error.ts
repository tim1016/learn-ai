// Reads a rejected broker/control-plane operation's literal server-authored
// message. Legacy FastAPI string `detail`, FastAPI's structured
// `{detail: {message}}`, and the fleet's flat `{reason, message, next_step}`
// body are all supported.

import { isRecord } from '../../api/operator-blocker.types';
import { refusalBody } from '../../shared/errors/refusal-body';

/**
 * Reads the literal FastAPI error detail without deriving any operator copy.
 * Both legacy string details and structured server-message contracts are
 * supported so desk surfaces do not hide actionable server responses.
 *
 * Reads `refusalBody` first — the same shape-aware parser
 * `deriveActionRejection` uses — so the fleet's flat `{reason, message,
 * next_step}` body (which `refusalBody` handles alongside FastAPI's nested
 * `{detail: {...}}` envelope) is read here too. This is the third of three
 * parsers that read a rejected control-plane call's body (#2081 fixed the
 * other two); without it, a fleet refusal reaching this surface fell through
 * to `fallback` because `error.error.detail` is never an object for a flat
 * body.
 */
export function extractServerMessage(error: unknown, fallback: string): string {
  const refusal = refusalBody(error);
  if (refusal !== null && typeof refusal['message'] === 'string') return refusal['message'];
  if (!isRecord(error) || !isRecord(error['error'])) return fallback;
  const detail = error['error']['detail'];
  if (typeof detail === 'string') return detail;
  return isRecord(detail) && typeof detail['message'] === 'string' ? detail['message'] : fallback;
}
