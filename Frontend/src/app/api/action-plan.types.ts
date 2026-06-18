/**
 * PRD #593 Slice 1A — action-plan TypeScript mirror of the Pydantic shape
 * in ``PythonDataService/app/schemas/action_plan.py``. The Python model is
 * authoritative; this file is updated in lockstep when Slices 1B (#595)
 * and 1C (#596) land stock and option leg variants.
 *
 * Slice 1A ships only the empty-plan envelope so the cockpit card, the
 * type system, and the `/status` round-trip have a stable container to
 * exchange before the leg shapes arrive.
 */

/** Operator-declared instrument plan for a live run. Hashed into
 * ``run_id`` via ``live_config.action`` (ledger key). Engine consumption
 * is deferred to Slice 4 (ADR 0012 §"Scope") — the cockpit labels any
 * declared plan as "not active until Slice 4". */
export interface ActionPlan {
  /** Legs the bot opens when the strategy emits an entry signal.
   * Leg shapes land incrementally in #595 (stock) and #596 (option). */
  on_enter: ActionPlanEntryLeg[];
  /** Lifecycle actions on the entry legs. Slice 1 ships only the
   * ``close_leg`` variant (lands in #595). */
  on_exit: ActionPlanExitEntity[];
}

/** Placeholder union — variants ship in #595 (stock) and #596 (option).
 * Slice 1A only round-trips the empty-list case. */
export type ActionPlanEntryLeg = Record<string, unknown>;

/** Placeholder union — ``close_leg`` ships in #595. */
export type ActionPlanExitEntity = Record<string, unknown>;
