import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { SpecStrategyService } from '../../services/spec-strategy.service';
import {
  Condition,
  ComparisonOp,
  IndicatorBlock,
  IndicatorComparisonCondition,
  IndicatorBetweenCondition,
  FreshCrossCondition,
  BarsSinceEntryCondition,
  TimeOfDayCondition,
  PnLPercentCondition,
  PnLPointsCondition,
  DrawdownFromPeakCondition,
  BarPropertyCondition,
  Operand,
  StrategySpec,
  SpecStrategyBacktestResult,
  EntryBlock,
} from '../../graphql/spec-strategy-types';
import { CANONICAL_FIXTURES, CanonicalFixture } from './canonical-fixtures';
import {
  CONDITION_CATALOG,
  ConditionGroup,
  ConditionKind,
  groupedConditionsForContext,
} from './condition-catalog';
import {
  buildSummaryFragments,
  formatConditionLine,
  formatEntryBlock,
  formatExitBlock,
  formatSurvivalBlock,
  SummaryFragment,
} from './plain-english';
import { collectIndicatorReferences, validateStrategy, ValidationIssue } from './validation';
import {
  addEntryCondition,
  addExitCondition,
  addIndicator,
  addSurvivalRule,
  buildCloseAllSurvivalRule,
  removeEntryConditionAt,
  removeExitConditionAt,
  removeIndicatorAt,
  removeSurvivalRuleAt,
  setEntryLogic,
  setEntrySize,
  setExitLogic,
  setStrategyName,
  updateEntryConditionAt,
  updateExitConditionAt,
  updateIndicatorAt,
  updateSurvivalRuleAt,
} from './spec-mutators';
import { SpecStrategyStore } from './strategy-store.service';

type LifecycleTab = 'entry' | 'manage' | 'exit';

const CONDITION_KINDS: readonly ConditionKind[] = [
  'IndicatorComparison',
  'IndicatorBetween',
  'FreshCross',
  'BarsSinceEntry',
  'TimeOfDay',
  'PnLPercent',
  'PnLPoints',
  'DrawdownFromPeak',
  'BarProperty',
];

interface QuickManageRule {
  readonly label: string;
  readonly build: () => { name: string; conditionKind: ConditionKind; value: number };
}

/**
 * Form-driven Strategy Spec runner.
 *
 * Edits the underlying ``StrategySpec`` via three tabs (Entry / Manage /
 * Exit), plus an Indicators panel above. Each condition card is type-
 * aware: the form fields differ per ``kind``. The JSON view is still
 * available as a collapsed Advanced panel — JSON remains the canonical
 * format and the form is just a structured projection over it.
 *
 * State model: a single ``signal<StrategySpec>`` is the source of
 * truth. All form events call into ``spec-mutators.ts`` immutable
 * helpers; the JSON view is a derived ``computed()`` of the spec.
 *
 * Persistence: saved strategies live in ``SpecStrategyStore``
 * (localStorage today, future server-backed). Save / Load / Clone are
 * exposed inline above the editor.
 */
@Component({
  selector: 'app-spec-strategy-runner',
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './spec-strategy-runner.component.html',
  styleUrl: './spec-strategy-runner.component.scss',
})
export class SpecStrategyRunnerComponent {
  private readonly specService = inject(SpecStrategyService);
  private readonly store = inject(SpecStrategyStore);

  // ---- Static data ------------------------------------------------------
  readonly fixtures: readonly CanonicalFixture[] = CANONICAL_FIXTURES;
  readonly indicatorKinds = ['SMA', 'EMA', 'RSI', 'ADX', 'MACD', 'SUPERTREND'] as const;
  readonly conditionKinds = CONDITION_KINDS;
  readonly comparisonOps: readonly ComparisonOp[] = ['<', '<=', '==', '>=', '>', '!='];
  readonly sources = ['open', 'high', 'low', 'close', 'hlc3', 'ohlc4'] as const;
  readonly barProperties = ['range', 'body', 'range_pct', 'body_pct'] as const;
  readonly directions = ['up', 'down'] as const;

