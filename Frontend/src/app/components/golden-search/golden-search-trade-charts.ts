import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { dataIndexOf, seriesIndexOf, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { centsText, signedCentsText } from './golden-search-display';
import type { EntryTimes, EvidenceCandidate, MeasuredEntryRsi, MeasuredTradeCharts, TradeHistogram, TradeRecord } from './golden-search.types';

/**
 * The Compare evidence's trade charts (#2821): the selected candidate's
 * development trades over time with their running net profit (V29), their
 * net profits in bins (V28), against decision bars held (V30) and RSI at
 * entry (V31), and by entry weekday and Eastern half hour (V32). Every trade
 * counts its net profit after its entry and exit commission. Every value —
 * the running total, the bins, the bars held, the band averages and the
 * cells — is the server's; the charts only draw and label them.
 */

type Candidate = Pick<EvidenceCandidate, 'key' | 'label'>;

const DEVELOPMENT = 'Development data, used for choosing.';
const NET_OF_COMMISSION = 'Net profit is the trade’s P&L less its entry and exit commission.';
/** An axis tick or bin edge as short signed dollars (`+$1.1K`, `-$43.8`). */
const COMPACT_USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1, signDisplay: 'exceptZero' });
const ONE_DECIMAL = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });

function tradesText(count: number): string {
  return count === 1 ? '1 trade' : `${count} trades`;
}

function instant(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'local' });
}

function rsiText(value: number | null): string {
  return value === null ? 'not recorded' : ONE_DECIMAL.format(value);
}

function barsText(count: number): string {
  return count === 1 ? '1 decision bar' : `${count} decision bars`;
}

/** What one decision bar is, from the server's bar length. */
function barNote(barSpanMs: number): string {
  return barSpanMs >= 86_400_000
    ? 'A decision bar is one trading session.'
    : `A decision bar is ${barSpanMs / 60_000} minutes of a trading session; nights, weekends and holidays hold none.`;
}

const usdAxis = (theme: ChartTheme) => ({ color: theme.textSecondary, fontSize: 11, formatter: (value: number) => COMPACT_USD.format(value) });

/** A dashed line at $0 on a value axis. */
function zeroLine(theme: ChartTheme, axis: 'xAxis' | 'yAxis' = 'yAxis') {
  return { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: theme.textSecondary, type: 'dashed' as const, width: 1 }, data: [{ [axis]: 0 }] };
}

// ---------------------------------------------------------------- V29 trade timeline

/** Every trade at its exit, with the running net profit above. */
export function tradeTimelineSpec(candidate: Candidate, charts: MeasuredTradeCharts): ChartSpec {
  const trades = charts.trades;
  const last = trades.at(-1);
  return {
    label: `${candidate.label} trade timeline`,
    summary: `${candidate.label}'s ${tradesText(trades.length)} in exit order, each net of commission; their running net profit ends at ${signedCentsText(last?.running_net_profit)}.`,
    featured: candidate.key,
    option: (theme) => timelineOption(candidate, charts, theme),
    table: {
      caption: `${candidate.label}'s development trades in exit order, each net of its entry and exit commission`,
      columns: ['Exited', 'Entered', 'Entry price', 'Exit price', 'Quantity', 'P&L before fees', 'Net profit', 'Running net profit', 'Bars held', 'RSI at entry', 'Exit'],
      rows: trades.map((trade) => ({
        key: `${trade.entry_ms}|${trade.exit_ms}`,
        cells: [
          instant(trade.exit_ms),
          instant(trade.entry_ms),
          centsText(trade.entry_price),
          centsText(trade.exit_price),
          String(trade.quantity),
          signedCentsText(trade.pnl),
          signedCentsText(trade.net_profit),
          signedCentsText(trade.running_net_profit),
          String(trade.bars_held),
          rsiText(trade.entry_rsi),
          trade.exit_reason,
        ],
      })),
    },
  };
}

