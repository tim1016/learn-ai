import type { ChartSpec } from './charts/golden-search-chart-spec';
import { axisText, dataIndexOf, NOT_RECORDED, seriesIndexOf, tooltipFrame, tooltipHtml, type ChartTheme, type TooltipRow } from './charts/golden-search-chart-theme';
import type { ChartOption } from './charts/golden-search-echarts';
import type { PairCellView, PairMapView } from './golden-search-compare';
import { cellDetail } from './golden-search-compare';
import {
  compactUsdText,
  DEVELOPMENT_NOTE,
  entryText,
  fullPointEntries,
  knobsByName,
  percentText,
  ratioText,
  ruleWords,
  signedPercentText,
  signedUsdText,
} from './golden-search-display';
import type { KnobProfile, Point, ProcedureCharts, ScoredPoint, StrategyCapability, TriedPoint } from './golden-search.types';

/**
 * The Search step's charts (#2821): the path Zoom took with the best result
 * so far (V7), where each searched knob started and ended in its range (V8),
 * each knob's profile with the others held (V9), every scored point against
 * the rules (V10), and a two-knob landscape (V11). Every number is the
 * server's, from the procedure's own record and its stored evaluations. All of
 * it is in-sample: these runs chose the settings.
 */

type Objective = ProcedureCharts['policy']['objective'];

const OBJECTIVE_NAMES: Readonly<Record<Objective, string>> = { sharpe_ratio: 'Sharpe', total_return_pct: 'Net return', net_profit: 'Net profit' };

function objectiveText(objective: Objective, value: number | null): string {
  if (value === null) return NOT_RECORDED;
  return objective === 'sharpe_ratio' ? ratioText(value) : objective === 'total_return_pct' ? signedPercentText(value) : signedUsdText(value);
}

function axisValue(objective: Objective, value: number): string {
  return objective === 'sharpe_ratio' ? ratioText(value) : objective === 'total_return_pct' ? signedPercentText(value) : compactUsdText(value);
}

/** Every knob's value, a default the canonical point leaves out included. */
function pointText(point: Point, capability: StrategyCapability | null): string {
  return fullPointEntries(point, capability).map(entryText).join(' · ');
}

/** The scored numbers every tooltip lists, under the window's rules. */
function scoredRows(scored: Pick<ScoredPoint, 'sharpe_ratio' | 'total_return_pct' | 'net_profit' | 'total_trades' | 'max_drawdown_pct' | 'ineligibility'>): TooltipRow[] {
  return [
    { label: 'Sharpe', values: [scored.sharpe_ratio === null ? NOT_RECORDED : ratioText(scored.sharpe_ratio)] },
    { label: 'Net return', values: [scored.total_return_pct === null ? NOT_RECORDED : signedPercentText(scored.total_return_pct)] },
    { label: 'Net profit', values: [scored.net_profit === null ? NOT_RECORDED : signedUsdText(scored.net_profit)] },
    { label: 'Trades', values: [scored.total_trades === null ? NOT_RECORDED : String(scored.total_trades)] },
    { label: 'Worst fall', values: [scored.max_drawdown_pct === null ? NOT_RECORDED : percentText(scored.max_drawdown_pct)] },
    { label: 'Rules', values: [ruleWords(scored.ineligibility)] },
  ];
}

/** The table's columns for the same numbers, so the table holds what the hover shows. */
const SCORED_COLUMNS = ['Sharpe', 'Net return', 'Net profit', 'Trades', 'Worst fall', 'Rules'] as const;

function scoredCells(scored: Parameters<typeof scoredRows>[0]): string[] {
  return scoredRows(scored).map((row) => (row.values[0] === NOT_RECORDED ? '—' : row.values[0]));
}

function rulesNote(procedure: ProcedureCharts): string {
  const policy = procedure.policy;
  const floor = policy.min_trades === null ? 'no trade floor' : `at least ${policy.min_trades} trades`;
  const net = policy.require_positive_net ? ', a profit' : '';
  return `The rules on this window: ${floor}, a worst fall within ${percentText(policy.max_drawdown_ceiling)}${net}.`;
}


// ---------------------------------------------------------------- V7 search path

