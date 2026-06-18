/**
 * Action-plan TypeScript mirror of the Pydantic shape in
 * ``PythonDataService/app/schemas/action_plan.py``. The Python model is
 * authoritative; this file is updated in lockstep when leg variants
 * land. Cross-language parity is asserted via shared JSON fixtures under
 * ``PythonDataService/tests/fixtures/action_plan/`` (Python via
 * Pydantic, frontend via Vitest + ``validateActionPlan``).
 *
 * Slice 1A (#594): empty-plan envelope.
 * Slice 1B (#595): stock entry leg + ``close_leg`` exit reference (this file).
 * Slice 1C (#596): option entry leg + strike/expiry selectors (future).
 */

/** Operator-declared instrument plan for a live run. Hashed into
 * ``run_id`` via ``live_config.action`` (ledger key). Engine consumption
 * is deferred to Slice 4 (ADR 0012 §"Scope") — the cockpit labels any
 * declared plan as "not active until Slice 4". */
export interface ActionPlan {
  on_enter: ActionPlanEntryLeg[];
  on_exit: ActionPlanExitEntity[];
}

/** Stable, lowercase ``[a-z0-9_]`` identifier (1-32 chars) — see
 * ADR 0012 §3. Exits reference entries by this id. */
export type LegId = string;

export interface StockInstrument {
  kind: 'stock';
  underlying: string;
}

/** Slice 1B: stock entry leg. Option variants land in #596 and the
 * union widens accordingly. */
export interface StockEntryLeg {
  leg_id: LegId;
  instrument: StockInstrument;
  position: 'long' | 'short';
  /** Declarative positive integer. Composition against
   * ``live_config.sizing`` is deferred to Slice 4 (ADR 0012 §4). */
  qty_ratio: number;
}

/** Slice 1B: stock-only. Option leg variants extend the union in #596. */
export type ActionPlanEntryLeg = StockEntryLeg;

export interface CloseLegExit {
  kind: 'close_leg';
  entry_leg_id: LegId;
}

/** Slice 1B: only ``close_leg``. Future lifecycle actions (``roll``,
 * etc.) extend the discriminated union here. */
export type ActionPlanExitEntity = CloseLegExit;
