/**
 * Pure geometry/color computation for the Recency Chart swimlane. No
 * Angular, no DOM — the component consumes SwimlaneLayout and only maps
 * it to SVG attribute bindings, so every branch here is independently
 * unit-testable (design spec §5.1, D6-D10).
 *
 * pnlColor / sharpeToOpacity stops are a first-cut CMYK-family diverging
 * scale (cyan loss / gray breakeven / magenta gain) chosen for
 * colorblind-safety over red/green; final perceptual tuning against the
 * dataviz skill's palette validator is a fast-follow polish pass, not a
 * blocker for this tracer-bullet render.
 */

export interface RecencySwimlaneTrade {
  symbol: string;
  strategyKey: string;
  paramsHash: string;
  /** Raw JSON of the combo's parameter assignment; optional so existing
   * fixtures/tests that predate the focus panel (design spec §4, D4)
   * don't need updating. Missing/empty means "unknown" to the panel. */
  paramsJson?: string;
  fingerprint: string;
  entryMs: number;
  exitMs: number;
  pnlPts: number;
  pnlPct: number;
  quantity: number;
  pnl: number;
  holdingSessions: number;
  sharpe: number | null;
  studyId: number | null;
  recencyRunId: number;
}

export interface SwimlaneBarLayout {
  trade: RecencySwimlaneTrade;
  subRow: number;
  fillColor: string;
  opacity: number;
}

export interface SwimlaneLaneLayout {
  symbol: string;
  subRowCount: number;
  mostRecentEntryMs: number;
  bars: SwimlaneBarLayout[];
}

export interface SwimlaneLayout {
  lanes: SwimlaneLaneLayout[];
  bars: SwimlaneBarLayout[];
  pnlDomain: number;
}

const PNL_NEG_RGB: [number, number, number] = [0x0e, 0x7d, 0x99]; // cyan (loss)
const PNL_MID_RGB: [number, number, number] = [0xe4, 0xe1, 0xe6]; // gray (breakeven)
const PNL_POS_RGB: [number, number, number] = [0xb2, 0x1e, 0x78]; // magenta (gain)

const OPACITY_FLOOR = 0.35;
const SHARPE_SATURATION_POINT = 2.0; // sharpe at/above this maps to full opacity

function lerpChannel(a: number, b: number, t: number): number {
  return Math.round(a + (b - a) * t);
}

function rgbToHex([r, g, b]: [number, number, number]): string {
  const clamp = (v: number) => Math.max(0, Math.min(255, v));
  return `#${[r, g, b].map((v) => clamp(v).toString(16).padStart(2, "0")).join("")}`;
}

/** Diverging cyan/gray/magenta interpolation, clamped to +/-domain. */
export function pnlColor(pnl: number, domain: number): string {
  if (domain <= 0) return rgbToHex(PNL_MID_RGB);
  const t = Math.max(-1, Math.min(1, pnl / domain));
  const endpoint = t >= 0 ? PNL_POS_RGB : PNL_NEG_RGB;
  const magnitude = Math.abs(t);
  const rgb: [number, number, number] = [
    lerpChannel(PNL_MID_RGB[0], endpoint[0], magnitude),
    lerpChannel(PNL_MID_RGB[1], endpoint[1], magnitude),
    lerpChannel(PNL_MID_RGB[2], endpoint[2], magnitude),
  ];
  return rgbToHex(rgb);
}

/** Sharpe -> bar opacity, floored so a low/undefined-quality combo never disappears. */
export function sharpeToOpacity(sharpe: number | null): number {
  if (sharpe === null || sharpe <= 0) return OPACITY_FLOOR;
  const t = Math.min(1, sharpe / SHARPE_SATURATION_POINT);
  return OPACITY_FLOOR + (1 - OPACITY_FLOOR) * t;
}

interface PackedTrade {
  trade: RecencySwimlaneTrade;
  subRow: number;
}

/**
 * Greedy interval packing: highest-pnl trade first, into the topmost
 * sub-row with no time overlap. Bars are never nudged along the time
 * axis — only which sub-row a bar occupies changes (design spec D7).
 */
export function packLane(trades: RecencySwimlaneTrade[]): PackedTrade[] {
  const sorted = [...trades].sort((a, b) => b.pnl - a.pnl || a.entryMs - b.entryMs);
  const rows: RecencySwimlaneTrade[][] = [];

  for (const t of sorted) {
    let placedRow = rows.findIndex((row) => row.every((o) => t.entryMs >= o.exitMs || t.exitMs <= o.entryMs));
    if (placedRow === -1) {
      placedRow = rows.length;
      rows.push([]);
    }
    rows[placedRow].push(t);
  }

  const result: PackedTrade[] = [];
  rows.forEach((row, subRow) => {
    for (const t of row) result.push({ trade: t, subRow });
  });
  return result;
}

function computePnlDomain(trades: RecencySwimlaneTrade[]): number {
  return trades.reduce((max, t) => Math.max(max, Math.abs(t.pnl)), 0);
}

export function computeSwimlaneLayout(trades: RecencySwimlaneTrade[]): SwimlaneLayout {
  const domain = computePnlDomain(trades);

  const bySymbol = new Map<string, RecencySwimlaneTrade[]>();
  for (const t of trades) {
    const group = bySymbol.get(t.symbol);
    if (group) group.push(t);
    else bySymbol.set(t.symbol, [t]);
  }

  const lanes: SwimlaneLaneLayout[] = Array.from(bySymbol.entries())
    .map(([symbol, symbolTrades]): SwimlaneLaneLayout => {
      const packed = packLane(symbolTrades);
      const bars: SwimlaneBarLayout[] = packed.map((p) => ({
        trade: p.trade,
        subRow: p.subRow,
        fillColor: pnlColor(p.trade.pnl, domain),
        opacity: sharpeToOpacity(p.trade.sharpe),
      }));
      const subRowCount = bars.reduce((max, b) => Math.max(max, b.subRow + 1), 0);
      const mostRecentEntryMs = symbolTrades.reduce((max, t) => Math.max(max, t.entryMs), 0);
      return { symbol, subRowCount, mostRecentEntryMs, bars };
    })
    .sort((a, b) => b.mostRecentEntryMs - a.mostRecentEntryMs);

  return {
    lanes,
    bars: lanes.flatMap((l) => l.bars),
    pnlDomain: domain,
  };
}