  // ---- Spec state -------------------------------------------------------
  /** Source of truth for everything the editor renders. */
  readonly spec = signal<StrategySpec>(structuredClone(CANONICAL_FIXTURES[0].spec));
  readonly selectedFixtureId = signal<string>(CANONICAL_FIXTURES[0].id);
  readonly selectedTab = signal<LifecycleTab>('entry');
  /** Saved-store id of the currently-loaded strategy, if any. */
  readonly currentSavedId = signal<string | null>(null);

  // ---- Run controls (orthogonal to the spec) ----------------------------
  readonly startDate = signal<string>('2024-03-28');
  readonly endDate = signal<string>('2024-12-31');
  readonly initialCash = signal<number>(100000);
  readonly fillMode = signal<'signal_bar_close' | 'next_bar_open'>('signal_bar_close');

  // ---- Status / errors --------------------------------------------------
  readonly result = this.specService.result;
  readonly serviceError = this.specService.error;
  readonly loading = this.specService.loading;
  readonly localError = signal<string | null>(null);
  readonly statusMessage = signal<string | null>(null);

  // ---- Advanced JSON ----------------------------------------------------
  readonly showAdvancedJson = signal<boolean>(false);
  readonly jsonDraftText = signal<string>('');
  readonly jsonDraftError = signal<string | null>(null);

  // ---- Save dialog ------------------------------------------------------
  readonly showSaveDialog = signal<boolean>(false);
  readonly saveDialogName = signal<string>('');
  readonly saveDialogMode = signal<'save-as' | 'clone'>('save-as');

  // ---- Saved strategies (from store) -----------------------------------
  readonly savedStrategies = this.store.entries;

  // ---- Computed views ---------------------------------------------------
  readonly entrySummary = computed(() =>
    formatEntryBlock(this.spec().entry, this.spec().indicators),
  );
  readonly exitSummary = computed(() => formatExitBlock(this.spec().exit, this.spec().indicators));
  readonly survivalSummary = computed(() =>
    formatSurvivalBlock(this.spec().survival ?? [], this.spec().indicators),
  );
  readonly specJson = computed(() => JSON.stringify(this.spec(), null, 2));
  readonly tradeCount = computed<number>(() => this.result()?.totalTrades ?? 0);

  /** Rich-fragment summary for the design's Strategy Summary hero card. */
  readonly summaryFragments = computed<readonly SummaryFragment[]>(() =>
    buildSummaryFragments(this.spec()),
  );

  /** Set of indicator ids referenced anywhere in the spec — used to dim
   * unused indicators in the reference panel and flag them in validation. */
  readonly referencedIndicators = computed<Set<string>>(() =>
    collectIndicatorReferences(this.spec()),
  );

  /** All validation issues for the current spec + run config. */
  readonly issues = computed<readonly ValidationIssue[]>(() =>
    validateStrategy(this.spec(), {
      start: this.startDate(),
      end: this.endDate(),
      initialCash: this.initialCash(),
      fillMode: this.fillMode(),
      resolutionMinutes: this.spec().resolution.period_minutes,
    }),
  );
  readonly errors = computed(() => this.issues().filter((i) => i.sev === 'error'));
  readonly warnings = computed(() => this.issues().filter((i) => i.sev === 'warn'));
  readonly infos = computed(() => this.issues().filter((i) => i.sev === 'info'));
  readonly canRun = computed(() => this.errors().length === 0);

  /** Per-condition read/edit toggle state. Key format: "ctx:ruleIndex:index"
   * (ruleIndex = -1 for entry/exit; positive for manage rule). */
  readonly editingKeys = signal<Set<string>>(new Set());

