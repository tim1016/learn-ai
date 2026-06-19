/**
 * Action-plan TypeScript mirror of the Pydantic shape in
 * ``PythonDataService/app/schemas/action_plan.py``. The Python model is
 * authoritative; this file is updated in lockstep when leg variants
 * land.
 *
 * Slice 1A (#594): empty-plan envelope.
 * Slice 1B (#595): stock entry leg + ``close_leg`` exit reference.
 * Slice 1C (#596): option entry leg + strike / expiry selectors (this file).
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

export interface OptionInstrument {
  kind: 'option';
  underlying: string;
}

// ---- Strike selectors (Slice 1C) ------------------------------------------
// ``delta`` is deliberately absent until Slice 6 wires its resolver
// (ADR 0012 §"Anti-patterns").

export interface AtmStrike {
  selector: 'atm';
}

export interface AtmOffsetStrike {
  selector: 'atm_offset';
  offset: number;
}

export type StrikeSelector = AtmStrike | AtmOffsetStrike;

// ---- Expiry selectors (Slice 1C) ------------------------------------------

export interface MinDteExpiry {
  selector: 'min_dte';
  days: number;
}

export interface NearestWeeklyExpiry {
  selector: 'nearest_weekly';
}

export interface AbsoluteExpiry {
  selector: 'absolute';
  /** ``int64`` ms UTC per the repo timestamp policy. Display conversion
   * to ``America/New_York`` lives at the UI boundary. */
  expiration_ms: number;
}

export type ExpirySelector = MinDteExpiry | NearestWeeklyExpiry | AbsoluteExpiry;

// ---- Entry-leg variants ---------------------------------------------------

export interface StockEntryLeg {
  leg_id: LegId;
  instrument: StockInstrument;
  position: 'long' | 'short';
  /** Declarative positive integer. Composition against
   * ``live_config.sizing`` is deferred to Slice 4 (ADR 0012 §4). */
  qty_ratio: number;
}

export interface OptionEntryLeg {
  leg_id: LegId;
  instrument: OptionInstrument;
  position: 'long' | 'short';
  qty_ratio: number;
  right: 'call' | 'put';
  strike: StrikeSelector;
  expiry: ExpirySelector;
}

export type ActionPlanEntryLeg = StockEntryLeg | OptionEntryLeg;

export interface CloseLegExit {
  kind: 'close_leg';
  entry_leg_id: LegId;
}

/** Slice 1B: only ``close_leg``. Future lifecycle actions (``roll``,
 * etc.) extend the discriminated union here. */
export type ActionPlanExitEntity = CloseLegExit;

/** Narrowing helper — TS doesn't reliably infer the discriminated leg
 * variant just from ``leg.instrument.kind`` when the parent type is the
 * union, so the picker / card use this to switch on the variant in one
 * place rather than scattering ``leg.instrument.kind === 'option'`` checks. */
export function isOptionLeg(leg: ActionPlanEntryLeg): leg is OptionEntryLeg {
  return leg.instrument.kind === 'option';
}
