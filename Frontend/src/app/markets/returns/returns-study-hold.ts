import { Injectable } from '@angular/core';

import type { ReturnDistributionStudy, StudyQuery } from './returns-distribution.service';

/** How many answers an instrument's place keeps: enough to flip the bin width back and forth. */
const HELD_STUDIES = 6;

interface Held {
  readonly symbol: string;
  readonly fromMsUtc: number;
  readonly toMsUtc: number;
  readonly study: ReturnDistributionStudy;
}

/**
 * The studies an instrument's place already holds, by the whole request that
 * produced them (symbol, window, bin geometry).
 *
 * Provided by the place, so the Returns tab can come back to a request it
 * already answered without asking Python again (PRD #3490 R05): a tab change
 * remounts the study, and a remount alone is no reason to run a 12-second read
 * twice. Only a study the person ran is held, so holding one is also what lets
 * the tab show it again without being asked. A different request replaces nothing it does not push out, and a
 * study whose lake capture did not finish is never held, so reopening the tab
 * asks again rather than showing a failed capture as the settled answer.
 * `invalidate` drops an instrument's studies when its lake changed under them
 * (a Fill added sessions): a repaired dataset is new evidence.
 */
@Injectable()
export class ReturnsStudyHold {
  private readonly held = new Map<string, Held>();

  get(query: StudyQuery): ReturnDistributionStudy | undefined {
    const key = keyOf(query);
    const entry = this.held.get(key);
    if (entry === undefined) return undefined;
    // Most recently used goes last, so the oldest is the one pushed out.
    this.held.delete(key);
    this.held.set(key, entry);
    return entry.study;
  }

  put(query: StudyQuery, study: ReturnDistributionStudy): void {
    if (study.capture !== null && study.capture.status !== 'complete' && study.capture.status !== 'not_attempted' && study.capture.status !== 'skipped') {
      return;
    }
    const key = keyOf(query);
    this.held.delete(key);
    this.held.set(key, { symbol: query.symbol, fromMsUtc: query.fromMsUtc, toMsUtc: query.toMsUtc, study });
    for (const oldest of this.held.keys()) {
      if (this.held.size <= HELD_STUDIES) break;
      this.held.delete(oldest);
    }
  }

  /**
   * Whether the place already holds a study of this instrument and window, at any bin width. A held study was run
   * by the person once, so showing it again, or re-binning it, asks nothing the lake has not already been asked.
   */
  holdsScope(symbol: string, fromMsUtc: number, toMsUtc: number): boolean {
    for (const entry of this.held.values()) {
      if (entry.symbol === symbol && entry.fromMsUtc === fromMsUtc && entry.toMsUtc === toMsUtc) return true;
    }
    return false;
  }

  /** Forgets every study of one instrument. */
  invalidate(symbol: string): void {
    for (const [key, entry] of this.held) {
      if (entry.symbol === symbol) this.held.delete(key);
    }
  }
}

function keyOf(query: StudyQuery): string {
  return [query.symbol, query.fromMsUtc, query.toMsUtc, query.binWidthPct, query.spanPct].join('|');
}