  /** One-line summary lines per stage — used by the lifecycle pipeline. */
  readonly entryLines = computed<string[]>(() =>
    (this.spec().entry?.conditions ?? []).map((c) => formatConditionLine(c, this.spec().indicators)),
  );
  readonly exitLines = computed<string[]>(() =>
    (this.spec().exit?.conditions ?? []).map((c) => formatConditionLine(c, this.spec().indicators)),
  );
  readonly manageLines = computed<string[]>(() =>
    (this.spec().survival ?? []).map((r) => `${r.name} → close`),
  );

  /** Dirty marker — whether the current spec differs from the last saved state. */
  readonly lastSavedSnapshot = signal<string>('');
  readonly isDirty = computed<boolean>(() => {
    const cur = this.specJson();
    const saved = this.lastSavedSnapshot();
    if (this.currentSavedId() == null) return false; // never saved → no dirty marker
    return cur !== saved;
  });

  // ---- Quick manage-rule presets (from the design) ---------------------
  readonly quickManageRules: readonly QuickManageRule[] = [
    {
      label: '+ 1.5% stop-loss',
      build: () => ({ name: 'Stop loss', conditionKind: 'PnLPercent', value: -0.015 }),
    },
    {
      label: '+ 3% take-profit',
      build: () => ({ name: 'Take profit', conditionKind: 'PnLPercent', value: 0.03 }),
    },
    {
      label: '+ Trailing stop',
      build: () => ({ name: 'Trailing stop', conditionKind: 'DrawdownFromPeak', value: 0.005 }),
    },
  ];

  // -----------------------------------------------------------------------
  // Fixture / saved load
  // -----------------------------------------------------------------------
  selectFixture(id: string): void {
    this.selectedFixtureId.set(id);
    const fixture = this.fixtures.find((f) => f.id === id);
    if (!fixture) return;
    this.spec.set(structuredClone(fixture.spec));
    this.currentSavedId.set(null);
    this.localError.set(null);
    this.statusMessage.set(`Loaded fixture "${fixture.label}".`);
  }

  loadSaved(id: string): void {
    const saved = this.store.getById(id);
    if (!saved) return;
    this.spec.set(structuredClone(saved.spec));
    this.currentSavedId.set(saved.id);
    this.snapshotCurrentSpec();
    this.localError.set(null);
    this.statusMessage.set(`Loaded "${saved.name}".`);
  }

  // -----------------------------------------------------------------------
  // Top-level spec edits
  // -----------------------------------------------------------------------
  setName(name: string): void {
    this.spec.update((s) => setStrategyName(s, name));
  }

  selectTab(tab: LifecycleTab): void {
    this.selectedTab.set(tab);
  }

  // -----------------------------------------------------------------------
  // Indicators
  // -----------------------------------------------------------------------
  addIndicatorOfKind(kind: IndicatorBlock['kind']): void {
    this.spec.update((s) => addIndicator(s, this.defaultIndicator(kind, s.indicators)));
  }

  removeIndicator(index: number): void {
    this.spec.update((s) => removeIndicatorAt(s, index));
  }

  updateIndicatorField(index: number, patch: Partial<IndicatorBlock>): void {
    this.spec.update((s) => updateIndicatorAt(s, index, patch));
  }

  private defaultIndicator(
    kind: IndicatorBlock['kind'],
    existing: readonly IndicatorBlock[],
  ): IndicatorBlock {
    const count = existing.filter((i) => i.kind === kind).length + 1;
    const id = `${kind.toLowerCase()}_${count}`;
    switch (kind) {
      case 'SMA':
      case 'EMA':
        return { id, kind, period: 20, source: 'close' };
      case 'RSI':
        return { id, kind, period: 14, source: 'close', ma_type: 'wilders' };
      case 'ADX':
        return { id, kind, period: 14 };
      case 'MACD':
        return { id, kind, period: 26, fast_period: 12, signal_period: 9, source: 'close' };
      case 'SUPERTREND':
        return { id, kind, period: 10, multiplier: 3.0 };
    }
  }