function knobLabel(name: string, capability: StrategyCapability | null): string {
  return knobsByName(capability).get(name)?.label ?? name;
}

function stepText(point: TriedPoint, capability: StrategyCapability | null): string {
  return point.knob === null ? 'The starting point' : `Pass ${(point.pass_index ?? 0) + 1}, ${knobLabel(point.knob, capability)} round ${(point.round_index ?? 0) + 1}`;
}

/** Every point Zoom tried, in order, with the best eligible result so far. */
export function convergenceSpec(procedure: ProcedureCharts, tried: readonly TriedPoint[], capability: StrategyCapability | null, label: string): ChartSpec {
  const objective = procedure.policy.objective;
  const name = OBJECTIVE_NAMES[objective];
  const last = tried.at(-1);
  return {
    label: `${label} search path`,
    summary: `${label}: ${tried.length} points tried in order, each by its ${name}; the best eligible ${name} so far ends at ${objectiveText(objective, last?.best_so_far ?? null)}.`,
    featured: null,
    option: (theme) => convergenceOption(procedure, tried, capability, theme),
    table: {
      caption: `${label}: every point tried, in order, with its settings and numbers`,
      columns: ['Tried', 'Step', 'Value', 'Settings', ...SCORED_COLUMNS, `Best ${name} so far`],
      rows: tried.map((point) => ({
        key: String(point.order),
        cells: [
          String(point.order + 1),
          stepText(point, capability),
          point.value === null ? '—' : String(point.value),
          pointText(point.point, capability),
          ...scoredCells(point),
          objectiveText(objective, point.best_so_far),
        ],
      })),
    },
  };
}

