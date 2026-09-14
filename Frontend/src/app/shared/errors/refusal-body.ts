import { HttpErrorResponse } from '@angular/common/http';

/**
 * The refusal body of a rejected control-plane call, in whichever shape it arrived.
 *
 * Two shapes are live at once, and a parser that knows only one silences the other:
 *
 * - **Flat** `{reason, message, next_step}` — what the broker clerk fleet coordinator
 *   returns. `FleetControlError.detail()` *builds* the body; it does not nest it under
 *   a `detail` member, despite the method's name.
 * - **Nested** `{detail: {...}}` — FastAPI's `HTTPException` envelope, which the
 *   pre-fleet routes still use.
 *
 * Reading only the nested shape made every fleet refusal unreadable: the parser
 * returned `null`, the surface fell back to generic prose, and none of the
 * coordinator's typed refusal families could reach an operator.
 *
 * Returns the object a caller should read `reason` / `message` / `next_step` from,
 * or `null` when the response carries no refusal body at all.
 */
export function refusalBody(error: unknown): Record<string, unknown> | null {
  if (!(error instanceof HttpErrorResponse)) return null;
  const body: unknown = error.error;
  if (!isRecord(body)) return null;

  // Nested first: when both are present the explicit envelope wins, so a
  // legacy body that also happens to carry a top-level `message` is unchanged.
  const nested: unknown = body['detail'];
  if (isRecord(nested)) return nested;

  // Flat: accept it only when it actually looks like a refusal, so an
  // unrelated JSON error body is still reported as `null` rather than
  // being mined for fields it never meant to carry.
  return typeof body['reason'] === 'string' || typeof body['message'] === 'string' ? body : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