  // -----------------------------------------------------------------------
  // Entry conditions
  // -----------------------------------------------------------------------
  addEntryConditionOfKind(kind: ConditionKind): void {
    this.spec.update((s) => addEntryCondition(s, this.defaultCondition(kind, s.indicators)));
  }

  removeEntryCondition(index: number): void {
    this.spec.update((s) => removeEntryConditionAt(s, index));
  }

  updateEntryCondition(index: number, cond: Condition): void {
    this.spec.update((s) => updateEntryConditionAt(s, index, cond));
  }

  setEntryLogic(logic: 'AND' | 'OR'): void {
    this.spec.update((s) => setEntryLogic(s, logic));
  }

  setEntrySizeFraction(fraction: number): void {
    const safe = Math.max(0.01, Math.min(1, fraction));
    this.spec.update((s) => setEntrySize(s, { kind: 'SetHoldings', fraction: safe }));
  }

  // -----------------------------------------------------------------------
  // Exit conditions
  // -----------------------------------------------------------------------
  addExitConditionOfKind(kind: ConditionKind): void {
    this.spec.update((s) => addExitCondition(s, this.defaultCondition(kind, s.indicators)));
  }

  removeExitCondition(index: number): void {
    this.spec.update((s) => removeExitConditionAt(s, index));
  }

  updateExitCondition(index: number, cond: Condition): void {
    this.spec.update((s) => updateExitConditionAt(s, index, cond));
  }

  setExitLogic(logic: 'AND' | 'OR'): void {
    this.spec.update((s) => setExitLogic(s, logic));
  }

  // -----------------------------------------------------------------------
  // Survival (Manage) rules
  // -----------------------------------------------------------------------
  addManageRule(): void {
    const rule = buildCloseAllSurvivalRule('new rule', {
      logic: 'AND',
      conditions: [{ kind: 'PnLPercent', op: '<=', value: -0.01 }],
    });
    this.spec.update((s) => addSurvivalRule(s, rule));
  }

  removeManageRule(index: number): void {
    this.spec.update((s) => removeSurvivalRuleAt(s, index));
  }

  updateManageRuleName(index: number, name: string): void {
    this.spec.update((s) => {
      const rules = s.survival ?? [];
      const rule = rules[index];
      if (!rule) return s;
      return updateSurvivalRuleAt(s, index, { ...rule, name });
    });
  }

  updateManageRuleCondition(ruleIndex: number, condIndex: number, cond: Condition): void {
    this.spec.update((s) => {
      const rules = s.survival ?? [];
      const rule = rules[ruleIndex];
      if (!rule) return s;
      const conditions = [...rule.when.conditions];
      conditions[condIndex] = cond;
      return updateSurvivalRuleAt(s, ruleIndex, {
        ...rule,
        when: { ...rule.when, conditions },
      });
    });
  }

  addManageRuleCondition(ruleIndex: number, kind: ConditionKind): void {
    this.spec.update((s) => {
      const rules = s.survival ?? [];
      const rule = rules[ruleIndex];
      if (!rule) return s;
      const newCond = this.defaultCondition(kind, s.indicators);
      return updateSurvivalRuleAt(s, ruleIndex, {
        ...rule,
        when: {
          ...rule.when,
          conditions: [...rule.when.conditions, newCond],
        },
      });
    });
  }

  removeManageRuleCondition(ruleIndex: number, condIndex: number): void {
    this.spec.update((s) => {
      const rules = s.survival ?? [];
      const rule = rules[ruleIndex];
      if (!rule) return s;
      const conditions = rule.when.conditions.filter((_, i) => i !== condIndex);
      return updateSurvivalRuleAt(s, ruleIndex, {
        ...rule,
        when: { ...rule.when, conditions },
      });
    });
  }

