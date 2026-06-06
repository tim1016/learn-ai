import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { InstanceProvenance } from '../../../api/live-instances.types';
import { BrokerProvenanceCardComponent } from './broker-provenance-card.component';

function makeProv(overrides: Partial<InstanceProvenance> = {}): InstanceProvenance {
  return {
    run_id: 'abcdef0123456789',
    schema_version: '1.2',
    code_sha: 'c0ffee1234deadbeef',
    strategy_spec_path:
      'PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json',
    strategy_spec_sha256: 'aaaaspecsha',
    qc_audit_copy_path: 'references/qc-shadow/SpyEmaCrossoverAlgorithm.py',
    qc_audit_copy_sha256: 'bbbbauditsha',
    qc_cloud_backtest_id: 'd2fe45a7142e88575f6fbd75229f8681',
    account_id: 'DU1234567',
    start_date_ms: 1714838400000,
    created_at_ms: 1714838400500,
    live_config: { symbol: 'SPY', consolidator_period_min: 15 },
    ...overrides,
  };
}

function render(prov: InstanceProvenance): HTMLElement {
  const fixture = TestBed.createComponent(BrokerProvenanceCardComponent);
  fixture.componentRef.setInput('provenance', prov);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerProvenanceCardComponent', () => {
  it('explains what the run identity proves in plain language', () => {
    const text = render(makeProv()).textContent ?? '';

    expect(text).toContain('Identity & Provenance');
    expect(text).toContain('abcdef012345'); // short run_id
    expect(text).toContain('Ran this committed code');
    expect(text).toContain('Byte-identical to backtest');
    expect(text).toContain('d2fe45a7142e88575f6fbd75229f8681');
    expect(text).toContain('SpyEmaCrossoverAlgorithm.py'); // audit-copy filename
    expect(text).toContain('DU1234567');
    // live_config is part of the identity hash — surface it as a proof row.
    expect(text).toContain('Runtime config');
    expect(text).toContain('symbol=SPY');
  });

  it('keeps the full fingerprints behind a disclosure', () => {
    const details = render(makeProv()).querySelector('details.fingerprints');

    expect(details).toBeTruthy();
    expect(details?.textContent).toContain('c0ffee1234deadbeef'); // full code sha
    expect(details?.textContent).toContain('aaaaspecsha');
  });

  it('omits proofs a legacy ledger did not record', () => {
    const text =
      render(
        makeProv({
          code_sha: '',
          qc_cloud_backtest_id: '',
          qc_audit_copy_sha256: '',
          qc_audit_copy_path: '',
        }),
      ).textContent ?? '';

    expect(text).not.toContain('Ran this committed code');
    expect(text).not.toContain('Byte-identical to backtest');
    expect(text).toContain('DU1234567'); // account proof still present
  });
});
