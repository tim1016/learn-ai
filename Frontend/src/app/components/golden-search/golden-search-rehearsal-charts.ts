import type { ChartSpec } from './charts/golden-search-chart-spec';
import { dataIndexOf, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import { shareText } from './golden-search-chance-charts';
import type { CandidateRef, MeasuredChanceRehearsal } from './golden-search.types';

/**
 * The final-test rehearsal chart (#3012): each rung's share of resamples
 * meeting every modeled rule, beside each rule's own share, against the
 * owner's 80% reading heuristic. Every number is the server's — the rung
 * counts, shares, floors, the guidance sentence and the labels; the browser
 * only formats and draws. A rule the plan does not require has no series,
 * a longer rung is drawn at its actual value even when support falls, and
 * the x axis counts ordinal scheduled sessions, never invented dates.
 */

const RULE_LABELS: Readonly<Record<string, string>> = {
  positive_net: 'Positive net',
  drawdown: 'Drawdown within ceiling (at session closes)',
  exits: 'Trade floor',
  objective: 'At least the current settings',
};

const RULE_NOTES: Readonly<Record<string, string>> = {
  positive_net: 'Finite compounded net return above zero, when the plan requires a positive net.',
  drawdown: 'Finite worst fall at session closes within the frozen ceiling. The real final-test gate reads the engine’s per-bar drawdown, which can be deeper.',
  exits: 'Resampled completed exits at or above the rung’s trade floor — resampled session outcomes, not newly executed trades.',
  objective: 'Both objectives finite with the fit’s at least the current settings’.',
};

/** The per-rule series, joint first and most prominent; only rules the plan models. */
export function rehearsalSeries(rehearsal: MeasuredChanceRehearsal): { readonly key: 'joint' | string; readonly label: string; readonly note: string }[] {
  return [
    { key: 'joint', label: 'All modeled rules together', note: 'The share of resamples meeting every modeled rule — the intersection, never a product of the single-rule shares.' },
    ...rehearsal.active_rules.map((rule) => ({ key: rule, label: RULE_LABELS[rule] ?? rule, note: RULE_NOTES[rule] ?? rule })),
  ];
}

export function rehearsalSpec(candidate: CandidateRef, rehearsal: MeasuredChanceRehearsal, featured: string | null): ChartSpec {
  const series = rehearsalSeries(rehearsal);
  return {
    label: `${candidate.label} final-test rehearsal`,
    summary: `Share of ${rehearsal.basis.replications.toLocaleString('en-US')} resamples meeting every modeled rule, at 1× to 4× the final test's scheduled sessions.`,
    featured,
    option: (theme) => rehearsalOption(rehearsal, series, theme),
    table: {
      caption: `${candidate.label} final-test rehearsal: share of resamples meeting each modeled rule (— marks a rule this plan does not require)`,
      columns: ['Rung', 'Sessions', 'Trade floor', 'All rules together', ...series.slice(1).map((item) => item.label)],
      rows: rehearsal.rungs.map((rung) => ({
        key: `${rung.k}x`,
        cells: [
          `${rung.k}×`,
          String(rung.sessions),
          String(rung.trade_floor),
          shareText(rung.shares['joint'] ?? 0),
          ...rehearsal.active_rules.map((rule) => shareText(rung.shares[rule] ?? 0)),
        ],
      })),
    },
  };
}

function rehearsalOption(rehearsal: MeasuredChanceRehearsal, series: { key: string; label: string }[], theme: ChartTheme): ChartOption {
  const axisLabel = { color: theme.textSecondary, fontSize: 11 };
  return {
    grid: { left: 56, right: 24, top: 42, bottom: 46 },
    legend: {
      show: true,
      top: 0,
      left: 56,
      itemWidth: 14,
      textStyle: { color: theme.textSecondary, fontSize: 11 },
      data: series.map((item) => item.label),
    },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'axis',
      formatter: (params: unknown) => {
        const rung = rehearsal.rungs[dataIndexOf(params) ?? -1];
        if (rung === undefined) return '';
        const rows: TooltipRow[] = series.map((item, index) => ({
          label: item.label,
          swatch: { color: seriesColor(index, theme), dashed: false },
          values: [shareText(rung.shares[item.key] ?? 0)],
        }));
        return tooltipHtml(
          {
            title: `${rung.k}× rung · ${rung.sessions} scheduled sessions · trade floor ${rung.trade_floor}`,
            columns: ['Share of resamples'],
            rows,
            notes: ['A conditional rehearsal of resampled development sessions — not a power calculation, and not a guaranteed minimum test length.'],
          },
          theme,
        );
      },
    },
    xAxis: {
      type: 'category',
      name: 'scheduled sessions',
      nameLocation: 'middle',
      nameGap: 28,
      nameTextStyle: { color: theme.textSecondary, fontSize: 11 },
      data: rehearsal.rungs.map((rung) => `${rung.sessions}`),
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel,
      boundaryGap: false,
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      interval: 20,
      axisLabel: { ...axisLabel, formatter: (value: number) => `${value}%` },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    series: series.map((item, index) => ({
      id: `rehearsal:${item.key}`,
      name: item.label,
      type: 'line' as const,
      data: rehearsal.rungs.map((rung) => Math.round((rung.shares[item.key] ?? 0) * 1000) / 10),
      showSymbol: true,
      symbolSize: item.key === 'joint' ? 8 : 5,
      lineStyle: { color: seriesColor(index, theme), width: item.key === 'joint' ? 3 : 1.5, type: item.key === 'joint' ? 'solid' : 'solid' },
      itemStyle: { color: seriesColor(index, theme) },
      emphasis: { focus: 'series' as const },
      markLine:
        item.key === 'joint'
          ? {
              silent: true,
              symbol: 'none',
              lineStyle: { color: theme.warn, type: 'dashed', width: 1.5 },
              label: { show: true, position: 'insideEndTop', color: theme.warn, fontSize: 10, formatter: () => `${Math.round((rehearsal.basis.thresholds['rehearsal_target'] ?? 0.8) * 100)}% target (owner heuristic)` },
              data: [{ yAxis: (rehearsal.basis.thresholds['rehearsal_target'] ?? 0.8) * 100 }],
            }
          : undefined,
    })),
  };
}

function seriesColor(index: number, theme: ChartTheme): string {
  return index === 0 ? theme.candidates.all_period : theme.series[(index - 1) % theme.series.length];
}
