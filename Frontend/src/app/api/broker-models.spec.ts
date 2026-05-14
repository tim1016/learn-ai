import { describe, expect, it } from 'vitest';
import type {
  DiagnosticReport,
  DiagnosticReportActive,
  DiagnosticReportDisabled,
} from './broker-models';

/**
 * Runtime verification that the DiagnosticReport discriminated union
 * narrows correctly on the ``disabled`` boolean discriminant.
 *
 * These tests exist because the union was hand-mirrored from Python's
 * Pydantic model (see broker-models.ts note about broker.types.ts
 * regeneration). If the discriminant field or its literal types drift,
 * the runtime narrowing breaks silently — these tests catch that.
 */
describe('DiagnosticReport discriminated union', () => {
  it('disabled=false branch carries overall_status and checks', () => {
    const report: DiagnosticReport = {
      disabled: false,
      overall_status: 'pass',
      checks: [{ name: 'c1', label: 'Check 1', status: 'pass', detail: 'ok', fix: null }],
      fetched_at_ms: 1_700_000_000_000,
    };

    if (report.disabled === false) {
      const active: DiagnosticReportActive = report;
      expect(active.overall_status).toBe('pass');
      expect(active.checks).toHaveLength(1);
      expect(active.fetched_at_ms).toBe(1_700_000_000_000);
    } else {
      throw new Error('should have taken the active branch');
    }
  });

  it('disabled=true branch carries reason and since_ms', () => {
    const report: DiagnosticReport = {
      disabled: true,
      reason: 'host runner owns IBKR',
      since_ms: 1_700_000_000_000,
    };

    if (report.disabled === true) {
      const disabled: DiagnosticReportDisabled = report;
      expect(disabled.reason).toBe('host runner owns IBKR');
      expect(disabled.since_ms).toBe(1_700_000_000_000);
    } else {
      throw new Error('should have taken the disabled branch');
    }
  });

  it('disabled branch does not carry overall_status', () => {
    const report: DiagnosticReport = {
      disabled: true,
      reason: 'host runner active',
      since_ms: 0,
    };
    expect('overall_status' in report).toBe(false);
    expect('checks' in report).toBe(false);
  });

  it('active branch does not carry reason or since_ms', () => {
    const report: DiagnosticReport = {
      disabled: false,
      overall_status: 'warn',
      checks: [],
      fetched_at_ms: 0,
    };
    expect('reason' in report).toBe(false);
    expect('since_ms' in report).toBe(false);
  });

  it('switch on disabled exhausts both branches', () => {
    const reports: DiagnosticReport[] = [
      { disabled: false, overall_status: 'fail', checks: [], fetched_at_ms: 0 },
      { disabled: true, reason: 'x', since_ms: 0 },
    ];

    const results: string[] = [];
    for (const r of reports) {
      switch (r.disabled) {
        case false:
          results.push(`active:${r.overall_status}`);
          break;
        case true:
          results.push(`disabled:${r.reason}`);
          break;
      }
    }

    expect(results).toEqual(['active:fail', 'disabled:x']);
  });
});