function timelineOption(candidate: Candidate, charts: MeasuredTradeCharts, theme: ChartTheme): ChartOption {
  const trades = charts.trades;
  const color = theme.candidates[candidate.key];
  const axisLine = { lineStyle: { color: theme.axis } };
  const splitLine = { lineStyle: { color: theme.gridLine } };
  const dateLabel = { color: theme.textSecondary, fontSize: 11, hideOverlap: true, formatter: (value: number) => formatTimestampDisplay(value, { mode: 'date-et' }) };
  return {
    grid: [
      { left: 60, right: 16, top: 12, height: '50%' },
      { left: 60, right: 16, top: '68%', bottom: 28 },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: theme.textSecondary } },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const index = dataIndexOf(params);
        const trade = trades[index ?? -1];
        return index === null || trade === undefined ? '' : tradeTooltip(trade, `Trade ${index + 1} of ${trades.length}`, charts.bar_span_ms, theme);
      },
    },
    xAxis: [
      { type: 'time', gridIndex: 0, axisLine, axisTick: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'time', gridIndex: 1, axisLine, axisLabel: dateLabel, splitLine: { show: false } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, splitNumber: 4, axisLabel: usdAxis(theme), splitLine },
      { type: 'value', gridIndex: 1, splitNumber: 2, axisLabel: usdAxis(theme), splitLine },
    ],
    series: [
      {
        id: `running:${candidate.key}`,
        name: 'Running net profit',
        type: 'line',
        xAxisIndex: 0,
        yAxisIndex: 0,
        // It changes only when a trade exits and holds until the next.
        step: 'end',
        data: trades.map((trade) => [trade.exit_ms, trade.running_net_profit]),
        showSymbol: false,
        lineStyle: { color, width: 2 },
        itemStyle: { color },
        emphasis: { focus: 'series', blurScope: 'global' },
        markLine: zeroLine(theme),
      },
      {
        id: `trades:${candidate.key}`,
        name: 'Net profit per trade',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: trades.map((trade) => ({ value: [trade.exit_ms, trade.net_profit], itemStyle: { color: trade.net_profit < 0 ? theme.loss : theme.gain } })),
        barWidth: 2,
        emphasis: { focus: 'series', blurScope: 'global' },
      },
    ],
  };
}

function tradeTooltip(trade: TradeRecord, title: string, barSpanMs: number, theme: ChartTheme): string {
  const rows: TooltipRow[] = [
    { label: 'Entered', values: [instant(trade.entry_ms)] },
    { label: 'Exited', values: [instant(trade.exit_ms)] },
    { label: 'Entry price', values: [centsText(trade.entry_price)] },
    { label: 'Exit price', values: [centsText(trade.exit_price)] },
    { label: 'Quantity', values: [String(trade.quantity)] },
    { label: 'P&L before fees', values: [signedCentsText(trade.pnl)] },
    { label: 'Net profit', values: [signedCentsText(trade.net_profit)] },
    { label: 'Running net profit', values: [signedCentsText(trade.running_net_profit)] },
    { label: 'Held', values: [barsText(trade.bars_held)] },
    { label: 'RSI at entry', values: [rsiText(trade.entry_rsi)] },
  ];
  return tooltipHtml({ title, columns: ['Value'], rows, notes: [`${trade.exit_reason}.`, barNote(barSpanMs), NET_OF_COMMISSION, DEVELOPMENT] }, theme);
}

// ---------------------------------------------------------------- V28 trade P&L histogram

function binRange(histogram: TradeHistogram, index: number): string {
  const bin = histogram.bins[index];
  return histogram.bin_width === 0 ? `Exactly ${signedCentsText(bin.low)}` : `From ${signedCentsText(bin.low)} to ${signedCentsText(bin.high)}`;
}

/** The trades' net profits in the server's bins, losses left of $0. */
export function histogramSpec(candidate: Candidate, charts: MeasuredTradeCharts): ChartSpec {
  const histogram = charts.histogram;
  const width = histogram.bin_width === 0 ? 'one bin, as every trade netted the same' : `${histogram.bins.length} bins ${centsText(histogram.bin_width)} wide`;
  return {
    label: `${candidate.label} trade net profit histogram`,
    summary: `${candidate.label}'s ${tradesText(charts.trades.length)} by net profit, in ${width}, with $0 as an edge.`,
    featured: candidate.key,
    option: (theme) => histogramOption(histogram, theme),
    table: {
      caption: `${candidate.label}'s development trades by net profit`,
      columns: ['Net profit', 'Trades', 'Wins', 'Losses'],
      rows: histogram.bins.map((bin, i) => ({ key: String(bin.low), cells: [binRange(histogram, i), String(bin.trades), String(bin.wins), String(bin.losses)] })),
    },
  };
}