  // -----------------------------------------------------------------------
  // Default condition constructors — sensible blanks so the form is
  // never empty when a new card is added.
  // -----------------------------------------------------------------------
  private defaultCondition(
    kind: ConditionKind,
    indicators: readonly IndicatorBlock[],
  ): Condition {
    const firstId = indicators[0]?.id ?? '';
    const secondId = indicators[1]?.id ?? indicators[0]?.id ?? '';
    switch (kind) {
      case 'IndicatorComparison':
        return {
          kind: 'IndicatorComparison',
          left: { kind: 'IndicatorRef', indicator: firstId },
          op: '>',
          right: { kind: 'Const', value: 0 },
        };
      case 'IndicatorBetween':
        return { kind: 'IndicatorBetween', indicator: firstId, lo: 30, hi: 70, inclusive: true };
      case 'FreshCross':
        return { kind: 'FreshCross', left: firstId, right: secondId, direction: 'up' };
      case 'BarsSinceEntry':
        return { kind: 'BarsSinceEntry', op: '>=', value: 5 };
      case 'TimeOfDay':
        return { kind: 'TimeOfDay', after: '09:45', before: '15:30', tz: 'America/New_York' };
      case 'PnLPercent':
        return { kind: 'PnLPercent', op: '<=', value: -0.01 };
      case 'PnLPoints':
        return { kind: 'PnLPoints', op: '<=', value: -1 };
      case 'DrawdownFromPeak':
        return { kind: 'DrawdownFromPeak', value: 0.005 };
      case 'BarProperty':
        return { kind: 'BarProperty', property: 'range_pct', op: '>=', value: 0.003 };
    }
  }

  // -----------------------------------------------------------------------
  // Operand helpers — for the IndicatorComparison editor's two-operand UI.
  // We only allow IndicatorRef, Const, and Subtract(IndicatorRef -
  // IndicatorRef) in the form — Subtract is a one-shot helper for the
  // common "EMA gap" case rather than a full nested-AST editor.
  // -----------------------------------------------------------------------
  operandKindOf(op: Operand): 'IndicatorRef' | 'Const' | 'Subtract' {
    return op.kind === 'IndicatorRef' || op.kind === 'Const' || op.kind === 'Subtract'
      ? op.kind
      : 'Const';
  }

  changeOperandKind(
    op: Operand,
    newKind: 'IndicatorRef' | 'Const' | 'Subtract',
    indicators: readonly IndicatorBlock[],
  ): Operand {
    const firstId = indicators[0]?.id ?? '';
    const secondId = indicators[1]?.id ?? firstId;
    if (newKind === 'IndicatorRef') return { kind: 'IndicatorRef', indicator: firstId };
    if (newKind === 'Const') return { kind: 'Const', value: 0 };
    return {
      kind: 'Subtract',
      left: { kind: 'IndicatorRef', indicator: firstId },
      right: { kind: 'IndicatorRef', indicator: secondId },
    };
  }

  // -----------------------------------------------------------------------
  // Typed-update helpers for condition form fields. Each takes the
  // current condition + a patch and returns the updated condition.
  // The template wires these via (ngModelChange) so the component owns
  // immutability and the template stays close to declarative.
  // -----------------------------------------------------------------------
  patchIndicatorComparison(
    base: IndicatorComparisonCondition,
    patch: Partial<IndicatorComparisonCondition>,
  ): IndicatorComparisonCondition {
    return { ...base, ...patch };
  }

  patchIndicatorBetween(
    base: IndicatorBetweenCondition,
    patch: Partial<IndicatorBetweenCondition>,
  ): IndicatorBetweenCondition {
    return { ...base, ...patch };
  }

  patchFreshCross(
    base: FreshCrossCondition,
    patch: Partial<FreshCrossCondition>,
  ): FreshCrossCondition {
    return { ...base, ...patch };
  }

  patchBarsSinceEntry(
    base: BarsSinceEntryCondition,
    patch: Partial<BarsSinceEntryCondition>,
  ): BarsSinceEntryCondition {
    return { ...base, ...patch };
  }

