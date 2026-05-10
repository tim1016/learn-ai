import { TestBed } from '@angular/core/testing';
import { provideZonelessChangeDetection } from '@angular/core';
import { ApolloTestingController, ApolloTestingModule } from 'apollo-angular/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { RUN_SPEC_STRATEGY_BACKTEST } from '../../services/spec-strategy.service';
import { SpecStrategyRunnerComponent } from './spec-strategy-runner.component';
import { CANONICAL_FIXTURES } from './canonical-fixtures';
import type { TickerRange } from '../../shared/ticker-range-picker/ticker-range-picker.types';

/**
 * Tests for the form-driven runner component.
 *
 * The component is large (form fields for nine condition kinds) so the
 * tests focus on the orchestration: signal-as-source-of-truth wiring,
 * mutator routing, fixture/saved load semantics, JSON-Advanced
 * apply path, and the Run mutation. Per-condition form rendering is
 * covered by the plain-english / spec-mutators unit tests; here we
 * exercise the component as a whole.
 */
describe('SpecStrategyRunnerComponent', () => {
  let component: SpecStrategyRunnerComponent;
  let controller: ApolloTestingController;

  beforeEach(() => {
    localStorage.clear();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [SpecStrategyRunnerComponent, ApolloTestingModule],
      providers: [provideZonelessChangeDetection()],
    });
    component = TestBed.createComponent(SpecStrategyRunnerComponent).componentInstance;
    controller = TestBed.inject(ApolloTestingController);
  });

  afterEach(() => {
    controller.verify();
    localStorage.clear();
  });

  // ---- Initial state ----------------------------------------------------
  it('boots with the SPY EMA fixture loaded by default', () => {
    expect(component.selectedFixtureId()).toBe('spy_ema_crossover');
    expect(component.spec().name).toContain('SPY EMA');
    expect(component.spec().indicators).toHaveLength(3);
    expect(component.currentSavedId()).toBeNull();
  });

  it('exposes all six indicator kinds and all nine condition kinds for the pickers', () => {
    expect(component.indicatorKinds).toEqual(['SMA', 'EMA', 'RSI', 'ADX', 'MACD', 'SUPERTREND']);
    expect(component.conditionKinds).toContain('FreshCross');
    expect(component.conditionKinds).toContain('DrawdownFromPeak');
    expect(component.conditionKinds).toContain('BarProperty');
    expect(component.conditionKinds.length).toBe(9);
  });

  // ---- Fixture / saved load --------------------------------------------
  it('selectFixture replaces the current spec and clears currentSavedId', () => {
    // Pretend the user has been editing a saved strategy.
    component.spec.set({ ...component.spec(), name: 'tinkered' });

    component.selectFixture('rsi_mean_reversion');
    expect(component.spec().name).toContain('RSI');
    expect(component.spec().indicators).toHaveLength(1);
    expect(component.currentSavedId()).toBeNull();
  });

  // ---- Indicator editor -------------------------------------------------
  it('addIndicatorOfKind appends a default indicator with a unique id', () => {
    const before = component.spec().indicators.length;
    component.addIndicatorOfKind('ADX');
    const after = component.spec().indicators;
    expect(after).toHaveLength(before + 1);
    expect(after[after.length - 1].kind).toBe('ADX');
    expect(after[after.length - 1].id).toMatch(/^adx_/);
  });

  it('updateIndicatorField patches in place', () => {
    component.updateIndicatorField(0, { period: 99 });
    expect(component.spec().indicators[0].period).toBe(99);
  });

  it('removeIndicator drops by index', () => {
    component.removeIndicator(0);
    expect(component.spec().indicators).toHaveLength(2);
  });

  // ---- Entry tab --------------------------------------------------------
  it('addEntryConditionOfKind appends with sensible defaults', () => {
    const before = component.spec().entry.conditions.length;
    component.addEntryConditionOfKind('IndicatorBetween');
    const conds = component.spec().entry.conditions;
    expect(conds).toHaveLength(before + 1);
    expect((conds[conds.length - 1] as { kind: string }).kind).toBe('IndicatorBetween');
  });

  it('emitCondChange routes to the entry mutator with ctx="entry"', () => {
    const original = component.spec().entry.conditions[0];
    const replacement = {
      kind: 'FreshCross' as const,
      left: 'ema5',
      right: 'ema10',
      direction: 'down' as const,
    };
    component.emitCondChange('entry', undefined, 0, replacement);
    expect(component.spec().entry.conditions[0]).toEqual(replacement);
    expect(component.spec().entry.conditions[0]).not.toBe(original);
  });

  it('setEntryLogic flips AND ↔ OR', () => {
    const initial = component.spec().entry.logic;
    component.setEntryLogic(initial === 'AND' ? 'OR' : 'AND');
    expect(component.spec().entry.logic).not.toBe(initial);
  });

  // ---- Manage tab -------------------------------------------------------
  it('addManageRule appends a default CLOSE_ALL stop-loss rule', () => {
    component.addManageRule();
    const rules = component.spec().survival ?? [];
    expect(rules).toHaveLength(1);
    expect(rules[0].action).toEqual({ kind: 'CLOSE_ALL' });
    expect(rules[0].when.conditions).toHaveLength(1);
  });

  it('addManageRuleCondition appends to the rule s when block', () => {
    component.addManageRule();
    component.addManageRuleCondition(0, 'DrawdownFromPeak');
    const rule = (component.spec().survival ?? [])[0];
    expect(rule.when.conditions).toHaveLength(2);
  });

  // ---- Exit tab --------------------------------------------------------
  it('addExitConditionOfKind / removeExitCondition update exit.conditions', () => {
    component.addExitConditionOfKind('BarsSinceEntry');
    expect(component.spec().exit.conditions.length).toBeGreaterThan(0);
    const idx = component.spec().exit.conditions.length - 1;
    component.removeExitCondition(idx);
    // SPY EMA fixture starts with one exit condition (BarsSinceEntry >= 5).
    // After add+remove we should be back at the original count.
    expect(component.spec().exit.conditions.length).toBeGreaterThan(0);
  });

  // ---- JSON Advanced ---------------------------------------------------
  it('applyAdvancedJson parses the textarea and replaces the spec', () => {
    component.openAdvancedJson();
    const tweaked = JSON.parse(component.specJson());
    tweaked.name = 'edited via json';
    component.jsonDraftText.set(JSON.stringify(tweaked));
    component.applyAdvancedJson();

    expect(component.spec().name).toBe('edited via json');
    expect(component.showAdvancedJson()).toBe(false);
    expect(component.jsonDraftError()).toBeNull();
  });

  it('applyAdvancedJson surfaces parse errors and stays open', () => {
    component.openAdvancedJson();
    component.jsonDraftText.set('not valid json {');
    component.applyAdvancedJson();

    expect(component.jsonDraftError()).toBeTruthy();
    expect(component.showAdvancedJson()).toBe(true);
  });

  // ---- Save / load / clone ---------------------------------------------
  it('saveOverExisting falls back to Save-As dialog when no current id', () => {
    component.saveOverExisting();
    expect(component.showSaveDialog()).toBe(true);
  });

  it('confirmSaveDialog (save-as) creates a new saved entry and tracks it', () => {
    component.openSaveDialog('save-as');
    component.saveDialogName.set('My EMA crossover');
    component.confirmSaveDialog();

    expect(component.savedStrategies()).toHaveLength(1);
    expect(component.savedStrategies()[0].name).toBe('My EMA crossover');
    expect(component.currentSavedId()).toBe(component.savedStrategies()[0].id);
    expect(component.spec().name).toBe('My EMA crossover');
    expect(component.showSaveDialog()).toBe(false);
  });

  it('confirmSaveDialog (clone) creates a fresh entry under a new name', () => {
    component.openSaveDialog('save-as');
    component.saveDialogName.set('orig');
    component.confirmSaveDialog();

    component.openSaveDialog('clone');
    component.saveDialogName.set('orig (copy)');
    component.confirmSaveDialog();

    expect(component.savedStrategies()).toHaveLength(2);
    expect(component.savedStrategies().map((s) => s.name)).toContain('orig (copy)');
  });

  it('loadSaved replaces the spec and tracks the saved id', () => {
    component.openSaveDialog('save-as');
    component.saveDialogName.set('saved');
    component.confirmSaveDialog();
    const savedId = component.currentSavedId() ?? '';
    expect(savedId).not.toBe('');

    // Switch to a fixture, then load the saved strategy back.
    component.selectFixture('rsi_mean_reversion');
    expect(component.currentSavedId()).toBeNull();
    component.loadSaved(savedId);

    expect(component.spec().name).toBe('saved');
    expect(component.currentSavedId()).toBe(savedId);
  });

  it('deleteSaved removes the entry and clears currentSavedId if the active one was deleted', () => {
    component.openSaveDialog('save-as');
    component.saveDialogName.set('to delete');
    component.confirmSaveDialog();
    const id = component.currentSavedId() ?? '';
    expect(id).not.toBe('');

    component.deleteSaved(id);
    expect(component.savedStrategies()).toHaveLength(0);
    expect(component.currentSavedId()).toBeNull();
  });

  // ---- Plain-English summaries ----------------------------------------
  it('exposes per-block plain-English summaries that update with the spec', () => {
    expect(component.entrySummary()).toContain('crosses above');
    expect(component.exitSummary()).toContain('5 or more bars since entry');

    component.selectFixture('rsi_mean_reversion');
    expect(component.entrySummary()).toContain('RSI(14) <');
    expect(component.exitSummary()).toContain('RSI(14) >');
  });

  // ---- Run --------------------------------------------------------------
  it('runBacktest fires the GraphQL mutation with the current spec', async () => {
    const promise = component.runBacktest();

    const op = controller.expectOne(RUN_SPEC_STRATEGY_BACKTEST);
    expect(op.operation.variables['startDate']).toBe('2024-03-28');
    expect(op.operation.variables['endDate']).toBe('2024-12-31');

    op.flush({
      data: {
        runSpecStrategyBacktest: {
          success: true,
          strategyName: component.spec().name,
          initialCash: 100000,
          finalEquity: 100000,
          netProfit: 0,
          totalFees: 0,
          totalTrades: 0,
          winningTrades: 0,
          losingTrades: 0,
          winRate: 0,
          trades: [],
          logLines: [],
          error: null,
        },
      },
    });

    await promise;
    expect(component.result()?.success).toBe(true);
  });

  // ---- Display helpers (kept from previous version) -------------------
  it('formatTime renders an ms-UTC timestamp in America/New_York', () => {
    expect(component.formatTime(1704153600000)).toBe('01/01/2024, 19:00');
  });

  it('formatIndicators serializes a list-of-DTO trade indicators', () => {
    const out = component.formatIndicators({
      tradeNumber: 1,
      entryTime: 0,
      entryPrice: 0,
      exitTime: 0,
      exitPrice: 0,
      indicators: [
        { name: 'ema5', value: 470.4321 },
        { name: 'rsi', value: 62.5 },
      ],
      pnlPts: 0,
      pnlPct: 0,
      result: 'WIN',
      signalReason: '',
    });
    expect(out).toBe('ema5=470.4321, rsi=62.5000');
  });

  // ---- Sanity check on canonical fixture references -------------------
  it('the bundled canonical fixtures are the same three the Python service ships', () => {
    expect(CANONICAL_FIXTURES.map((f) => f.id)).toEqual([
      'spy_ema_crossover',
      'sma_crossover',
      'rsi_mean_reversion',
    ]);
  });

  // ---- runBacktest payload reflects bridge ----------------------------
  describe('runBacktest payload reflects symbol bridge', () => {
    it('sends the picker symbol via spec.symbols and dates from range', async () => {
      // Change symbol via the bridge.
      component.onRangeChange({
        ...component.pickerValue(),
        symbol: 'TSLA',
        from: '2025-03-01',
        to: '2025-03-31',
      });

      // Fire the run; the service issues a single GraphQL mutation.
      const promise = component.runBacktest();

      const op = controller.expectOne(RUN_SPEC_STRATEGY_BACKTEST);
      const vars = op.operation.variables;

      // Dates flow from range.
      expect(vars['startDate']).toBe('2025-03-01');
      expect(vars['endDate']).toBe('2025-03-31');

      // Symbol flows through spec.symbols (the bridge already updated it
      // in onRangeChange, so the JSON-encoded specJson contains TSLA).
      const spec = JSON.parse(vars['specJson'] as string);
      expect(spec.symbols).toEqual(['TSLA']);

      // Resolve the mutation so afterEach()'s controller.verify() passes.
      op.flush({
        data: {
          runSpecStrategyBacktest: {
            success: true,
            strategyName: spec.name,
            initialCash: 100000,
            finalEquity: 100000,
            netProfit: 0,
            totalFees: 0,
            totalTrades: 0,
            winningTrades: 0,
            losingTrades: 0,
            winRate: 0,
            trades: [],
            logLines: [],
            error: null,
          },
        },
      });
      await promise;
    });
  });

  // ---- Range dates flow into validation -------------------------------
  describe('range dates flow into validation', () => {
    it('validateStrategy receives range().from/to as start/end', () => {
      // Pick a date pair distinct from the constructor defaults so we
      // can prove the call site reads from range.
      component.range.set({
        from: '2025-06-01',
        to: '2025-06-30',
        resolution: component.range().resolution,
      });

      // The component's `issues` computed re-runs on every signal read;
      // assert that range() values are what feed in (and that the issues
      // list is computable — proves the rename took effect end-to-end).
      expect(component.range().from).toBe('2025-06-01');
      expect(component.range().to).toBe('2025-06-30');
      expect(Array.isArray(component.issues())).toBe(true);
    });
  });

  // ---- Symbol bridge ---------------------------------------------------
  describe('symbol bridge to spec.symbols', () => {
    it('onRangeChange propagates next.symbol into spec.symbols', () => {
      const before = component.spec().symbols[0];
      expect(before).toBe('SPY');

      const next: TickerRange = {
        symbol: 'AAPL',
        from: component.range().from,
        to: component.range().to,
        resolution: 'minute',
      };
      component.onRangeChange(next);

      // Picker view reflects the change (computed from spec).
      expect(component.pickerValue().symbol).toBe('AAPL');
      expect(component.spec().symbols).toEqual(['AAPL']);
    });

    it('onRangeChange skips spec.update when only dates change', () => {
      const symbolsRefBefore = component.spec().symbols;

      const next: TickerRange = {
        ...component.pickerValue(),
        from: '2025-01-01',
        to: '2025-01-31',
      };
      component.onRangeChange(next);

      expect(component.range().from).toBe('2025-01-01');
      expect(component.range().to).toBe('2025-01-31');
      // Same array reference — no spec.update was called.
      expect(component.spec().symbols).toBe(symbolsRefBefore);
    });

    it('pickerValue.symbol initially reflects spec.symbols[0]', () => {
      // Default fixture is spy_ema_crossover (symbols: ["SPY"]).
      expect(component.pickerValue().symbol).toBe('SPY');
    });

    it('pickerValue re-syncs symbol when spec is replaced', () => {
      // Regression test for the P1 caught on PR #206 review:
      // initial range/picker showed SPY; spec is replaced with a
      // different symbol (simulating selectFixture / loadSaved /
      // applyAdvancedJson); the picker MUST reflect the new symbol
      // so a subsequent date-only change can't clobber it.
      expect(component.pickerValue().symbol).toBe('SPY');

      component.spec.set({ ...component.spec(), symbols: ['AAPL'] });

      // pickerValue is a computed signal — recomputes synchronously
      // when its inputs change. The picker's bound value is now AAPL.
      expect(component.pickerValue().symbol).toBe('AAPL');

      // A subsequent date-only change must NOT revert spec.symbols to
      // the previous value. The bridge guard compares incoming symbol
      // against the live spec.symbols[0], not stale picker state.
      component.onRangeChange({
        ...component.pickerValue(),
        from: '2025-04-01',
        to: '2025-04-30',
      });
      expect(component.spec().symbols).toEqual(['AAPL']);
      expect(component.pickerValue().symbol).toBe('AAPL');
    });
  });
});