function histogramOption(histogram: TradeHistogram, theme: ChartTheme): ChartOption {
  const bins = histogram.bins;
  // Every bin lies on one side of $0: its low edge says which.
  const sides = [
    { group: 'losses', name: 'Losing trades', color: theme.loss, takes: (low: number) => low < 0 },
    { group: 'wins', name: 'Trades at $0 or more', color: theme.gain, takes: (low: number) => low >= 0 },
  ];
  const width = histogram.bin_width === 0 ? 'Every trade netted the same.' : `Bins ${centsText(histogram.bin_width)} wide (the Freedman–Diaconis rule), with $0 as an edge, so no bin mixes wins and losses.`;
  return {
    grid: { left: 40, right: 12, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const index = dataIndexOf(params);
        const bin = bins[index ?? -1];
        if (index === null || bin === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Trades', values: [String(bin.trades)] },
          { label: 'Wins', values: [String(bin.wins)] },
          { label: 'Losses', values: [String(bin.losses)] },
        ];
        return tooltipHtml({ title: binRange(histogram, index), columns: ['Count'], rows, notes: [width, NET_OF_COMMISSION, DEVELOPMENT] }, theme);
      },
    },
    xAxis: {
      type: 'category',
      data: bins.map((bin) => COMPACT_USD.format(bin.low)),
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { color: theme.textSecondary, fontSize: 11, hideOverlap: true },
    },
    yAxis: { type: 'value', minInterval: 1, splitNumber: 3, axisLabel: { color: theme.textSecondary, fontSize: 11 }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: sides.map((side) => ({
      id: `${side.group}:bins`,
      name: side.name,
      type: 'bar' as const,
      data: bins.map((bin) => (side.takes(bin.low) ? bin.trades : '-')),
      barGap: '-100%',
      barCategoryGap: '8%',
      itemStyle: { color: side.color },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}

// ---------------------------------------------------------------- V30 hold time vs P&L

const EXIT_KINDS = [
  { kind: 'strategy', group: 'strategy', name: 'Exited by the strategy', symbol: 'circle' },
  { kind: 'window_end', group: 'window', name: 'Closed at the window’s end', symbol: 'diamond' },
] as const;

/** Each trade at the decision bars it was held and its net profit, by how it exited. */
export function holdTimeSpec(candidate: Candidate, charts: MeasuredTradeCharts): ChartSpec {
  return {
    label: `${candidate.label} hold time against net profit`,
    summary: `${candidate.label}'s ${tradesText(charts.trades.length)} by decision bars held and net profit; trades closed by the end of the tested window are diamonds.`,
    featured: candidate.key,
    option: (theme) => holdOption(candidate, charts, theme),
    table: {
      caption: `${candidate.label}'s development trades by decision bars held`,
      columns: ['Entered', 'Bars held', 'Net profit', 'Exit'],
      rows: charts.trades.map((trade) => ({
        key: `${trade.entry_ms}|${trade.exit_ms}`,
        cells: [instant(trade.entry_ms), String(trade.bars_held), signedCentsText(trade.net_profit), trade.exit_reason],
      })),
    },
  };
}

function holdOption(candidate: Candidate, charts: MeasuredTradeCharts, theme: ChartTheme): ChartOption {
  const byKind = EXIT_KINDS.map((exit) => charts.trades.filter((trade) => trade.exit_kind === exit.kind));
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  return {
    grid: { left: 56, right: 16, top: 12, bottom: 40 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const trade = byKind[seriesIndexOf(params) ?? -1]?.[dataIndexOf(params) ?? -1];
        return trade === undefined ? '' : tradeTooltip(trade, `Held ${barsText(trade.bars_held)}`, charts.bar_span_ms, theme);
      },
    },
    xAxis: {
      type: 'value',
      minInterval: 1,
      name: 'Decision bars held',
      nameLocation: 'middle',
      nameGap: 24,
      nameTextStyle: axisLabel,
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel,
      splitLine: { show: false },
    },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: usdAxis(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
    series: EXIT_KINDS.map((exit, i) => ({
      id: `${exit.group}:${candidate.key}`,
      name: exit.name,
      type: 'scatter' as const,
      symbol: exit.symbol,
      symbolSize: exit.kind === 'strategy' ? 7 : 10,
      data: byKind[i].map((trade) => [trade.bars_held, trade.net_profit]),
      itemStyle: { color: exit.kind === 'strategy' ? theme.candidates[candidate.key] : theme.warn, opacity: 0.75 },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      ...(i === 0 ? { markLine: zeroLine(theme) } : {}),
    })),
  };
}

// ---------------------------------------------------------------- V31 entry RSI vs P&L

function bandName(low: number, high: number): string {
  return `RSI ${ONE_DECIMAL.format(low)}–${ONE_DECIMAL.format(high)}`;
}

/** Each trade at its RSI at entry and net profit, with each band's average between the gates. */
export function entryRsiSpec(candidate: Candidate, charts: MeasuredTradeCharts, rsi: MeasuredEntryRsi): ChartSpec {
  const gates = `${ONE_DECIMAL.format(rsi.gate_low)}–${ONE_DECIMAL.format(rsi.gate_high)}`;
  const outside = rsi.unbanded > 0 ? ` ${tradesText(rsi.unbanded)} have no RSI recorded or one outside the gates, so sit in no band.` : '';
  return {
    label: `${candidate.label} RSI at entry against net profit`,
    summary: `${candidate.label}'s development trades by the RSI at entry and net profit, with the average net profit of each band between the gates ${gates}.${outside}`,
    featured: candidate.key,
    option: (theme) => rsiOption(candidate, charts, rsi, theme),
    table: {
      caption: `${candidate.label}'s average development net profit by RSI band at entry; — marks a band no trade entered in`,
      columns: ['RSI band', 'Trades', 'Average net profit'],
      rows: rsi.bands.map((band) => ({ key: String(band.low), cells: [bandName(band.low, band.high), String(band.trades), signedCentsText(band.mean_net_profit)] })),
    },
  };
}

function rsiOption(candidate: Candidate, charts: MeasuredTradeCharts, rsi: MeasuredEntryRsi, theme: ChartTheme): ChartOption {
  const entered = charts.trades.filter((trade) => trade.entry_rsi !== null);
  // Each band's average as a level segment across the band; a gap after each, and none for an empty band.
  const segments: { value: [number, number | '-']; band: number | null; symbol?: string }[] = rsi.bands.flatMap((band, i) =>
    band.mean_net_profit === null
      ? []
      : [
          { value: [band.low, band.mean_net_profit] as [number, number], band: i },
          { value: [band.high, band.mean_net_profit] as [number, number], band: null, symbol: 'none' },
          { value: [band.high, '-'] as [number, '-'], band: null, symbol: 'none' },
        ],
  );
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  const color = theme.candidates[candidate.key];
  return {
    grid: { left: 56, right: 16, top: 20, bottom: 40 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const index = dataIndexOf(params) ?? -1;
        if (seriesIndexOf(params) === 0) {
          const trade = entered[index];
          return trade === undefined ? '' : tradeTooltip(trade, `RSI ${rsiText(trade.entry_rsi)} at entry`, charts.bar_span_ms, theme);
        }
        const band = rsi.bands[segments[index]?.band ?? -1];
        if (band === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Trades', values: [String(band.trades)] },
          { label: 'Average net profit', values: [signedCentsText(band.mean_net_profit)] },
        ];
        return tooltipHtml({ title: bandName(band.low, band.high), columns: ['Value'], rows, notes: ['A band runs up to, not including, its upper edge; the last one includes the upper gate.', NET_OF_COMMISSION, DEVELOPMENT] }, theme);
      },
    },
    xAxis: {
      type: 'value',
      // Display range only: the gates with a little room either side.
      min: Math.floor(rsi.gate_low) - 2,
      max: Math.ceil(rsi.gate_high) + 2,
      name: 'RSI at entry',
      nameLocation: 'middle',
      nameGap: 24,
      nameTextStyle: axisLabel,
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel,
      splitLine: { show: false },
    },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: usdAxis(theme), splitLine: { lineStyle: { color: theme.gridLine } } },
    series: [
      {
        id: `trades:${candidate.key}`,
        name: 'Trades',
        type: 'scatter',
        data: entered.map((trade) => [trade.entry_rsi ?? 0, trade.net_profit]),
        symbolSize: 6,
        itemStyle: { color, opacity: 0.6 },
        emphasis: { focus: 'series', blurScope: 'global' },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: theme.textSecondary, type: 'dashed', width: 1 },
          label: { color: theme.textSecondary, fontSize: 11, position: 'end', formatter: (params: { value: unknown }) => `gate ${ONE_DECIMAL.format(Number(params.value))}` },
          data: [{ xAxis: rsi.gate_low }, { xAxis: rsi.gate_high }],
        },
      },
      {
        id: `bands:${candidate.key}`,
        name: 'Band average',
        type: 'line',
        data: segments.map(({ value, symbol }) => (symbol === undefined ? { value } : { value, symbol })),
        connectNulls: false,
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { color: theme.text, width: 2 },
        itemStyle: { color: theme.text },
        emphasis: { focus: 'series', blurScope: 'global' },
      },
    ],
  };
}

