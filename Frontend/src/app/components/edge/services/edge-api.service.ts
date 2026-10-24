import { HttpClient, HttpErrorResponse } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { firstValueFrom } from "rxjs";
import type { components } from "../../../api/broker.types";
import { JobsService } from "../../../services/jobs.service";
import {
  readLakeStudyRefusal,
  type LakeStudyRefused,
} from "../../../shared/lake-study/lake-study";
import type { Session } from "../../../shared/ticker-range-picker/ticker-range-picker.types";
import {
  RV_WINDOWS,
  type BarSize, type HfSession, type LiveIv30Marker, type LiveIv30State, type RealizedVsIvData,
} from "./edge-data.model";
import { projectRealizedVsIvResult, WIRE_BAR_SIZE, type RealizedVsIvResultRaw } from "./realized-vs-iv-projection";

interface Iv30LiveResponse {
  symbol: string;
  method: "vix_style" | "parametric";
  target_calendar_days: number;
  iv30_act365: number;
  spot: number;
  rate: number;
  dividend_yield: number;
  rate_source: string;
  dividend_source: string;
  expiries_used_calendar_days: number[];
  snapshot_ts_ms: number;
  iv_provenance: {
    iv_source: string;
    price_source_mix: Record<string, number>;
    variance_contribution_synthetic: number;
    strike_coverage_score: number;
  };
}

export type { BarSize };
export type Tenor = "7D" | "14D" | "30D" | "60D";
export type Estimator = "ctc" | "parkinson" | "gk" | "yz";

/** The window a run reads when it names none, as the server resolves it (`GET /api/edge/realized-vs-iv/window`). */
export interface DefaultWindow {
  readonly barSize: BarSize;
  /** The session the window was resolved for: a regular-hours bar holds fewer bars a session than an extended one. */
  readonly session: Session;
  /** ET midnight of the first and last session, `int64 ms UTC`: the values a run request takes. */
  readonly windowStartMs: number;
  readonly windowEndMs: number;
  /** Scheduled sessions from the first to the last. */
  readonly sessions: number;
}

export interface ComputeRvIvRequest {
  symbol: string;
  barSize: BarSize;
  tenor: Tenor;
  estimators: readonly Estimator[];
  /**
   * The session the bars are folded from. Daily bars read regular hours only (the page offers no choice); a 15-minute
   * run reads either, and its HF estimator follows it: regular hours use the HF estimator's RTH session, extended hours
   * its ETH one, so the page has one session control and the two cannot disagree.
   */
  session: Session;
  /** The window the run reads. Omitted, the server resolves its default window when the job starts. */
  window?: { readonly startMs: number; readonly endMs: number };
  windows?: readonly number[];
  /** The saved run these inputs were filled in from; the new run's record names it. */
  rerunOf?: string;
}

/** The HF estimator's session for the session a 15-minute run folds its bars from. */
export function hfSessionFor(session: Session): HfSession {
  return session === "rth" ? "RTH" : "ETH";
}

/** The lake could not supply the study's bars: the reason and the counts admission reached, for the data-source line. */
export class LakeStudyRefusedError extends Error {
  constructor(readonly refused: LakeStudyRefused) {
    super(refused.refusal.reason);
    this.name = "LakeStudyRefusedError";
  }
}

type JobEnd =
  | { kind: "completed" }
  | { kind: "failed"; message: string }
  | { kind: "cancelled"; reason: string };

