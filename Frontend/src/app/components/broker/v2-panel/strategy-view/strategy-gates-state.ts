import { computed, inject, linkedSignal, resource, type Signal } from '@angular/core';

import type {
  CustomGate,
  CustomGateInput,
  GateCatalogueEntry,
  StrategyViewResponse,
} from '../lib/broker-v2-panel.types';
import { settledRead } from './settled-read';
import { StrategyGatesService } from './strategy-gates.service';
import {
  DRAFT_GATE_ID,
  gateEvaluationRequest,
  gateRefusalMessage,
  withCustomGates,
  type GateEvaluation,
} from './strategy-gates-model';

/** What the chart panel needs to show and edit one strategy's custom gates. */
export interface StrategyGatesState {
  /** The read view with the saved gates (and a previewed draft) folded in. */
  readonly view: Signal<StrategyViewResponse | null>;
  readonly saved: Signal<readonly CustomGate[]>;
  /** The catalogue indicators a gate can read, as the data plane offers them; `null` when it could not list them. */
  readonly catalogue: Signal<readonly GateCatalogueEntry[] | null>;
  /** The draft being previewed, once the data plane has judged it. */
  readonly previewing: Signal<boolean>;
  /** Why the data plane refused the draft, in its words. */
  readonly draftRefusal: Signal<string | null>;
  preview(draft: CustomGateInput): void;
  clearDraft(): void;
  /** Save a new gate (or replace `gateId`); rejects with the data plane's reason. */
  save(draft: CustomGateInput, gateId: string | null): Promise<CustomGate>;
  /** Delete a saved gate; rejects with the data plane's reason. */
  remove(gateId: string): Promise<void>;
}

/** A previewed draft and the data plane's answer, or its reason for refusing it. */
interface JudgedDraft {
  readonly draft: CustomGateInput;
  readonly evaluation: GateEvaluation | null;
  readonly refusal: string | null;
}

interface JudgedSaved {
  /** Each judged gate's version, so an answer for a since-edited gate is not shown for its new expression. */
  readonly versions: ReadonlyMap<string, number>;
  readonly evaluation: GateEvaluation;
}

/**
 * One strategy's custom gates for a strategy view (#2639 D8–D10). Call it in
 * an injection context, with the view the host read.
 *
 * The saved list is read per strategy; the saved gates are judged on the
 * view's own candles whenever either changes, and a draft is judged apart
 * so a refused draft never blanks the saved gates' shading. While a newer
 * read is judged, the last answer stays on screen: results are matched to
 * candles by bar close, so they never land on the wrong bar. A list or
 * judgement the data plane could not answer becomes a notice on the view,
 * never a broken chart.
 */
