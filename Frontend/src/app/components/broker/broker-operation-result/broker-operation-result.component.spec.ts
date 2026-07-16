import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { BrokerOperationResultComponent } from './broker-operation-result.component';
import type { OperationError } from '../operation-error';

const ERR: OperationError = {
  category: 'precondition',
  title: 'Deploy — blocked',
  detail: 'Working tree is dirty at PythonDataService',
  remediation: 'Commit or stash the listed paths, then deploy again.',
  status: 409,
};

function render(error: OperationError | null) {
  const fixture = TestBed.createComponent(BrokerOperationResultComponent);
  fixture.componentRef.setInput('error', error);
  fixture.detectChanges();
  return fixture;
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerOperationResultComponent', () => {
  it('renders title, detail and remediation as an alert', () => {
    const fixture = render(ERR);
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector('[role="alert"]')).toBeTruthy();
    expect(el.textContent).toContain('Deploy — blocked');
    expect(el.textContent).toContain('Working tree is dirty');
    expect(el.textContent).toContain('Commit or stash');
  });

  it('exposes the complete server diagnostic in the alert', () => {
    const fixture = render({
      ...ERR,
      detail: 'deiagAPPL6 is durably STOPPED. Resume the bot to clear the stop latch.',
      status: 503,
      reason_code: 'FLEET_CONTAMINATION_UNAVAILABLE',
      gate_id: 'fleet.contamination',
    });
    const alert = (fixture.nativeElement as HTMLElement).querySelector<HTMLElement>('[role="alert"]');

    expect(alert?.textContent).toContain('Deploy — blocked');
    expect(alert?.textContent).toContain('HTTP 503');
    expect(alert?.textContent).toContain('Fleet Contamination Unavailable');
    expect(alert?.textContent).toContain('Fleet Contamination');
    expect(alert?.textContent).toContain('deiagAPPL6 is durably STOPPED');
    expect(alert?.querySelector('[aria-hidden="true"]')).toBeNull();
  });

  it('renders nothing when error is null', () => {
    const fixture = render(null);
    expect((fixture.nativeElement as HTMLElement).querySelector('.op-result')).toBeNull();
  });

  it('omits the detail line when detail is empty', () => {
    const fixture = render({ ...ERR, detail: '' });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.op-detail')).toBeNull();
    expect(el.textContent).toContain('Commit or stash');
  });

  it('renders typed server reason and gate labels beside the next step', () => {
    const fixture = render({
      ...ERR,
      reason_code: 'DEPLOY_PREFLIGHT_BLOCKED',
      gate_id: 'daily_lifecycle.effective_stop',
    });
    const el = fixture.nativeElement as HTMLElement;

    expect(el.textContent).toContain('Server reason');
    expect(el.textContent).toContain('Deploy Preflight Blocked');
    expect(el.textContent).toContain('Rejected at');
    expect(el.textContent).toContain('Daily Lifecycle Effective Stop');
    expect(el.textContent).toContain('Next:');
  });
});