@Injectable({ providedIn: "root" })
export class EdgeApiService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);

  /** Start the `realized_vs_iv` job (the worker reads the bars from the data lake, never the browser), wait for it, and
   *  project its result into the RealizedVsIvData shape used by the chart components. Also fires a parallel live-IV30
   *  fetch (vix-style → parametric fallback) and attaches the result, or why there is none, so the page can show the
   *  current-IV marker without it ever being placed on a past date.
   *
   *  A run the lake could not supply rejects with a {@link LakeStudyRefusedError} carrying the reason and the counts;
   *  any other failure rejects with its message. There is no fallback dataset. `onStatus` receives the job's log lines. */
  async computeRealizedVsIv(req: ComputeRvIvRequest, onStatus?: (message: string) => void): Promise<RealizedVsIvData> {
    const tenorDays = this.tenorToDays(req.tenor);
    const windows = req.windows ?? RV_WINDOWS;
    const wireBarSize = WIRE_BAR_SIZE[req.barSize];
    const jobId = await this.jobs.startJob("realized_vs_iv", {
      symbol: req.symbol,
      bar_size: wireBarSize,
      tenor_days: tenorDays,
      session: req.session,
      ...(wireBarSize === "15m" ? { hf_session: hfSessionFor(req.session) } : {}),
      estimators: req.estimators,
      windows,
      ...(req.window === undefined ? {} : { window_start_ms: req.window.startMs, window_end_ms: req.window.endMs }),
      ...(req.rerunOf === undefined ? {} : { rerun_of: req.rerunOf }),
    });
    const liveIvP = this.getLiveIv30(req.symbol, tenorDays);
    const end = await this.settle(jobId, onStatus);
    if (end.kind === "cancelled") throw new Error(end.reason);
    if (end.kind === "failed") {
      const refused = readLakeStudyRefusal(await this.parkedResult(jobId));
      if (refused !== null) throw new LakeStudyRefusedError(refused);
      throw new Error(end.message);
    }
    const result = await this.jobs.fetchResult<RealizedVsIvResultRaw>(jobId);
    return { ...projectRealizedVsIvResult(result, windows), liveIv30: await liveIvP };
  }

  /** The window a run of this bar size and session reads when its request names none: the server resolves it from the
   *  calendar, so the page shows it and counts what the lake lacks over exactly that span, with no calendar of its own. */
  async getDefaultWindow(barSize: BarSize, session: Session): Promise<DefaultWindow> {
    const wire = await firstValueFrom(this.http.get<components["schemas"]["RealizedVsIvWindow"]>(
      "/api/edge/realized-vs-iv/window", { params: { bar_size: WIRE_BAR_SIZE[barSize], session } },
    ));
    return {
      barSize: wire.bar_size === "1d" ? "1D" : "15m",
      session: wire.session,
      windowStartMs: wire.window_start_ms,
      windowEndMs: wire.window_end_ms,
      sessions: wire.sessions,
    };
  }

  /** Fetch the live IV30 value for `symbol`. Tries `vix-style` first
   *  (matches the server-side recorder preference); on failure falls back
   *  to `parametric`. When both fail the answer is `unavailable` with each
   *  endpoint's reason: the marker is non-load-bearing, but a missing one is
   *  never left to look like a market with no IV. */
  async getLiveIv30(symbol: string, targetCalendarDays = 30): Promise<LiveIv30State> {
    const attempts: readonly { path: string; method: LiveIv30Marker["method"]; label: string }[] = [
      { path: "/api/edge/iv30/vix-style", method: "vix_style", label: "vix-style" },
      { path: "/api/edge/iv30/parametric", method: "parametric", label: "parametric" },
    ];
    const failures: string[] = [];
    for (const { path, method, label } of attempts) {
      try {
        const resp = await firstValueFrom(this.http.post<Iv30LiveResponse>(
          path, { symbol, target_calendar_days: targetCalendarDays },
        ));
        return { kind: "available", marker: liveResponseToMarker(resp, method) };
      } catch (error) {
        failures.push(`${label}: ${describeFailure(error)}`);
      }
    }
    return { kind: "unavailable", reason: `Neither live IV30 endpoint answered. ${failures.join("; ")}.` };
  }

  /** Resolve when the job reaches a terminal state, passing its log lines on as it goes. `onEvent` replays what this
   *  tab already saw, so a job that finished before the listener attached still settles. */
  private settle(id: string, onStatus?: (message: string) => void): Promise<JobEnd> {
    return new Promise<JobEnd>((resolve) => {
      let unsubscribe: (() => void) | null = null;
      let settled = false;
      const finish = (end: JobEnd): void => {
        if (settled) return;
        settled = true;
        unsubscribe?.();
        resolve(end);
      };
      unsubscribe = this.jobs.onEvent(id, (event) => {
        switch (event.type) {
          case "job.log":
            if (typeof event["message"] === "string") onStatus?.(event["message"]);
            break;
          case "job.completed":
            finish({ kind: "completed" });
            break;
          case "job.failed":
            finish({ kind: "failed", message: typeof event["message"] === "string" ? event["message"] : "The run failed." });
            break;
          case "job.cancelled":
            finish({ kind: "cancelled", reason: typeof event["reason"] === "string" ? event["reason"] : "The run was cancelled." });
            break;
          default:
            break;
        }
      });
      // A terminal event replayed during the call above settled before `unsubscribe` was assigned.
      if (settled) unsubscribe();
    });
  }

  /** The result a failed job parked, or null when it parked none: only a refused window leaves one, so a 404 is the
   *  ordinary answer for any other failure. Anything else is a real error and is not swallowed. */
  private async parkedResult(id: string): Promise<unknown> {
    try {
      return await this.jobs.fetchResult<unknown>(id);
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 404) return null;
      throw error;
    }
  }

  private tenorToDays(t: Tenor): number {
    return Number(t.replace("D", ""));
  }
}

function liveResponseToMarker(
  resp: Iv30LiveResponse,
  method: LiveIv30Marker["method"],
): LiveIv30Marker {
  return {
    method,
    iv30Act365: resp.iv30_act365,
    snapshotTsMs: resp.snapshot_ts_ms,
    spot: resp.spot,
    varianceContributionSynthetic: resp.iv_provenance.variance_contribution_synthetic,
    strikeCoverageScore: resp.iv_provenance.strike_coverage_score,
  };
}

/** Why a live-IV request failed, in words: the server's own detail when it sent one, else the HTTP status. */
export function describeFailure(error: unknown): string {
  if (error instanceof HttpErrorResponse) {
    if (error.status === 0) return "the data service did not answer";
    const detail: unknown = (error.error as { detail?: unknown } | null)?.detail;
    return typeof detail === "string" && detail !== "" ? `${error.status} ${detail}` : `${error.status} ${error.statusText}`;
  }
  return error instanceof Error ? error.message : "an unexpected error";
}
