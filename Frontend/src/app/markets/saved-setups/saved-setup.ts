import { barTimeframeByValue, barTimeframeFor, parseChartTimeframe } from '../../components/data-lab/data-lab-scope';
import { DATA_LAB_WORKSPACE_SCHEMA_VERSION } from '../../components/data-lab/data-lab-workspace-store';
import type { DataLabCompanionSettings } from '../../components/data-lab/data-lab-workspace-store';
import type { DataLabSession, DataLabSessionChartSnapshot } from '../../services/data-lab-session.service';
import {
  DEFAULT_ADJUSTMENT,
  DEFAULT_FORWARD_FILL,
  windowMsUtc,
  type InstrumentAdjustment,
  type InstrumentSession,
  type InstrumentWindow,
} from '../instrument-context';

/*
 * Opening a saved setup under its instrument (PRD #3490 R10, R33).
 *
 * A saved setup is a record Python holds (`/api/data-lab/sessions`): the
 * instrument, a window, the session, the bar policy and the indicator
 * recipe, and the chart it was saved with. Opening it is two things: the
 * workspace the place holds takes the setup's state (through the store's own
 * bounded validator, which reports what it could not take), and the address
 * moves to the setup's instrument and window. The record is read, never
 * rewritten: whatever this cannot show stays in it.
 */

/** What opening a setup does, before anything is applied. Pure. */
export interface SetupOpening {
  /** The instrument the setup is for, upper-cased; empty when the record names none. */
  readonly ticker: string;
  /** The v2 workspace envelope for `DataLabWorkspaceStore.restore`, which validates it. */
  readonly envelope: Record<string, unknown>;
  /**
   * The context the address takes: the setup's window (null when its dates are not dates), bar timeframe, session,
   * price adjustment and forward-fill choice. The address is where the place reads all of them, so a setup saved with
   * raw prices or without forward fill has to say so there, or the chart would read the defaults after a reload.
   */
  readonly address: {
    readonly window: InstrumentWindow | null;
    readonly timeframe: string;
    readonly session: InstrumentSession;
    readonly adjustment: InstrumentAdjustment;
    readonly forwardFill: boolean;
  };
  /** The saved chart, when it can stand in for a request without one being made. */
  readonly chart: DataLabSessionChartSnapshot | null;
  /** What this could not take from the record, for the person to read. The record keeps it. */
  readonly notes: readonly string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Whether the chart saved with a setup has every part the chart draws from. A
 * snapshot saved before a field existed, or hand-edited, is left in the
 * record and not drawn.
 */
export function isShowableChart(value: unknown): value is DataLabSessionChartSnapshot {
  return (
    isRecord(value) &&
    typeof value['timeframe'] === 'string' &&
    Array.isArray(value['bars']) &&
    Array.isArray(value['indicators']) &&
    isRecord(value['quality']) &&
    Array.isArray(value['allowedTimeframes']) &&
    isRecord(value['estimatedBarsPerTimeframe']) &&
    typeof value['recommendedTimeframe'] === 'string' &&
    Array.isArray(value['visibleIndicatorIds'])
  );
}

/**
 * What opening `session` does.
 *
 * `shownTimeframe` is the bar timeframe the place shows now: a record carries
 * the timeframe only inside its saved chart, so a setup saved without one
 * opens at the one on screen. A saved chart at a timeframe this place does not
 * offer is not drawn, and a chart whose recipe could not be taken whole is not
 * drawn either, because it would answer a request the page does not make.
 */
export function planSetupOpening(
  session: DataLabSession,
  shownTimeframe: string,
  companions: DataLabCompanionSettings,
): SetupOpening {
  const notes: string[] = [];
  const config = session.config;
  const ticker = typeof config.ticker === 'string' ? config.ticker.trim().toUpperCase() : '';

  const dates: InstrumentWindow = { from: config.fromDate, to: config.toDate };
  const ms = windowMsUtc(dates);
  if (ms === null) {
    notes.push(`The setup's dates ${String(config.fromDate)} to ${String(config.toDate)} are not both dates, so it opens on the default window.`);
  }

  const saved = session.chartSnapshot;
  let chart: DataLabSessionChartSnapshot | null = null;
  let chosen = barTimeframeByValue(shownTimeframe);
  if (saved !== null && saved !== undefined) {
    if (!isShowableChart(saved)) {
      notes.push('The chart saved with this setup is not in a shape this page can show, so the chart loads fresh. The saved chart stays in the setup.');
    } else {
      const parsed = parseChartTimeframe(saved.timeframe);
      const offered = parsed === null ? undefined : barTimeframeFor(parsed.timespan, parsed.multiplier);
      if (offered === undefined) {
        notes.push(`The chart saved with this setup used ${saved.timeframe} bars, which the Chart tab does not offer, so the chart loads fresh at ${chosen?.label ?? shownTimeframe}. The saved chart stays in the setup.`);
      } else {
        chosen = offered;
        chart = saved;
      }
    }
  }
  const timeframe = chosen ?? barTimeframeByValue('1d');
  if (timeframe === undefined) throw new Error('The place offers no 1 day bar timeframe.');

  const sessionKind: InstrumentSession = config.session === 'extended' ? 'extended' : 'regular';
  // Only a boolean the record holds is taken, as the store's validator takes it; a record without one reads the
  // default. The envelope below still hands the validator the record's own values, so it names what it drops.
  const adjustment: InstrumentAdjustment = config.adjusted === false ? 'raw' : DEFAULT_ADJUSTMENT;
  const forwardFill = typeof config.forwardFill === 'boolean' ? config.forwardFill : DEFAULT_FORWARD_FILL;
  const entries: readonly unknown[] = Array.isArray(config.entries) ? config.entries : [];
  const envelope: Record<string, unknown> = {
    schemaVersion: DATA_LAB_WORKSPACE_SCHEMA_VERSION,
    ticker,
    ...(ms === null ? {} : { windowMsUtc: ms }),
    scope: {
      timeframe: timeframe.timespan === 'day' && timeframe.multiplier === 1 ? 'day' : timeframe.value,
      timespan: timeframe.timespan,
      multiplier: timeframe.multiplier,
      session: sessionKind,
      forwardFill: config.forwardFill,
      adjusted: config.adjusted,
    },
    // The record's `name` is the indicator; the store's validator speaks `canonicalKey`.
    // An entry that is not an object goes through as it is, so the validator says so.
    indicators: entries.map((entry) => (isRecord(entry) ? { canonicalKey: entry['name'], params: entry['params'] } : entry)),
    colorTokenOverrides: {},
    companions,
    savedSession: { id: session.id, schemaVersion: DATA_LAB_WORKSPACE_SCHEMA_VERSION },
  };

  return {
    ticker,
    envelope,
    address: { window: ms === null ? null : dates, timeframe: timeframe.value, session: sessionKind, adjustment, forwardFill },
    chart,
    notes,
  };
}
