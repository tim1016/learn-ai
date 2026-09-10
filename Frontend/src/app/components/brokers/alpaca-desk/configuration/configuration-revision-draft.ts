// The editable shape of one broker-configuration revision, and the rules for
// turning it back into the contract's request body.
//
// The draft is **flat** — the six live-envelope values sit beside the slot and
// the endpoint mode rather than nested — because Signal Forms binds a field
// tree, and a nullable nested object would make every envelope input reach
// through a branch that is `null` for a paper revision.
//
// The six envelope values start as `null`, never as a number. ADR 0059
// Decision 4 forbids a default for any of them, and a pre-filled form is a
// default with a nicer name: it would put a limit nobody chose in front of an
// operator about to bound real money. An empty field is refused by
// `draftProblems` until the operator types a value.

import type {
  BrokerCredentialSlot,
  BrokerEndpointMode,
  BrokerProfileRevision,
} from '../../../../api/alpaca.types';
import type { RevisionContent } from './broker-configuration.service';

export interface RevisionDraft {
  credential_slot: string;
  endpoint_mode: BrokerEndpointMode;
  loss_fraction: number | null;
  loss_usd: number | null;
  shadow_sessions: number | null;
  arming_max_sessions: number | null;
  xh_entry_bps: number | null;
  xh_exit_bps: number | null;
}

/** The six envelope keys, in the order the form renders them. */
export const ENVELOPE_KEYS = [
  'loss_fraction',
  'loss_usd',
  'shadow_sessions',
  'arming_max_sessions',
  'xh_entry_bps',
  'xh_exit_bps',
] as const;

export type EnvelopeKey = (typeof ENVELOPE_KEYS)[number];

export const ENVELOPE_LABELS: Readonly<Record<EnvelopeKey, string>> = {
  loss_fraction: 'Daily loss fraction',
  loss_usd: 'Daily loss cap (USD)',
  shadow_sessions: 'Shadow sessions required',
  arming_max_sessions: 'Sessions one arming covers',
  xh_entry_bps: 'Extended-hours entry offset (bps)',
  xh_exit_bps: 'Extended-hours exit offset (bps)',
};

/** A draft for a profile that does not exist yet, or for a revision's next edit. */
export function emptyDraft(credentialSlot: string): RevisionDraft {
  return {
    credential_slot: credentialSlot,
    endpoint_mode: 'paper',
    loss_fraction: null,
    loss_usd: null,
    shadow_sessions: null,
    arming_max_sessions: null,
    xh_entry_bps: null,
    xh_exit_bps: null,
  };
}

/** The draft that edits an existing revision, carrying its stored values forward. */
export function draftFromRevision(revision: BrokerProfileRevision): RevisionDraft {
  const envelope = revision.live_envelope;
  return {
    credential_slot: revision.credential_slot,
    endpoint_mode: revision.endpoint_mode,
    loss_fraction: envelope?.loss_fraction ?? null,
    loss_usd: envelope?.loss_usd ?? null,
    shadow_sessions: envelope?.shadow_sessions ?? null,
    arming_max_sessions: envelope?.arming_max_sessions ?? null,
    xh_entry_bps: envelope?.xh_entry_bps ?? null,
    xh_exit_bps: envelope?.xh_exit_bps ?? null,
  };
}

/** The first admissible slot for a new draft, preferring one with credentials injected. */
export function preferredSlot(slots: readonly BrokerCredentialSlot[]): string {
  return (slots.find((slot) => slot.available) ?? slots[0])?.slot ?? '';
}

/** `min` is exclusive, `max` is exclusive — the contract §2.4 shape for a fraction. */
function betweenExclusive(
  value: number | null,
  label: string,
  min: number,
  max: number,
): string | null {
  if (value === null || !Number.isFinite(value)) return `${label} needs a number.`;
  if (value <= min || value >= max) {
    return `${label} must be above ${min} and below ${max}.`;
  }
  return null;
}

function aboveZero(value: number | null, label: string): string | null {
  if (value === null || !Number.isFinite(value)) return `${label} needs a number.`;
  if (value <= 0) return `${label} must be above 0.`;
  return null;
}