  patchTimeOfDay(base: TimeOfDayCondition, patch: Partial<TimeOfDayCondition>): TimeOfDayCondition {
    return { ...base, ...patch };
  }

  patchPnLPercent(
    base: PnLPercentCondition,
    patch: Partial<PnLPercentCondition>,
  ): PnLPercentCondition {
    return { ...base, ...patch };
  }

  patchPnLPoints(
    base: PnLPointsCondition,
    patch: Partial<PnLPointsCondition>,
  ): PnLPointsCondition {
    return { ...base, ...patch };
  }

  patchDrawdownFromPeak(
    base: DrawdownFromPeakCondition,
    patch: Partial<DrawdownFromPeakCondition>,
  ): DrawdownFromPeakCondition {
    return { ...base, ...patch };
  }

  patchBarProperty(
    base: BarPropertyCondition,
    patch: Partial<BarPropertyCondition>,
  ): BarPropertyCondition {
    return { ...base, ...patch };
  }

  // -----------------------------------------------------------------------
  // JSON Advanced view
  // -----------------------------------------------------------------------
  openAdvancedJson(): void {
    this.jsonDraftText.set(this.specJson());
    this.jsonDraftError.set(null);
    this.showAdvancedJson.set(true);
  }

  cancelAdvancedJson(): void {
    this.showAdvancedJson.set(false);
    this.jsonDraftError.set(null);
  }

  applyAdvancedJson(): void {
    try {
      const parsed = JSON.parse(this.jsonDraftText()) as StrategySpec;
      this.spec.set(parsed);
      this.jsonDraftError.set(null);
      this.showAdvancedJson.set(false);
      this.statusMessage.set('Spec updated from JSON.');
    } catch (e) {
      this.jsonDraftError.set(e instanceof Error ? e.message : String(e));
    }
  }

  // -----------------------------------------------------------------------
  // Save / load / clone
  // -----------------------------------------------------------------------
  openSaveDialog(mode: 'save-as' | 'clone'): void {
    this.saveDialogMode.set(mode);
    const current = this.spec().name;
    const suggested = mode === 'clone' ? `${current} (copy)` : current;
    this.saveDialogName.set(suggested);
    this.showSaveDialog.set(true);
  }

  cancelSaveDialog(): void {
    this.showSaveDialog.set(false);
  }

  confirmSaveDialog(): void {
    const name = this.saveDialogName().trim();
    if (!name) return;
    if (this.saveDialogMode() === 'clone') {
      // Clone always creates a fresh entry, even if currentSavedId is set.
      const namedSpec = setStrategyName(this.spec(), name);
      const saved = this.store.save(name, namedSpec);
      this.spec.set(structuredClone(saved.spec));
      this.currentSavedId.set(saved.id);
      this.snapshotCurrentSpec();
      this.statusMessage.set(`Cloned as "${name}".`);
    } else {
      // Save-as creates a new entry; update the spec.name to match.
      const namedSpec = setStrategyName(this.spec(), name);
      const saved = this.store.save(name, namedSpec);
      this.spec.set(structuredClone(saved.spec));
      this.currentSavedId.set(saved.id);
      this.snapshotCurrentSpec();
      this.statusMessage.set(`Saved as "${name}".`);
    }
    this.showSaveDialog.set(false);
  }

  saveOverExisting(): void {
    const id = this.currentSavedId();
    if (!id) {
      this.openSaveDialog('save-as');
      return;
    }
    this.store.save(this.spec().name, this.spec(), id);
    this.snapshotCurrentSpec();
    this.statusMessage.set('Saved.');
  }

  deleteSaved(id: string): void {
    this.store.remove(id);
    if (this.currentSavedId() === id) this.currentSavedId.set(null);
    this.statusMessage.set('Deleted.');
  }

