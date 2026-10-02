import { computed, effect, inject, resource, signal, type Signal } from '@angular/core';

import type {
  CustomGate,
  CustomGateInput,
  GateCatalogueEntry,
  StrategyViewResponse,
} from '../lib/broker-v2-panel.types';
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
  /** The catalogue indicators a gate can read, as the data plane offers them. */
  readonly catalogue: Signal<readonly GateCatalogueEntry[]>;
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

interface JudgedDraft {
  readonly draft: CustomGateInput;
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
    loader: async ({ params }): Promise<GateEvaluation> => ({
      closes: params.view.candles.map((candle) => candle.bar_close_ms),
      response: await api.evaluate(params.view.strategy_key, gateEvaluationRequest(params.view)),
    }),
  });

  const draft = signal<CustomGateInput | null>(null);
  const draftJudged = resource({
    params: () => {
      const view = source();
      const pending = draft();
      return view === null || pending === null ? undefined : { view, draft: pending };
    },
    loader: async ({ params }): Promise<JudgedDraft> => {
      const response = await api.evaluate(params.view.strategy_key, gateEvaluationRequest(params.view, params.draft));
      return {
        draft: params.draft,
        evaluation: {
          closes: params.view.candles.map((candle) => candle.bar_close_ms),
          // The draft's own column: the saved gates keep the answer judged for them.
          response: { ...response, results: { [DRAFT_GATE_ID]: response.results[DRAFT_GATE_ID] ?? [] } },
        },
      };
    },
  });

  // The last settled answers, kept while a newer read is judged: a resource
  // clears its value when its params change.
  const lastSaved = signal<GateEvaluation | null>(null);
  const lastDraft = signal<JudgedDraft | null>(null);
  effect(() => {
    const status = savedJudged.status();
    if (status === 'resolved' || status === 'local') lastSaved.set(savedJudged.value() ?? null);
    else if (status === 'error' || status === 'idle') lastSaved.set(null);
  });
  effect(() => {
    const status = draftJudged.status();
    if (status === 'resolved' || status === 'local') lastDraft.set(draftJudged.value() ?? null);
    else if (status === 'error' || status === 'idle') lastDraft.set(null);
  });

  /** The previewed draft's answer, only while it is still the draft shown. */
  const shownDraft = computed(() => {
    const judged = lastDraft();
    return judged !== null && judged.draft === draft() ? judged : null;
  });
  const previewing = computed(() => shownDraft() !== null);
  const draftRefusal = computed(() => {
    const error = draftJudged.error();
    return error === undefined ? null : gateRefusalMessage(error, 'The data plane could not judge this gate.');
  });

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
    const evaluations = [lastSaved(), judged?.evaluation ?? null].filter(
      (evaluation): evaluation is GateEvaluation => evaluation !== null,
    );
    return withCustomGates(read, saved(), evaluations, judged?.draft ?? null, notices);
  });

  return {
    view,
    saved,
    catalogue: computed(() => (catalogueRead.hasValue() ? catalogueRead.value().indicators : [])),
    previewing,
    draftRefusal,
    preview: (pending) => draft.set({ ...pending }),
    clearDraft: () => draft.set(null),
    save: async (pending, gateId) => {
      const key = strategyKey();
      if (key === undefined) throw new Error('No strategy is shown, so there is nothing to save the gate on.');
      try {
        const gate = gateId === null ? await api.create(key, pending) : await api.replace(key, gateId, pending);
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
