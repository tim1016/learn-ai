import { HttpErrorResponse } from "@angular/common/http";
import type { HttpTestingController } from "@angular/common/http/testing";
import type { LakeDataSourceRaw } from "../../../shared/lake-study/lake-study";
import type { JobStreamEvent } from "../../../services/jobs.service";

const FIRST_BAR_MS = Date.UTC(2026, 0, 5, 14, 30);
const DAY_MS = 86_400_000;
const BARS = 40;
const IV_FROM = 5;
const BLIND_TAIL = 3;
const JOB_ID_PREFIX = "job-rviv-";

/** Lets the Compute chain issue its next request (each hop is a promise). */
export const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function barTimestamps(): number[] {
  return Array.from({ length: BARS }, (_, i) => FIRST_BAR_MS + i * DAY_MS);
}

/** The trailing RV each Yang-Zhang window measures, distinct per window so a
 *  page that reads the wrong series shows the wrong number. */
export const YZ_RV_BY_WINDOW: Readonly<Record<number, number>> = { 5: 0.31, 10: 0.22, 30: 0.14 };
const OTHER_ESTIMATOR_RV = 0.5;
const WINDOWS = [5, 10, 30];

const emptyGroup = { expected_sessions: 0, reused: 0, added: 0, missing: 0, failed: 0, building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 0 };

/** The `data_source` block a lake study writes, as the server sends it. */
export function lakeDataSource(over: Partial<LakeDataSourceRaw> = {}): LakeDataSourceRaw {
  return {
    kind: "lake",
    provider: "polygon",
    symbol: "SPY",
    price_adjustment_mode: "polygon_split_adjusted",
    session_policy: "regular",
    window_start_ms: Date.UTC(2025, 0, 6, 5),
    window_end_ms: Date.UTC(2026, 0, 2, 5),
    effective_window: { start_ms: Date.UTC(2025, 0, 6, 5), end_ms: Date.UTC(2026, 0, 2, 5) },
    warmup_sessions: 0,
    bar_minutes: 1440,
    aggregation: "minute_fold_et_wall_clock_no_fill",
    identity_digest: "a".repeat(64),
    calculation_version: "realized-vs-iv-study/2",
    snapshot_digest: "b".repeat(64),
    coverage: {
      window: { ...emptyGroup, expected_sessions: 40, reused: 30, added: 10 },
      warmup: emptyGroup,
      artifacts: { fetched: 10, reused: 30, refreshed: 0 },
    },
    bars_used: 40,
    cache: { hit: false, cached_at_ms: null },
    ...over,
  };
}

export interface ComputeAnswer {
  /** False answers with a series that carries no IV30 at all. */
  ivPresent?: boolean;
  /** The estimators the request named, in request order; the response holds a
   *  series for each. Defaults to Yang-Zhang alone. */
  estimators?: readonly string[];
  /** The settings the server echoes back as the run's. Default: daily bars at a 30-day tenor. */
  run?: { bar_size: "15m" | "1d"; tenor_days: number; hf_session: "ETH" | "RTH" | null };
  /** What the live IV30 endpoints answer. Default: both dark. */
  live?: "dark" | "vix" | "parametric-only";
  /** The IV source label the server reports. Default: `recorder` when IV is present, else `absent`. */
  ivSource?: "caller_supplied" | "recorder" | "absent";
  /** The IV snapshots the server read and what became of them. Default: none read. */
  alignment?: { snapshots_read: number; bars_with_iv: number; superseded: number; outside_bars: number };
  /** Overrides on the lake `data_source` block. */
  dataSource?: Partial<LakeDataSourceRaw>;
  /** How many IV points the run read, for the data source's IV identity. Default: those the series holds. */
  ivSnapshots?: number;
}

/** What the `realized_vs_iv` job parks as its result for a 40-bar window:
 *  IV30 from bar 5 on (or none at all), the last three forward RVs blind, and
 *  one RV series per estimator per window, estimator-major as the server
 *  orders them (`yz_5`, `yz_10`, `yz_30`). */
