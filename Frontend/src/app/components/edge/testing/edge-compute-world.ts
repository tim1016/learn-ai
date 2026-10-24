import type { HttpTestingController } from "@angular/common/http/testing";

const FIRST_BAR_MS = Date.UTC(2026, 0, 5, 14, 30);
const DAY_MS = 86_400_000;
const BARS = 40;
const IV_FROM = 5;
const BLIND_TAIL = 3;

/** Lets the Compute chain issue its next request (each hop is a promise). */
export const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function barTimestamps(): number[] {
  return Array.from({ length: BARS }, (_, i) => FIRST_BAR_MS + i * DAY_MS);
}

function aggregatesResponse() {
  return {
    success: true,
    ticker: "SPY",
    summary: {},
    data: barTimestamps().map((timestamp, i) => ({
      timestamp,
      open: 500 + i, high: 503 + i, low: 498 + i, close: 501 + i, volume: 1_000_000,
    })),
  };
}

/** The trailing RV each Yang-Zhang window measures, distinct per window so a
 *  page that reads the wrong series shows the wrong number. */
export const YZ_RV_BY_WINDOW: Readonly<Record<number, number>> = { 5: 0.31, 10: 0.22, 30: 0.14 };
const OTHER_ESTIMATOR_RV = 0.5;
const WINDOWS = [5, 10, 30];

/** What `/api/edge/realized-vs-iv/series` returns for a 40-bar daily window:
 *  IV30 from bar 5 on (or none at all), the last three forward RVs blind, and
 *  one RV series per estimator per window, estimator-major as the server
 *  orders them (`yz_5`, `yz_10`, `yz_30`). */
function seriesResponse(ivPresent: boolean, estimators: readonly string[]) {
  const ts = barTimestamps();
  const upTo = (from: number, to: number, value: (i: number) => number) =>
    ts.map((_, i) => (i >= from && i < to ? value(i) : null));
  const rvByKey = (from: number, to: number) => Object.fromEntries(
    estimators.flatMap((est) => WINDOWS.map((w) => [
      `${est}_${w}`,
      upTo(from, to, () => (est === "yz" ? YZ_RV_BY_WINDOW[w] : OTHER_ESTIMATOR_RV)),
    ])),
  );
  const iv30 = ivPresent ? upTo(IV_FROM, BARS, () => 0.21) : ts.map(() => null);
  return {
    symbol: "SPY",
    ts,
    rv_trailing: rvByKey(3, BARS),
    rv_forward: rvByKey(0, BARS - BLIND_TAIL),
    iv30,
    iv30_trd252: iv30,
    rv_hf_trailing: upTo(3, BARS, () => 0.14),
    rv_hf_forward: upTo(0, BARS - BLIND_TAIL, () => 0.15),
    vrp_forward: ivPresent ? upTo(IV_FROM, BARS - BLIND_TAIL, () => 0.06) : ts.map(() => null),
    vrp_z: ivPresent ? upTo(IV_FROM, BARS - BLIND_TAIL, (i) => (i % 2 === 0 ? 1.4 : -1.4)) : ts.map(() => null),
    iv_source: ivPresent ? "recorder" : "absent",
    coverage: {
      n_bars: BARS,
      iv_first_ts: ivPresent ? ts[IV_FROM] : null,
      iv_last_ts: ivPresent ? ts[BARS - 1] : null,
      forward_nan_bars: BLIND_TAIL,
    },
  };
}

export interface ComputeAnswer {
  /** False answers with a series that carries no IV30 at all. */
  ivPresent?: boolean;
  /** The estimators the request named, in request order; the response holds a
   *  series for each. Defaults to Yang-Zhang alone. */
  estimators?: readonly string[];
}

/** Answers the requests one Compute click makes, in order, with a small
 *  real-shaped response. The live IV30 marker endpoints fail, as they do
 *  when the chain is dark. */
export async function answerCompute(http: HttpTestingController, answer: ComputeAnswer = {}): Promise<void> {
  http.expectOne("/api/aggregates/fetch").flush(aggregatesResponse());
  await tick();
  http.expectOne("/api/edge/realized-vs-iv/series").flush(seriesResponse(answer.ivPresent ?? true, answer.estimators ?? ["yz"]));
  http.expectOne("/api/edge/iv30/vix-style").flush({ detail: "dark" }, { status: 503, statusText: "Unavailable" });
  await tick();
  http.expectOne("/api/edge/iv30/parametric").flush({ detail: "dark" }, { status: 503, statusText: "Unavailable" });
  await tick();
}

/** The bars fetch itself fails: Python answers `success: false` with its reason. */
export function failBarsFetch(http: HttpTestingController, error: string): void {
  http.expectOne("/api/aggregates/fetch").flush({ success: false, ticker: "SPY", summary: {}, data: [], error });
}