function convergenceOption(procedure: ProcedureCharts, tried: readonly TriedPoint[], capability: StrategyCapability | null, theme: ChartTheme): ChartOption {
  const objective = procedure.policy.objective;
  const groups = [
    { group: 'eligible', name: 'Meets the rules', points: tried.filter((point) => point.ineligibility === null), color: theme.candidates.all_period },
    { group: 'ineligible', name: 'Fails a rule', points: tried.filter((point) => point.ineligibility !== null), color: theme.textSecondary },
  ];
  return {
    grid: { left: 52, right: 16, top: 16, bottom: 40 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const series = seriesIndexOf(params) ?? -1;
        const point = series === 2 ? tried[dataIndexOf(params) ?? -1] : groups[series]?.points[dataIndexOf(params) ?? -1];
        if (point === undefined) return '';
        const rows: TooltipRow[] = [...scoredRows(point), { label: `Best ${OBJECTIVE_NAMES[objective]} so far`, values: [objectiveText(objective, point.best_so_far)] }];
        const value = point.knob === null ? [] : [`Tried ${knobLabel(point.knob, capability)} = ${point.value}, every other knob as it stood.`];
        return tooltipHtml({ title: `Point ${point.order + 1} · ${stepText(point, capability)}`, columns: ['Value'], rows, notes: [...value, pointText(point.point, capability), rulesNote(procedure), DEVELOPMENT_NOTE] }, theme);
      },
    },
    xAxis: { type: 'value', min: 0, max: Math.max(1, tried.length - 1), minInterval: 1, name: 'Points tried, in order', nameLocation: 'middle', nameGap: 24, nameTextStyle: axisText(theme), axisLine: { lineStyle: { color: theme.axis } }, axisLabel: { ...axisText(theme), formatter: (value: number) => String(value + 1) }, splitLine: { show: false } },
    yAxis: { type: 'value', scale: true, splitNumber: 4, axisLabel: { ...axisText(theme), formatter: (value: number) => axisValue(objective, value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: [
      ...groups.map((group) => ({
        id: `${group.group}:tried`,
        name: group.name,
        type: 'scatter' as const,
        data: group.points.map((point) => [point.order, point.objective ?? '-']),
        symbolSize: 7,
        itemStyle: { color: group.color, opacity: group.group === 'eligible' ? 0.85 : 0.5 },
        emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      })),
      {
        id: 'best:tried',
        name: `Best ${OBJECTIVE_NAMES[objective]} so far`,
        type: 'line' as const,
        step: 'end' as const,
        data: tried.map((point) => [point.order, point.best_so_far ?? '-']),
        showSymbol: false,
        connectNulls: false,
        lineStyle: { color: theme.text, width: 2 },
        itemStyle: { color: theme.text },
        emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      },
    ],
  };
}

// ---------------------------------------------------------------- V8 knob moves

/** Each searched knob's starting and retained value, placed in its searched range. */
export function knobMovesSpec(procedure: ProcedureCharts, label: string): ChartSpec {
  const moves = procedure.moves;
  const moved = moves.filter((move) => move.moved).map((move) => move.label);
  return {
    label: `${label} knob moves`,
    summary: `${label}: ${moved.length === 0 ? 'no knob moved from its start' : `${moved.join(', ')} moved`}; each knob is placed in its own searched range.`,
    featured: null,
    option: (theme) => movesOption(procedure, theme),
    table: {
      caption: `${label}: each searched knob's start and retained value in its range`,
      columns: ['Knob', 'Range', 'Start', 'Retained', 'Edge of range'],
      rows: moves.map((move) => ({ key: move.name, cells: [move.label, `${move.low} to ${move.high}`, String(move.start), String(move.retained), move.edge_hit ? 'yes' : 'no'] })),
    },
  };
}

function positions(moves: ProcedureCharts['moves']): number[] {
  return moves.flatMap((move) => [move.start_position, move.retained_position]).filter((value): value is number => value !== null);
}

function movesOption(procedure: ProcedureCharts, theme: ChartTheme): ChartOption {
  const moves = procedure.moves;
  const ends = [
    { group: 'start', name: 'Start', positions: moves.map((move) => move.start_position), symbol: 'emptyCircle', color: theme.textSecondary },
    { group: 'retained', name: 'Retained', positions: moves.map((move) => move.retained_position), symbol: 'circle', color: theme.candidates.all_period },
  ];
  return {
    grid: { left: 110, right: 24, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const move = moves[dataIndexOf(params) ?? -1];
        if (move === undefined) return '';
        const rows: TooltipRow[] = [
          { label: 'Searched range', values: [`${move.low} to ${move.high}`] },
          { label: 'Start', values: [String(move.start)] },
          { label: 'Retained', values: [String(move.retained)] },
        ];
        const edge = move.edge_hit ? ['It ended at the edge of its range: a better value may lie outside it.'] : [];
        return tooltipHtml({ title: `${move.label} (${move.unit})`, columns: ['Value'], rows, notes: [...edge, move.moved ? 'The search moved it.' : 'The search kept its starting value.', DEVELOPMENT_NOTE] }, theme);
      },
    },
    // A start outside the searched range (the seed need not lie in it) stays on the axis.
    xAxis: {
      type: 'value',
      min: Math.min(0, ...positions(moves)),
      max: Math.max(1, ...positions(moves)),
      splitNumber: 2,
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel: { ...axisText(theme), formatter: (value: number) => (value === 0 ? 'low' : value === 1 ? 'high' : '') },
      splitLine: { lineStyle: { color: theme.gridLine } },
    },
    yAxis: { type: 'category', data: moves.map((move) => move.label), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisText(theme), width: 100, overflow: 'truncate' } },
    series: ends.map((end) => ({
      id: `${end.group}:moves`,
      name: end.name,
      type: 'scatter' as const,
      symbol: end.symbol,
      symbolSize: 11,
      data: end.positions.map((position, i) => ({
        value: [position ?? '-', i],
        itemStyle: { color: end.group === 'retained' && moves[i].edge_hit ? theme.warn : end.color },
      })),
      label: { show: end.group === 'retained', position: 'top' as const, color: theme.text, fontSize: 10, formatter: (params: { dataIndex: number }) => String(moves[params.dataIndex]?.retained ?? '') },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}

// ---------------------------------------------------------------- V9 one-knob profiles

const PROFILE_WIDTH = 260;

/** Each searched knob's results across the values tried, every other knob held; small multiples on one row. */
export function profilesSpec(procedure: ProcedureCharts, label: string): ChartSpec {
  const objective = procedure.policy.objective;
  const profiles = procedure.profiles;
  return {
    label: `${label} one-knob profiles`,
    summary: `${label}: the ${OBJECTIVE_NAMES[objective]} at each value tried of ${profiles.map((profile) => profile.label).join(', ')}, every other knob held.`,
    featured: null,
    option: (theme) => profilesOption(procedure, theme),
    table: {
      caption: `${label}: each knob's values tried, every other knob held as listed`,
      columns: ['Knob', 'Value', ...SCORED_COLUMNS, 'Retained', 'Others held at'],
      rows: profiles.flatMap((profile) =>
        profile.points.map((point) => ({
          key: `${profile.name}|${point.value}`,
          cells: [profile.label, String(point.value), ...scoredCells(point), point.retained ? 'yes' : '', heldText(profile)],
        })),
      ),
    },
  };
}

/** The width the profiles need: one small chart per knob. */
export function profilesMinWidth(count: number): number {
  return PROFILE_WIDTH * Math.max(1, count);
}

function heldText(profile: KnobProfile): string {
  return profile.held.map((knob) => `${knob.label} ${knob.value}`).join(' · ');
}

function profilesOption(procedure: ProcedureCharts, theme: ChartTheme): ChartOption {
  const objective = procedure.policy.objective;
  const profiles = procedure.profiles;
  const share = 100 / Math.max(1, profiles.length);
  return {
    // Each profile gets its own slot: a gutter for its value axis on the left, the plot in the rest.
    grid: profiles.map((_, i) => ({ left: `${i * share + share * 0.2}%`, width: `${share * 0.72}%`, top: 28, bottom: 40 })),
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const profile = profiles[seriesIndexOf(params) ?? -1];
        const point = profile?.points[dataIndexOf(params) ?? -1];
        if (profile === undefined || point === undefined) return '';
        const pass = profile.pass_index === null ? 'A slice of the grid through the winner.' : `Pass ${profile.pass_index + 1}: the last that searched this knob.`;
        return tooltipHtml(
          { title: `${profile.label} = ${point.value}${point.retained ? ' (retained)' : ''}`, columns: ['Value'], rows: scoredRows(point), notes: [pass, `Held: ${heldText(profile)}.`, rulesNote(procedure), DEVELOPMENT_NOTE] },
          theme,
        );
      },
    },
    xAxis: profiles.map((profile, i) => ({
      type: 'value' as const,
      gridIndex: i,
      min: Math.min(profile.low, ...profile.points.map((point) => point.value)),
      max: Math.max(profile.high, ...profile.points.map((point) => point.value)),
      splitNumber: 2,
      name: profile.label,
      nameLocation: 'middle' as const,
      nameGap: 24,
      nameTextStyle: axisText(theme),
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel: axisText(theme),
      splitLine: { show: false },
    })),
    yAxis: profiles.map((_, i) => ({
      type: 'value' as const,
      gridIndex: i,
      scale: true,
      splitNumber: 3,
      axisLabel: { ...axisText(theme), formatter: (value: number) => axisValue(objective, value) },
      splitLine: { lineStyle: { color: theme.gridLine } },
    })),
    series: profiles.map((profile, i) => ({
      id: `profile:${profile.name}`,
      name: profile.label,
      type: 'line' as const,
      xAxisIndex: i,
      yAxisIndex: i,
      data: profile.points.map((point) => ({
        value: [point.value, point.objective ?? '-'],
        symbol: point.ineligibility === null ? 'circle' : 'emptyCircle',
        symbolSize: point.retained ? 11 : 7,
        itemStyle: { color: point.retained ? theme.candidates.all_period : point.ineligibility === null ? theme.text : theme.textSecondary },
      })),
      connectNulls: false,
      lineStyle: { color: theme.textSecondary, width: 1 },
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
    })),
  };
}

// ---------------------------------------------------------------- V10 eligibility map

interface RuleGroup {
  readonly group: string;
  readonly name: string;
  readonly codes: readonly (string | null)[];
}

const RULE_GROUPS: readonly RuleGroup[] = [
  { group: 'eligible', name: 'Meets the rules', codes: [null] },
  { group: 'trades', name: 'Too few trades', codes: ['TOO_FEW_TRADES', 'NO_TRADES'] },
  { group: 'fall', name: 'Worst fall too deep', codes: ['DRAWDOWN_ABOVE_CEILING'] },
  { group: 'loss', name: 'Not profitable', codes: ['NOT_PROFITABLE'] },
  { group: 'other', name: 'No objective or worst fall', codes: ['FAILED', 'OBJECTIVE_UNDEFINED', 'DRAWDOWN_UNDEFINED', 'NOT_EVALUATED'] },
];

function groupOf(code: string | null): string {
  return RULE_GROUPS.find((group) => group.codes.includes(code))?.group ?? 'other';
}

/** Every point scored on the window by its trades and net profit, coloured by the rule it fails. */
export function eligibilityMapSpec(procedure: ProcedureCharts, capability: StrategyCapability | null, label: string): ChartSpec {
  const points = procedure.points;
  const failed = points.filter((point) => point.ineligibility === 'FAILED').length;
  return {
    label: `${label} eligibility map`,
    summary: `${label}: ${points.length} points scored on the window, each by its trades and net profit, coloured by the rule it fails${failed === 0 ? '' : `; ${failed} failed and have no numbers, listed in the table`}.`,
    featured: null,
    option: (theme) => eligibilityOption(procedure, capability, theme),
    table: {
      caption: `${label}: every point scored on the window, failed runs included`,
      columns: ['Settings', ...SCORED_COLUMNS],
      rows: points.map((point) => ({
        key: point.point_hash,
        cells: [`${pointText(point.point, capability)}${point.winner ? ' (winner)' : ''}`, ...scoredCells(point)],
      })),
    },
  };
}

function maxTrades(points: ProcedureCharts['points']): number {
  return Math.max(0, ...points.map((point) => point.total_trades ?? 0));
}

function eligibilityOption(procedure: ProcedureCharts, capability: StrategyCapability | null, theme: ChartTheme): ChartOption {
  const colors: Readonly<Record<string, string>> = { eligible: theme.candidates.all_period, trades: theme.warn, fall: theme.loss, loss: theme.stepBelow, other: theme.textSecondary };
  const groups = RULE_GROUPS.map((group) => ({ ...group, points: procedure.points.filter((point) => groupOf(point.ineligibility) === group.group) }));
  const floor = procedure.policy.min_trades;
  return {
    grid: { left: 56, right: 16, top: 16, bottom: 40 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const point = groups[seriesIndexOf(params) ?? -1]?.points[dataIndexOf(params) ?? -1];
        if (point === undefined) return '';
        return tooltipHtml(
          { title: point.winner ? 'The winner' : 'A scored point', columns: ['Value'], rows: scoredRows(point), notes: [pointText(point.point, capability), rulesNote(procedure), DEVELOPMENT_NOTE] },
          theme,
        );
      },
    },
    xAxis: {
      type: 'value',
      // The trade floor stays on the axis even when every point trades less; otherwise the axis keeps its round scale.
      ...(floor !== null && floor > maxTrades(procedure.points) ? { max: floor } : {}),
      name: 'Trades',
      nameLocation: 'middle',
      nameGap: 24,
      nameTextStyle: axisText(theme),
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel: axisText(theme),
      splitLine: { show: false },
    },
    yAxis: { type: 'value', splitNumber: 4, axisLabel: { ...axisText(theme), formatter: (value: number) => compactUsdText(value) }, splitLine: { lineStyle: { color: theme.gridLine } } },
    series: groups.map((group, i) => ({
      id: `${group.group}:points`,
      name: group.name,
      type: 'scatter' as const,
      data: group.points.map((point) => ({
        value: [point.total_trades ?? '-', point.net_profit ?? '-'],
        symbolSize: point.winner ? 14 : 6,
        itemStyle: { color: colors[group.group], opacity: point.winner ? 1 : 0.6, borderColor: point.winner ? theme.text : undefined, borderWidth: point.winner ? 2 : 0 },
      })),
      emphasis: { focus: 'series' as const, blurScope: 'global' as const },
      ...(i === 0
        ? {
            markLine: {
              silent: true,
              symbol: 'none',
              lineStyle: { color: theme.textSecondary, type: 'dashed' as const, width: 1 },
              label: { color: theme.textSecondary, fontSize: 10, formatter: (params: { name?: string }) => params.name ?? '' },
              data: [...(floor === null ? [] : [{ xAxis: floor, name: `trade floor ${floor}` }]), ...(procedure.policy.require_positive_net ? [{ yAxis: 0, name: 'no profit' }] : [])],
            },
          }
        : {}),
    })),
  };
}

// ---------------------------------------------------------------- V11 pair landscape

/** Two knobs' landscape of development net return, every other setting held; cells the rules could not test say why. */
export function pairLandscapeSpec(view: PairMapView): ChartSpec {
  const cells = view.rows.flat();
  return {
    label: `${view.title} landscape`,
    summary: `Development net return across ${view.yLabel} (rows) and ${view.xLabel} (columns); every other setting held.`,
    featured: null,
    option: (theme) => landscapeOption(view, cells, theme),
    table: {
      caption: `Development net return — rows: ${view.yLabel}, columns: ${view.xLabel}`,
      columns: [view.yLabel, ...view.xValues.map(String)],
      rows: view.rows.map((row, r) => ({ key: String(view.yValues[r]), cells: [String(view.yValues[r]), ...row.map((cell) => cell.words)] })),
    },
  };
}

function returnOf(cell: PairCellView): number | null {
  return cell.kind === 'tested' ? (cell.metrics?.total_return_pct ?? null) : null;
}

function landscapeOption(view: PairMapView, cells: readonly PairCellView[], theme: ChartTheme): ChartOption {
  const groups = [
    // A tested cell without a recorded return is drawn as a status cell, never as 0%.
    { group: 'tested', name: 'Tested', cells: cells.filter((cell) => returnOf(cell) !== null) },
    { group: 'other', name: 'Not tested', cells: cells.filter((cell) => returnOf(cell) === null) },
  ];
  // Display scale only: the colour runs from the deepest loss or gain to its mirror, with 0% in the middle.
  const reach = Math.max(1e-9, ...groups[0].cells.map((cell) => Math.abs(returnOf(cell) ?? 0)));
  return {
    grid: { left: 56, right: 12, top: 8, bottom: 40 },
    tooltip: {
      ...tooltipFrame(theme),
      trigger: 'item',
      formatter: (params: unknown) => {
        const cell = groups[seriesIndexOf(params) ?? -1]?.cells[dataIndexOf(params) ?? -1];
        if (cell === undefined) return '';
        const notes = [...(cell.center ? ['The candidate’s own settings.'] : []), 'Every other setting held at the candidate’s value.', DEVELOPMENT_NOTE];
        return tooltipHtml({ title: `${view.yLabel} ${cell.y} · ${view.xLabel} ${cell.x}`, columns: [], rows: [], notes: [cellDetail(view, cell), ...notes] }, theme);
      },
    },
    xAxis: { type: 'category', data: view.xValues.map(String), name: view.xLabel, nameLocation: 'middle', nameGap: 24, nameTextStyle: axisText(theme), axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: { ...axisText(theme), hideOverlap: true } },
    yAxis: { type: 'category', data: view.yValues.map(String), inverse: true, axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, axisLabel: axisText(theme) },
    visualMap: [
      { type: 'continuous', show: false, seriesIndex: 0, dimension: 2, min: -reach, max: reach, inRange: { color: [theme.loss, theme.neutral, theme.gain] } },
      { type: 'continuous', show: false, seriesIndex: 1, dimension: 2, min: -1, max: 1, inRange: { color: [theme.tooFew, theme.tooFew] } },
    ],
    series: groups.map((group) => ({
      id: `${group.group}:cells`,
      name: group.name,
      type: 'heatmap' as const,
      // A status cell's third value only picks its fixed grey (a heatmap skips an empty one); its mark is its label.
      data: group.cells.map((cell) => ({
        value: [cell.column, cell.row, returnOf(cell) ?? 0],
        itemStyle: cell.center ? { borderColor: theme.text, borderWidth: 2 } : { borderColor: theme.gridLine, borderWidth: 1 },
      })),
      // The return or the status mark in every cell, so colour is never the only cue.
      label: { show: true, color: group.group === 'tested' ? theme.text : theme.textSecondary, fontSize: 10, formatter: (params: { dataIndex: number }) => group.cells[params.dataIndex]?.text ?? '' },
      emphasis: { itemStyle: { borderColor: theme.text, borderWidth: 2 } },
    })),
  };
}