// ---------------------------------------------------------------- V32 entry time and weekday

function cellName(times: EntryTimes, weekday: number, halfHour: number): string {
  return `${times.weekdays[weekday]}, the half hour from ${times.half_hours[halfHour]} ET`;
}

/** Trades by entry weekday and Eastern half hour: each cell's count, coloured by its average net profit unless it holds too few. */
export function entryTimeSpec(candidate: Candidate, charts: MeasuredTradeCharts): ChartSpec {
  const times = charts.entry_times;
  return {
    label: `${candidate.label} entry time and weekday`,
    summary: `${candidate.label}'s development trades by entry weekday and Eastern half hour, each cell's trade count coloured by its average net profit; cells under ${times.min_trades} trades are grey, too few to judge.`,
    featured: candidate.key,
    option: (theme) => entryTimeOption(times, theme),
    table: {
      caption: `${candidate.label}'s development trades by entry weekday and Eastern half hour`,
      columns: ['Entered (ET)', 'Trades', 'Average net profit', 'Total net profit', 'Enough to judge'],
      rows: times.cells.map((cell) => ({
        key: `${cell.weekday}|${cell.half_hour}`,
        cells: [
          `${times.weekdays[cell.weekday]} ${times.half_hours[cell.half_hour]}`,
          String(cell.trades),
          signedCentsText(cell.mean_net_profit),
          signedCentsText(cell.total_net_profit),
          cell.too_few ? 'too few trades' : 'yes',
        ],
      })),
    },
  };
}