export function resultResponse(answer: ComputeAnswer = {}) {
  const ivPresent = answer.ivPresent ?? true;
  const estimators = answer.estimators ?? ["yz"];
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
  const run = answer.run ?? { bar_size: "1d", tenor_days: 30, hf_session: null };
  return {
    symbol: "SPY",
    ts,
    bars: {
      open: ts.map((_, i) => 500 + i),
      high: ts.map((_, i) => 503 + i),
      low: ts.map((_, i) => 498 + i),
      close: ts.map((_, i) => 501 + i),
      volume: ts.map(() => 1_000_000),
    },
    rv_trailing: rvByKey(3, BARS),
    rv_forward: rvByKey(0, BARS - BLIND_TAIL),
    iv30,
    iv30_trd252: iv30,
    rv_hf_trailing: upTo(3, BARS, () => 0.14),
    rv_hf_forward: upTo(0, BARS - BLIND_TAIL, () => 0.15),
    vrp_forward: ivPresent ? upTo(IV_FROM, BARS - BLIND_TAIL, () => 0.06) : ts.map(() => null),
    vrp_z: ivPresent ? upTo(IV_FROM, BARS - BLIND_TAIL, (i) => (i % 2 === 0 ? 1.4 : -1.4)) : ts.map(() => null),
    iv_source: answer.ivSource ?? (ivPresent ? "recorder" : "absent"),
    ...run,
    coverage: {
      n_bars: BARS,
      iv_first_ts: ivPresent ? ts[IV_FROM] : null,
      iv_last_ts: ivPresent ? ts[BARS - 1] : null,
      forward_nan_bars: BLIND_TAIL,
      vrp_lookback: BARS - 1,
      session: run.hf_session,
      iv_alignment: {
        rule: "snapshot_in_bar_latest",
        ...(answer.alignment ?? (ivPresent
          ? { snapshots_read: BARS - IV_FROM, bars_with_iv: BARS - IV_FROM, superseded: 0, outside_bars: 0 }
          : { snapshots_read: 0, bars_with_iv: 0, superseded: 0, outside_bars: 0 })),
      },
    },
    data_source: {
      ...lakeDataSource(answer.dataSource),
      iv_source: answer.ivSource ?? (ivPresent ? "recorder" : "absent"),
      iv_snapshots: answer.ivSnapshots ?? (ivPresent ? BARS - IV_FROM : 0),
      iv_digest: "c".repeat(64),
    },
  };
}

type Handler = (event: JobStreamEvent, eventId: string) => void;

/** The part of `JobsService` the Realized vs IV page uses, with the server's side played by the test: it records what
 *  was started, replays and delivers events like the real service's `onEvent`, and holds the result a job parked.
 *  Each start gets its own job; `complete`, `fail` and `emit` speak for the latest one. */
export class FakeJobs {
  readonly started: { type: string; payload: Record<string, unknown> }[] = [];
  private readonly parked = new Map<string, unknown>();
  private readonly retained = new Map<string, JobStreamEvent[]>();
  private readonly listeners = new Map<string, Set<Handler>>();

  /** The id of the job most recently started. */
  get currentId(): string {
    return `${JOB_ID_PREFIX}${this.started.length}`;
  }

  async startJob(type: string, payload: Record<string, unknown>): Promise<string> {
    this.started.push({ type, payload });
    return this.currentId;
  }

  onEvent(id: string, handler: Handler): () => void {
    for (const event of this.retained.get(id) ?? []) handler(event, "");
    const handlers = this.listeners.get(id) ?? new Set<Handler>();
    handlers.add(handler);
    this.listeners.set(id, handlers);
    return () => handlers.delete(handler);
  }

  async fetchResult<T = unknown>(id: string): Promise<T> {
    if (!this.parked.has(id)) {
      throw new HttpErrorResponse({ status: 404, statusText: "Not Found", url: `/api/jobs/${id}/result` });
    }
    return this.parked.get(id) as T;
  }

  /** The worker finished: it parks `result` and emits its log lines and the terminal event. */
  complete(result: unknown, logs: readonly string[] = []): void {
    this.parked.set(this.currentId, result);
    this.deliver(logs, { type: "job.completed", result_url: `/api/jobs/${this.currentId}/result` });
  }

  /** The worker failed. A refused window parks its payload; any other failure parks nothing. */
  fail(code: string, message: string, parked?: unknown): void {
    if (parked !== undefined) this.parked.set(this.currentId, parked);
    this.deliver([], { type: "job.failed", code, message });
  }

  private deliver(logs: readonly string[], terminal: JobStreamEvent): void {
    for (const message of logs) this.emit({ type: "job.log", level: "info", message });
    this.emit(terminal);
  }

  emit(event: JobStreamEvent): void {
    const id = this.currentId;
    this.retained.set(id, [...(this.retained.get(id) ?? []), event]);
    for (const listener of [...(this.listeners.get(id) ?? [])]) listener(event, "");
  }
}

export interface ComputeWorld {
  http: HttpTestingController;
  jobs: FakeJobs;
}