export function strategyGatesState(source: Signal<StrategyViewResponse | null>): StrategyGatesState {
  const api = inject(StrategyGatesService);
  const strategyKey = computed(() => source()?.strategy_key);

  const catalogueRead = resource({ loader: () => api.catalogue() });
  const savedRead = resource({
    params: () => strategyKey(),
    loader: ({ params }) => api.list(params).then((list) => list.gates),
  });
  const saved = computed((): readonly CustomGate[] => (savedRead.hasValue() ? savedRead.value() : []));

  const savedJudged = resource({
    params: () => {
      const view = source();
      const gates = saved();
      return view === null || gates.length === 0 ? undefined : { view, gates };
    },
    loader: async ({ params }): Promise<JudgedSaved> => ({
      versions: new Map(params.gates.map((gate) => [gate.gate_id, gate.updated_at_ms])),
      evaluation: {
        closes: params.view.candles.map((candle) => candle.bar_close_ms),
        response: await api.evaluate(params.view.strategy_key, gateEvaluationRequest(params.view)),
      },
    }),
  });

  // A draft belongs to the strategy it was written on: another strategy shown
  // in this panel starts with none. Kept while the same strategy is re-read.
  const draft = linkedSignal<string | undefined, CustomGateInput | null>({
    source: strategyKey,
    computation: (key, previous) => (previous !== undefined && previous.source === key ? previous.value : null),
  });
  const draftJudged = resource({
    params: () => {
      const view = source();
      const pending = draft();
      return view === null || pending === null ? undefined : { view, draft: pending };
    },
    // A refusal is an answer about this draft, kept with it rather than thrown,
    // so it is never shown against a newer draft still being judged.
    loader: async ({ params }): Promise<JudgedDraft> => {
      try {
        const response = await api.evaluate(params.view.strategy_key, gateEvaluationRequest(params.view, params.draft));
        return {
          draft: params.draft,
          refusal: null,
          evaluation: {
            closes: params.view.candles.map((candle) => candle.bar_close_ms),
            // The draft's own column: the saved gates keep the answer judged for them.
            response: { ...response, results: { [DRAFT_GATE_ID]: response.results[DRAFT_GATE_ID] ?? [] } },
          },
        };
      } catch (error) {
        return {
          draft: params.draft,
          evaluation: null,
          refusal: gateRefusalMessage(error, 'The data plane could not judge this gate.'),
        };
      }
    },
  });

  // The last settled answers (and refusals), kept while a newer read is judged.
  const savedSettled = settledRead(savedJudged);
  const draftSettled = settledRead(draftJudged);

  /** The saved gates' answer, for the gates as they are now: an edited gate waits for its own. */
  const savedEvaluation = computed((): GateEvaluation | null => {
    const judged = savedSettled().value;
    if (judged === null) return null;
    const current = new Map(saved().map((gate) => [gate.gate_id, gate.updated_at_ms]));
    const results = Object.fromEntries(
      Object.entries(judged.evaluation.response.results).filter(
        ([gateId]) => current.get(gateId) !== undefined && current.get(gateId) === judged.versions.get(gateId),
      ),
    );
    return { ...judged.evaluation, response: { ...judged.evaluation.response, results } };
  });
  /** The previewed draft's answer or refusal, only while it is still the draft shown. */
  const shownDraft = computed(() => {
    const judged = draftSettled().value;
    return judged !== null && judged.draft === draft() ? judged : null;
  });
  const previewing = computed(() => (shownDraft()?.evaluation ?? null) !== null);
  const draftRefusal = computed(() => shownDraft()?.refusal ?? null);

  const view = computed((): StrategyViewResponse | null => {
    const read = source();
    if (read === null) return null;
    const notices: string[] = [];
    const listError = savedRead.error();
    if (listError !== undefined) {
      notices.push(`Saved gates could not be loaded: ${gateRefusalMessage(listError, 'the data plane did not answer.')}`);
    }
    const judgeError = savedJudged.error();
    if (judgeError !== undefined) {
      notices.push(`Saved gates could not be judged: ${gateRefusalMessage(judgeError, 'the data plane did not answer.')}`);
    }
    const judged = shownDraft();
    const evaluations = [savedEvaluation(), judged?.evaluation ?? null].filter(
      (evaluation): evaluation is GateEvaluation => evaluation !== null,
    );
    return withCustomGates(read, saved(), evaluations, previewing() ? draft() : null, notices);
  });

  return {
    view,
    saved,
    catalogue: computed(() => {
      if (catalogueRead.error() !== undefined) return null;
      return catalogueRead.hasValue() ? catalogueRead.value().indicators : [];
    }),
    previewing,
    draftRefusal,
    preview: (pending) => draft.set({ ...pending }),
    clearDraft: () => draft.set(null),
    save: async (pending, gateId) => {
      const shown = source();
      if (shown === null) throw new Error('No strategy is shown, so there is nothing to save the gate on.');
      const key = shown.strategy_key;
      // Checked under the settings it was previewed under: a recorded name follows them.
      const toSave = { ...pending, settings: { ...(shown.settings ?? {}) } };
      try {
        const gate = gateId === null ? await api.create(key, toSave) : await api.replace(key, gateId, toSave);
        draft.set(null);
        savedRead.reload();
        return gate;
      } catch (error) {
        throw new Error(gateRefusalMessage(error, 'The gate could not be saved.'), { cause: error });
      }
    },
    remove: async (gateId) => {
      const key = strategyKey();
      if (key === undefined) return;
      try {
        await api.remove(key, gateId);
        savedRead.reload();
      } catch (error) {
        throw new Error(gateRefusalMessage(error, 'The gate could not be deleted.'), { cause: error });
      }
    },
  };
}