  // -----------------------------------------------------------------------
  // Run backtest
  // -----------------------------------------------------------------------
  async runBacktest(): Promise<void> {
    this.localError.set(null);
    try {
      await this.specService.runBacktest(this.spec(), {
        startDate: this.startDate(),
        endDate: this.endDate(),
        initialCash: this.initialCash(),
        fillMode: this.fillMode(),
      });
    } catch {
      // Service signal already captures the error.
    }
  }

  // -----------------------------------------------------------------------
  // Display helpers (kept from previous version)
  // -----------------------------------------------------------------------
  formatTime(ms: number): string {
    return new Date(ms).toLocaleString('en-US', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'America/New_York',
    });
  }

  formatIndicators(trade: SpecStrategyBacktestResult['trades'][0]): string {
    return trade.indicators.map((entry) => `${entry.name}=${entry.value.toFixed(4)}`).join(', ');
  }

  // -----------------------------------------------------------------------
  // Template-narrowing helpers — TS can't refine the union when the
  // template uses a `kind` discriminator inside @switch unless the
  // expression is a method call. These keep the template type-safe.
  // -----------------------------------------------------------------------
  asIndicatorComparison(c: Condition): IndicatorComparisonCondition {
    return c as IndicatorComparisonCondition;
  }
  asIndicatorBetween(c: Condition): IndicatorBetweenCondition {
    return c as IndicatorBetweenCondition;
  }
  asFreshCross(c: Condition): FreshCrossCondition {
    return c as FreshCrossCondition;
  }
  asBarsSinceEntry(c: Condition): BarsSinceEntryCondition {
    return c as BarsSinceEntryCondition;
  }
  asTimeOfDay(c: Condition): TimeOfDayCondition {
    return c as TimeOfDayCondition;
  }
  asPnLPercent(c: Condition): PnLPercentCondition {
    return c as PnLPercentCondition;
  }
  asPnLPoints(c: Condition): PnLPointsCondition {
    return c as PnLPointsCondition;
  }
  asDrawdownFromPeak(c: Condition): DrawdownFromPeakCondition {
    return c as DrawdownFromPeakCondition;
  }
  asBarProperty(c: Condition): BarPropertyCondition {
    return c as BarPropertyCondition;
  }

  /** Type-narrowing access for entry.size — Phase 1 always SetHoldings. */
  entrySizeFraction(entry: EntryBlock): number {
    return entry.size.kind === 'SetHoldings' ? entry.size.fraction : 1;
  }

  /** Type guard for survival-rule conditions inside the template @for. */
  asCondition(c: Condition | { logic: 'AND' | 'OR' }): Condition {
    return c as Condition;
  }

  // -----------------------------------------------------------------------
  // Template event routers — let the condition-card sub-template stay
  // context-agnostic (entry vs exit vs manage). The card emits an
  // updated Condition; this method routes it to the right mutator.
  // -----------------------------------------------------------------------
  emitCondChange(
    ctx: 'entry' | 'exit' | 'manage',
    ruleIndex: number | undefined,
    index: number,
    cond: Condition,
  ): void {
    if (ctx === 'entry') {
      this.updateEntryCondition(index, cond);
    } else if (ctx === 'exit') {
      this.updateExitCondition(index, cond);
    } else if (ctx === 'manage' && ruleIndex !== undefined) {
      this.updateManageRuleCondition(ruleIndex, index, cond);
    }
  }

  /** Route an operand change inside an IndicatorComparison through to the
   * right mutator. Side picks left vs right; we re-emit a full updated
   * IndicatorComparison since the operand belongs to one. */
  emitOperandChange(
    side: 'left' | 'right',
    base: IndicatorComparisonCondition,
    ctx: 'entry' | 'exit' | 'manage',
    ruleIndex: number | undefined,
    index: number,
    op: Operand,
  ): void {
    const next: IndicatorComparisonCondition =
      side === 'left' ? { ...base, left: op } : { ...base, right: op };
    this.emitCondChange(ctx, ruleIndex, index, next);
  }

  // -----------------------------------------------------------------------
  // Read/edit toggle for condition cards.
  //
  // Cards default to read mode (colored category tag + plain English).
  // Click the edit icon to expand into form-field mode. Keeping read mode
  // as default makes the page much less noisy when scanning a many-rule
  // strategy.
  // -----------------------------------------------------------------------
  private editingKeyFor(ctx: 'entry' | 'exit' | 'manage', ruleIndex: number, index: number): string {
    return `${ctx}:${ruleIndex}:${index}`;
  }

  isEditing(ctx: 'entry' | 'exit' | 'manage', ruleIndex: number, index: number): boolean {
    return this.editingKeys().has(this.editingKeyFor(ctx, ruleIndex, index));
  }

  toggleEditing(ctx: 'entry' | 'exit' | 'manage', ruleIndex: number, index: number): void {
    const key = this.editingKeyFor(ctx, ruleIndex, index);
    const next = new Set(this.editingKeys());
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.editingKeys.set(next);
  }

  // -----------------------------------------------------------------------
  // Catalog accessors — let the template ask "what's the friendly label /
  // group / blurb for this kind?" without re-importing the catalog file.
  // -----------------------------------------------------------------------
  conditionLabel(kind: string): string {
    return CONDITION_CATALOG[kind as ConditionKind]?.label ?? kind;
  }

  conditionGroup(kind: string): ConditionGroup {
    return CONDITION_CATALOG[kind as ConditionKind]?.group ?? 'signal';
  }

  conditionBlurb(kind: ConditionKind): string {
    return CONDITION_CATALOG[kind].blurb;
  }

  conditionExample(kind: ConditionKind): string {
    return CONDITION_CATALOG[kind].example;
  }

  conditionShort(kind: ConditionKind): string {
    return CONDITION_CATALOG[kind].short;
  }

  groupedConditions(ctx: 'entry' | 'exit' | 'manage') {
    return groupedConditionsForContext(ctx);
  }

  /** Plain-English line for a single condition — used in read-mode card. */
  conditionPlainText(c: Condition): string {
    return formatConditionLine(c, this.spec().indicators);
  }

  // -----------------------------------------------------------------------
  // Quick manage-rule presets — from the design's "+ 1.5% stop-loss" /
  // "+ 3% take-profit" / "+ Trailing stop" shortcut buttons.
  // -----------------------------------------------------------------------
  addQuickManageRule(preset: QuickManageRule): void {
    const built = preset.build();
    const cond: Condition =
      built.conditionKind === 'PnLPercent'
        ? { kind: 'PnLPercent', op: built.value < 0 ? '<=' : '>=', value: built.value }
        : { kind: 'DrawdownFromPeak', value: built.value };
    this.spec.update((s) =>
      addSurvivalRule(s, {
        name: built.name,
        when: { logic: 'AND', conditions: [cond] },
        action: { kind: 'CLOSE_ALL' },
      }),
    );
  }

  // -----------------------------------------------------------------------
  // Validation fix shortcuts — when an issue carries a ``fix`` label,
  // the template renders it as a clickable hint. This handler implements
  // the small library of fixes the validation issues offer.
  // -----------------------------------------------------------------------
  applyValidationFix(issue: ValidationIssue): void {
    if (!issue.fix) return;
    if (issue.fix === 'Add EMA-9') {
      this.spec.update((s) =>
        addIndicator(s, { id: 'ema_9', kind: 'EMA', period: 9, source: 'close' }),
      );
      this.statusMessage.set('Added EMA-9 indicator.');
      return;
    }
    if (issue.fix === 'Add a 1.5% stop-loss') {
      this.addQuickManageRule(this.quickManageRules[0]);
      this.statusMessage.set('Added 1.5% stop-loss manage rule.');
      return;
    }
  }

  // -----------------------------------------------------------------------
  // Save snapshot tracking — used by the dirty marker.
  // -----------------------------------------------------------------------
  private snapshotCurrentSpec(): void {
    this.lastSavedSnapshot.set(this.specJson());
  }
}
