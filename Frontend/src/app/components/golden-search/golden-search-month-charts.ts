import { formatTimestampDisplay } from '../../shared/timestamp';
import type { ChartSpec, ChartTable } from './charts/golden-search-chart-spec';
import { dataIndexOf, MONTH_NAMES, monthAxisText, monthText, tooltipFrame, tooltipHtml, type ChartTheme } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { compactUsdText, DEVELOPMENT_NOTE, signedPercentText, signedUsdText } from './golden-search-display';
import type { CandidateRef, MonthlyResult } from './golden-search.types';

/**
 * The Compare evidence's month charts (#2821): the selected candidate's
 * development net profit for each ET calendar month, as a year-by-month
 * calendar (V24) and as bars in time order (V25). Every value, the month's
 * year and month included, is the server's; a month the run did not cover
 * has no cell, never a $0 one.
 */

/** A calendar cell's amount to two significant figures (`+$890`, `-$1.2K`), so it fits the cell; the tooltip and table give it in full. */
const CELL_USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: 'compact', maximumSignificantDigits: 2, signDisplay: 'exceptZero' });


function monthFrom(month: MonthlyResult): string {
  return formatTimestampDisplay(month.month_start_ms, { mode: 'date-et' });
}

/** The months as a table: both charts show the same values, each under its own caption. */
function monthTable(caption: string, months: readonly MonthlyResult[]): ChartTable {
  return {
    caption: `${caption}; — marks a return not recorded`,
    columns: ['Month from (ET)', 'Net profit', 'Net return', 'Trades closed'],
    rows: months.map((month) => ({
      key: String(month.month_start_ms),
      cells: [monthFrom(month), signedUsdText(month.net_profit), signedPercentText(month.return_fraction), String(month.trades)],
    })),
  };
}

function monthTooltip(month: MonthlyResult, theme: ChartTheme): string {
  return tooltipHtml(
    {
      title: monthText(month),
      columns: ['Value'],
      rows: [
        { label: 'Net profit', values: [signedUsdText(month.net_profit)] },
        { label: 'Net return', values: [signedPercentText(month.return_fraction)] },
        { label: 'Trades closed', values: [String(month.trades)] },
      ],
      notes: [`The month from ${monthFrom(month)} (ET). Net profit is the change in equity, so a position open at a month end splits its profit; trades closed count in the month they exit.`, DEVELOPMENT_NOTE],
    },
    theme,
  );
}

function spanText(months: readonly MonthlyResult[]): string {
  const first = months[0];
  const last = months.at(-1);
  return first === undefined || last === undefined ? 'no months' : `${monthText(first)} to ${monthText(last)}`;
}

// ---------------------------------------------------------------- V24 month calendar

/** The months as a calendar: a row per year, a column per month, coloured by net profit. */
export function monthCalendarSpec(candidate: CandidateRef, months: readonly MonthlyResult[]): ChartSpec {
  return {
    label: `${candidate.label} month calendar`,
    summary: `${candidate.label}'s development net profit for each of its ${months.length} months, ${spanText(months)}, a row per year.`,
    featured: candidate.key,
    option: (theme) => calendarOption(candidate, months, theme),
    table: monthTable(`${candidate.label}'s development months in the calendar`, months),
  };
}

function calendarOption(candidate: CandidateRef, months: readonly MonthlyResult[], theme: ChartTheme): ChartOption {
  const years = [...new Set(months.map((month) => month.year))].sort((a, b) => a - b);
  // Display scale only: the colour runs from the deepest loss or gain to its mirror, with $0 in the middle.
  const reach = Math.max(1, ...months.map((month) => Math.abs(month.net_profit)));
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  return {
    grid: { left: 44, right: 8, top: 4, bottom: 24 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const month = months[dataIndexOf(params) ?? -1];
        return month === undefined ? '' : monthTooltip(month, theme);
      },
    },
    xAxis: { type: 'category', data: [...MONTH_NAMES], axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel },
    yAxis: { type: 'category', data: years.map(String), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel },
    visualMap: { type: 'continuous', show: false, seriesIndex: 0, dimension: 2, min: -reach, max: reach, inRange: { color: [theme.loss, theme.neutral, theme.gain] } },
    series: [
      {
        id: `months:${candidate.key}`,
        name: candidate.label,
        type: 'heatmap',
        data: months.map((month) => [month.month - 1, years.indexOf(month.year), month.net_profit]),
        // The signed amount in each cell, so colour is never the only cue.
        label: {
          show: true,
          color: theme.text,
          fontSize: 10,
          formatter: (params: { dataIndex: number }) => {
            const month = months[params.dataIndex];
            return month === undefined ? '' : CELL_USD.format(month.net_profit);
          },
        },
        itemStyle: { borderColor: theme.gridLine, borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: theme.text, borderWidth: 2 } },
      },
    ],
  };
}

// ---------------------------------------------------------------- V25 monthly net profit

/** The months as bars in time order: above $0 a gain, below a loss. */
export function monthlyNetSpec(candidate: CandidateRef, months: readonly MonthlyResult[]): ChartSpec {
  return {
    label: `${candidate.label} monthly net profit`,
    summary: `${candidate.label}'s development net profit in each of its ${months.length} months, ${spanText(months)}.`,
    featured: candidate.key,
    option: (theme) => barsOption(candidate, months, theme),
    table: monthTable(`${candidate.label}'s development net profit by month, in time order`, months),
  };
}

function barsOption(candidate: CandidateRef, months: readonly MonthlyResult[], theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  return {
    grid: { left: 56, right: 12, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const month = months[dataIndexOf(params) ?? -1];
        return month === undefined ? '' : monthTooltip(month, theme);
      },
    },
    xAxis: {
      type: 'category',
      data: months.map((month) => monthAxisText(month)),
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { ...axisLabel, hideOverlap: true },
    },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: { ...axisLabel, formatter: (value: number) => compactUsdText(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: [
      {
        id: `bars:${candidate.key}`,
        name: candidate.label,
        type: 'bar',
        data: months.map((month) => ({ value: month.net_profit, itemStyle: { color: month.net_profit < 0 ? theme.loss : theme.gain } })),
        barMaxWidth: 18,
        emphasis: { focus: 'series', blurScope: 'global' },
      },
    ],
  };
}