/** Zero is admissible for an offset; the ceiling is exclusive. */
function offsetBelow(value: number | null, label: string, max: number): string | null {
  if (value === null || !Number.isFinite(value)) return `${label} needs a number.`;
  if (value < 0 || value >= max) return `${label} must be 0 or more and below ${max}.`;
  return null;
}

function wholeAtLeastOne(value: number | null, label: string): string | null {
  if (value === null || !Number.isFinite(value)) return `${label} needs a number.`;
  if (!Number.isInteger(value)) return `${label} must be a whole number.`;
  if (value < 1) return `${label} must be at least 1.`;
  return null;
}

/**
 * Whether the slot this draft names can be saved at all.
 *
 * An empty allowlist means the slot directory has not been read — which is not
 * the same as the slot being unknown, and must not be reported as if it were.
 * A slot the directory does list but that has no injected pair is *not* a
 * problem here: saving such a revision is allowed, and the form says separately
 * what it means.
 */
function slotProblemFor(
  slot: string,
  slots: readonly BrokerCredentialSlot[],
): string | null {
  if (slot.trim().length === 0) return 'Choose a credential slot.';
  if (slots.length === 0) {
    return 'The credential slots have not been read, so this revision cannot be saved yet.';
  }
  return slots.some((candidate) => candidate.slot === slot)
    ? null
    : 'This revision names a credential slot this deployment no longer lists.';
}

/**
 * What still stops this draft being saved. Empty means the request is worth
 * sending — the service stays the authority, and its refusal is rendered as it
 * arrives; these mirror its bounds only so an operator is not told "422" for a
 * blank field they can see.
 */
export function draftProblems(
  draft: RevisionDraft,
  slots: readonly BrokerCredentialSlot[] = [],
): readonly string[] {
  const slotProblem = slotProblemFor(draft.credential_slot, slots);
  if (slotProblem !== null) return [slotProblem];
  if (draft.endpoint_mode !== 'live') return [];
  return [
    betweenExclusive(draft.loss_fraction, ENVELOPE_LABELS.loss_fraction, 0, 1),
    aboveZero(draft.loss_usd, ENVELOPE_LABELS.loss_usd),
    wholeAtLeastOne(draft.shadow_sessions, ENVELOPE_LABELS.shadow_sessions),
    wholeAtLeastOne(draft.arming_max_sessions, ENVELOPE_LABELS.arming_max_sessions),
    offsetBelow(draft.xh_entry_bps, ENVELOPE_LABELS.xh_entry_bps, 10_000),
    offsetBelow(draft.xh_exit_bps, ENVELOPE_LABELS.xh_exit_bps, 10_000),
  ].filter((problem): problem is string => problem !== null);
}

/**
 * The request body for this draft. Callers must check `draftProblems` first —
 * a live draft with a missing value throws rather than substituting a number
 * nobody chose.
 */
export function toRevisionContent(draft: RevisionDraft): RevisionContent {
  if (draft.endpoint_mode !== 'live') {
    return {
      credential_slot: draft.credential_slot,
      endpoint_mode: draft.endpoint_mode,
      live_envelope: null,
    };
  }
  const {
    loss_fraction,
    loss_usd,
    shadow_sessions,
    arming_max_sessions,
    xh_entry_bps,
    xh_exit_bps,
  } = draft;
  if (
    loss_fraction === null
    || loss_usd === null
    || shadow_sessions === null
    || arming_max_sessions === null
    || xh_entry_bps === null
    || xh_exit_bps === null
  ) {
    const missing = ENVELOPE_KEYS.filter((key) => draft[key] === null);
    throw new Error(`A live revision needs every envelope value; missing: ${missing.join(', ')}.`);
  }
  return {
    credential_slot: draft.credential_slot,
    endpoint_mode: 'live',
    live_envelope: {
      loss_fraction,
      loss_usd,
      shadow_sessions,
      arming_max_sessions,
      xh_entry_bps,
      xh_exit_bps,
    },
  };
}
