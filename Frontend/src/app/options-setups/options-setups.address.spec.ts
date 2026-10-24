import { describe, expect, it } from 'vitest';

import {
  isOptionsEvidenceKind,
  OPTIONS_ANALYSIS_VIEWS,
  OPTIONS_SETUP_TABS,
  optionsAnalysisRoute,
  optionsAnalysisViewOf,
  optionsEvidenceRoute,
  optionsSetupLocation,
  optionsSetupRoute,
  optionsSetupWindowTitle,
  UNSAVED_DRAFT_SEGMENT,
} from './options-setups.address';

/** A saved name must start with a letter or a digit (`saved_setups.py`), so the unsaved draft's segment can never be one. */
const NAME_START = /^[A-Za-z0-9]/;

describe('the Options setups address', () => {
  it('names a setup and its tab in two segments, and the unsaved draft by a segment no saved name can be', () => {
    expect(optionsSetupRoute('SPY 0DTE', 'define')).toEqual(['/options-setups', 'SPY 0DTE', 'define']);
    expect(optionsSetupRoute(null, 'overview')).toEqual(['/options-setups', UNSAVED_DRAFT_SEGMENT, 'overview']);
    expect(NAME_START.test(UNSAVED_DRAFT_SEGMENT)).toBe(false);
  });

  it.each([
    ['/options-setups/SPY%200DTE/define', { name: 'SPY 0DTE', tab: 'define' }],
    ['/options-setups/SPY,%20Buy%201%20ATM%20call%20%280945%29/overview?probe=1#frag', { name: 'SPY, Buy 1 ATM call (0945)', tab: 'overview' }],
    ['/options-setups/_new/define', { name: null, tab: 'define' }],
    // A setup that happens to be named like a page of the place is still a setup, because its address carries a tab.
    ['/options-setups/evidence/overview', { name: 'evidence', tab: 'overview' }],
    ['/options-setups/analysis/define', { name: 'analysis', tab: 'define' }],
  ])('reads %s as one setup and one tab', (url, location) => {
    expect(optionsSetupLocation(url)).toEqual(location);
  });

  it.each([
    '/options-setups',
    '/options-setups/SPY%200DTE',
    '/options-setups/SPY%200DTE/nonsense',
    '/options-setups/evidence/run/76',
    '/options-setups/SPY%200DTE/define/extra',
    '/markets/SPY/chart',
  ])('reads %s as no setup tab', (url) => {
    expect(optionsSetupLocation(url)).toBeNull();
  });

  it('keeps a name that is not valid percent-encoding as it is written', () => {
    expect(optionsSetupLocation('/options-setups/100%/define')).toEqual({ name: '100%', tab: 'define' });
  });

  it('declares the tabs once, in the order the owner works through them', () => {
    expect(OPTIONS_SETUP_TABS.tabs.map((tab) => tab.label)).toEqual(['Overview', 'Define', 'Explore', 'Golden']);
  });

  it('titles a setup\'s window with the setup, its tab and the group, and the unsaved draft as new', () => {
    expect(optionsSetupWindowTitle('/options-setups/SPY%200DTE/define')).toBe('SPY 0DTE · Define · Options setups');
    expect(optionsSetupWindowTitle('/options-setups/_new/overview')).toBe('New setup · Overview · Options setups');
    expect(optionsSetupWindowTitle('/options-setups')).toBeNull();
  });

  it('addresses the analytical views beside the setups, and reads a setup named analysis as a setup', () => {
    expect(optionsAnalysisRoute('builder')).toEqual(['/options-setups', 'analysis']);
    expect(optionsAnalysisRoute('finder')).toEqual(['/options-setups', 'analysis', 'finder']);
    expect(OPTIONS_ANALYSIS_VIEWS.tabs.map((tab) => tab.label)).toEqual(['Strategy Builder', 'Strategy Finder (not available)']);
    expect(optionsAnalysisViewOf('/options-setups/analysis?probe=1#legs')).toBe('builder');
    expect(optionsAnalysisViewOf('/options-setups/analysis/finder')).toBe('finder');
    expect(optionsAnalysisViewOf('/options-setups/analysis/define')).toBeNull();
    expect(optionsAnalysisViewOf('/options-setups/SPY%200DTE/define')).toBeNull();
    expect(optionsSetupLocation('/options-setups/analysis')).toBeNull();
    expect(optionsSetupLocation('/options-setups/analysis/finder')).toBeNull();
  });

  it('addresses evidence by what it froze, and knows only the three kinds', () => {
    expect(optionsEvidenceRoute('run', 76)).toEqual(['/options-setups', 'evidence', 'run', '76']);
    expect(optionsEvidenceRoute('golden', 'ogs_abc')).toEqual(['/options-setups', 'evidence', 'golden', 'ogs_abc']);
    expect(['run', 'study', 'golden'].every(isOptionsEvidenceKind)).toBe(true);
    expect(isOptionsEvidenceKind('setup')).toBe(false);
  });
});
