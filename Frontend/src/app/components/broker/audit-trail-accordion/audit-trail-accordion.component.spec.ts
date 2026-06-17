import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import type { InstanceProvenance } from '../../../api/live-instances.types';
import { AuditTrailAccordionComponent } from './audit-trail-accordion.component';

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

function render(prov: InstanceProvenance): ComponentFixture<AuditTrailAccordionComponent> {
  TestBed.configureTestingModule({
    imports: [NoopAnimationsModule],
  });
  const fixture = TestBed.createComponent(AuditTrailAccordionComponent);
  fixture.componentRef.setInput('provenance', prov);
  fixture.detectChanges();
  return fixture;
}

function expand(fixture: ComponentFixture<AuditTrailAccordionComponent>): void {
  const header = (fixture.nativeElement as HTMLElement).querySelector<HTMLElement>(
    '[data-pc-section="header"], p-accordion-header, .p-accordionheader',
  );
  if (!header) throw new Error('accordion header not found');
  header.click();
  fixture.detectChanges();
}

afterEach(() => TestBed.resetTestingModule());

describe('AuditTrailAccordionComponent', () => {
  it('renders the Audit & Diagnostics heading and is collapsed by default', () => {
    const fixture = render(makeProv());
    const el = fixture.nativeElement as HTMLElement;

    expect(el.textContent).toContain('Audit & Diagnostics');
    // The accordion is closed by default — no panel carries the active class.
    expect(el.querySelector('.p-accordionpanel-active')).toBeNull();
  });

  it('reveals plain-language proofs once expanded', () => {
    const fixture = render(makeProv());
    expand(fixture);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';

    expect(text).toContain('abcdef012345'); // short run_id
    expect(text).toContain('Ran this committed code');
    // VCR-0014 / Phase 7D — the QC provenance row is split into two
    // honest proofs. The audit copy is a verifiable SHA; the QC Cloud
    // backtest id is operator-recorded.
    expect(text).toContain('Audit copy');
    expect(text).toContain('SpyEmaCrossoverAlgorithm.py');
    expect(text).toContain('QC Cloud backtest');
    expect(text).toContain('d2fe45a7142e88575f6fbd75229f8681');
    expect(text).toContain('Operator-recorded, not auto-verified');
    expect(text).toContain('DU1234567');
    expect(text).toContain('Runtime config');
    expect(text).toContain('symbol=SPY');
  });

  it('never renders the forbidden VCR-0014 strings, even when expanded', () => {
    const fixture = render(makeProv());
    expand(fixture);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';

    // PRD §7D forbids these labels until a real QC Cloud API verification
    // path exists. The card was the original source of "QC-approved" /
    // "Byte-identical to backtest" — Phase 7D removes both.
    expect(text).not.toContain('QC-approved');
    expect(text).not.toContain('Byte-identical to backtest');
    expect(text).not.toContain('verified backtest');
  });

  it('keeps the full fingerprints behind a nested disclosure', () => {
    const fixture = render(makeProv());
    expand(fixture);
    const details = (fixture.nativeElement as HTMLElement).querySelector('details.fingerprints');

    expect(details).toBeTruthy();
    expect(details?.textContent).toContain('c0ffee1234deadbeef'); // full code sha
    expect(details?.textContent).toContain('aaaaspecsha');
  });

  it('omits proofs a legacy ledger did not record', () => {
    const fixture = render(
      makeProv({
        code_sha: '',
        qc_cloud_backtest_id: '',
        qc_audit_copy_sha256: '',
        qc_audit_copy_path: '',
      }),
    );
    expand(fixture);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';

    expect(text).not.toContain('Ran this committed code');
    expect(text).not.toContain('Audit copy');
    expect(text).not.toContain('QC Cloud backtest');
    expect(text).toContain('DU1234567'); // account proof still present
  });
});
