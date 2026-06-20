// PRD #607 / Slice 3 (#610) — typed banner-pill descriptions.
//
// The banner pills (FLEET, STATE, INTENT, SAFETY, LAST RUN) are a
// different cut from the System Health connectivity rows, so they need
// a dedicated tooltip map rather than rebinding ``healthRows().guide``.
// This module is the SINGLE source of operator-language pill copy.
// Adding a new pill is a typed addition to the union + a closure-test
// failure that points at the missing entry.

export type BannerPillId =
  | 'fleet'
  | 'state'
  | 'intent'
  | 'safety'
  | 'last_run';

export interface PillDescription {
  /** Short label rendered on the pill itself. */
  label: string;
  /** Operator-language tooltip body (rendered as ``[title]``). */
  description: string;
  /** Screen-reader supplement so the tooltip's intent is announced. */
  ariaHint: string;
}

export const PILL_DESCRIPTIONS: Record<BannerPillId, PillDescription> = {
  fleet: {
    label: 'FLEET',
    description:
      'Aggregate cockpit verdict across this bot — STEADY when nothing needs attention, otherwise CONFIGURE or BLOCKED.',
    ariaHint: 'Fleet verdict',
  },
  state: {
    label: 'STATE',
    description:
      'Live host-process state (running / stopping / exited / idle) reported by the host daemon.',
    ariaHint: 'Process state',
  },
  intent: {
    label: 'INTENT',
    description:
      'Durable operator intent (RUNNING / PAUSED / STOPPED).  Gates the next host start even when no daemon is bound.',
    ariaHint: 'Operator intent',
  },
  safety: {
    label: 'SAFETY',
    description:
      'Server-authored safety verdict from operator_surface.broker.safety_verdict — PAPER, LIVE, DEGRADED, DISCONNECTED, or UNKNOWN.',
    ariaHint: 'Broker safety verdict',
  },
  last_run: {
    label: 'LAST RUN',
    description:
      'Classification of the most recent terminated run from operator_surface.prior_run.classification.',
    ariaHint: 'Prior-run classification',
  },
};

export function pillDescription(id: BannerPillId): PillDescription {
  return PILL_DESCRIPTIONS[id];
}