function marker(method: "vix_style" | "parametric") {
  return {
    symbol: "SPY",
    method,
    target_calendar_days: 30,
    iv30_act365: 0.213,
    spot: 591,
    rate: 0.045,
    dividend_yield: 0.015,
    rate_source: "fred",
    dividend_source: "polygon",
    expiries_used_calendar_days: [28, 35],
    snapshot_ts_ms: Date.UTC(2026, 2, 2, 20, 0),
    iv_provenance: {
      iv_source: method,
      price_source_mix: { opra_mid: 1 },
      variance_contribution_synthetic: 0.04,
      strike_coverage_score: 0.92,
    },
  };
}

/** Answers the live IV30 marker requests one Compute click makes: both dark, as when the chain is dark, or one answering. */
export async function answerLiveIv(http: HttpTestingController, live: ComputeAnswer["live"] = "dark"): Promise<void> {
  const dark = { status: 503, statusText: "Unavailable" };
  if (live === "vix") {
    http.expectOne("/api/edge/iv30/vix-style").flush(marker("vix_style"));
    return;
  }
  http.expectOne("/api/edge/iv30/vix-style").flush({ detail: "the chain is dark" }, dark);
  await tick();
  if (live === "parametric-only") http.expectOne("/api/edge/iv30/parametric").flush(marker("parametric"));
  else http.expectOne("/api/edge/iv30/parametric").flush({ detail: "the chain is dark" }, dark);
}

/** Answers what one Compute click starts, in order: the job is started and the live marker is asked for, then the
 *  worker finishes with a small real-shaped result. */
export async function answerCompute(world: ComputeWorld, answer: ComputeAnswer = {}): Promise<void> {
  await tick();
  world.jobs.complete(resultResponse(answer), ["Checking the lake for SPY daily bars", "Loaded 40 bars from 15,600 minute bars"]);
  await answerLiveIv(world.http, answer.live);
  await tick();
}

/** The worker failed for a reason other than a refused window: nothing is parked. */
export async function failCompute(world: ComputeWorld, message: string): Promise<void> {
  await tick();
  world.jobs.fail("internal_error", message);
  await answerLiveIv(world.http);
  await tick();
}

/** The lake could not supply the bars: the job fails with the refusal's code and parks its payload. */
export async function refuseCompute(world: ComputeWorld, refusal: Record<string, unknown>): Promise<void> {
  await tick();
  const refused = refusal["refusal"] as { code: string; reason: string };
  world.jobs.fail(refused.code, refused.reason, refusal);
  await answerLiveIv(world.http);
  await tick();
}

/** `GET /api/edge/realized-vs-iv/window`, the path the page asks for the window a run reads. */
export const WINDOW_PATH = "/api/edge/realized-vs-iv/window";

/** The window the server resolves for a bar size and session: ET midnights, `int64 ms UTC`. */
export interface WindowAnswer {
  bar_size?: "15m" | "1d";
  session?: "rth" | "extended";
  window_start_ms?: number;
  window_end_ms?: number;
  sessions?: number;
}

/** The window of 273 sessions ending 2024-12-30 that the server resolves for daily bars at 2025-01-01 00:00 UTC. */
export function windowResponse(asked: { bar_size: "15m" | "1d"; session: "rth" | "extended" }, over: WindowAnswer = {}) {
  return {
    window_start_ms: Date.UTC(2023, 10, 29, 5),
    window_end_ms: Date.UTC(2024, 11, 30, 5),
    sessions: 273,
    ...asked,
    ...over,
  };
}

/** A window request the page has open: what it asked for, and the answers a test can give it. */
export interface WindowRequest {
  readonly bar_size: string | null;
  readonly session: string | null;
  /** Answers with the window the server would resolve for what was asked, over which a test may put its own values. */
  answer(over?: WindowAnswer): void;
  fail(body: unknown, status: number): void;
}

/**
 * Takes the newest window request the page has open, which is the one it is waiting on. Requests it abandoned for a newer
 * one (a cancelled request, or an older one whose answer it would ignore) are discarded with it.
 */
export function takeWindowRequest(http: HttpTestingController): WindowRequest {
  const open = http.match((request) => request.url === WINDOW_PATH).filter((request) => !request.cancelled);
  const request = open[open.length - 1];
  if (request === undefined) throw new Error("the page has no window request open");
  const bar_size = request.request.params.get("bar_size");
  const session = request.request.params.get("session");
  return {
    bar_size,
    session,
    answer: (over = {}) => request.flush(windowResponse({ bar_size: bar_size as "15m" | "1d", session: session as "rth" | "extended" }, over)),
    fail: (body, status) => request.flush(body as Record<string, unknown>, { status, statusText: "Unavailable" }),
  };
}

/** Answers the newest open window request with the window the server would resolve for what it asked. */
export async function answerWindow(http: HttpTestingController, over: WindowAnswer = {}): Promise<void> {
  takeWindowRequest(http).answer(over);
  await tick();
}