function entryTimeOption(times: EntryTimes, theme: ChartTheme): ChartOption {
  const groups = [
    { group: 'enough', name: 'Enough trades', cells: times.cells.filter((cell) => !cell.too_few) },
    { group: 'few', name: 'Too few trades', cells: times.cells.filter((cell) => cell.too_few) },
  ];
  // Display scale only, as on the month calendar.
  const reach = Math.max(1, ...groups[0].cells.map((cell) => Math.abs(cell.mean_net_profit)));
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  return {
    grid: { left: 40, right: 8, top: 4, bottom: 24 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const cell = groups[seriesIndexOf(params) ?? -1]?.cells[dataIndexOf(params) ?? -1];
        if (cell === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Trades', values: [String(cell.trades)] },
          { label: 'Average net profit', values: [signedCentsText(cell.mean_net_profit)] },
          { label: 'Total net profit', values: [signedCentsText(cell.total_net_profit)] },
        ];
        const few = cell.too_few ? [`Fewer than ${times.min_trades} trades: too few for the average to mean much.`] : [];
        return tooltipHtml({ title: cellName(times, cell.weekday, cell.half_hour), columns: ['Value'], rows, notes: [...few, NET_OF_COMMISSION, DEVELOPMENT] }, theme);
      },
    },
    xAxis: { type: 'category', data: [...times.half_hours], axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisLabel, hideOverlap: true } },
    yAxis: { type: 'category', data: [...times.weekdays], inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel },
    visualMap: [
      { type: 'continuous', show: false, seriesIndex: 0, dimension: 2, min: -reach, max: reach, inRange: { color: [theme.loss, theme.neutral, theme.gain] } },
      { type: 'continuous', show: false, seriesIndex: 1, dimension: 2, min: -reach, max: reach, inRange: { color: [theme.tooFew, theme.tooFew] } },
    ],
    series: groups.map((group) => ({
      id: `${group.group}:cells`,
      name: group.name,
      type: 'heatmap' as const,
      data: group.cells.map((cell) => [cell.half_hour, cell.weekday, cell.mean_net_profit]),
      // The trade count in each cell; the average is in its tooltip and the table.
      label: { show: true, color: group.group === 'few' ? theme.textSecondary : theme.text, fontSize: 10, formatter: (params: { dataIndex: number }) => String(group.cells[params.dataIndex]?.trades ?? '') },
      // A dashed outline sets a too-few cell apart from a coloured one whose average is near $0.
      itemStyle: group.group === 'few' ? { borderColor: theme.textSecondary, borderWidth: 1, borderType: 'dashed' as const } : { borderColor: theme.gridLine, borderWidth: 1 },
      emphasis: { itemStyle: { borderColor: theme.text, borderWidth: 2 } },
    })),
  };
}
