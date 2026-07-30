import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterModule } from '@angular/router';
import { describe, expect, it, beforeEach } from 'vitest';
import { BrokerV2HelpDrawerComponent } from './broker-v2-help-drawer.component';
import { BrokerV2HelpDrawerService } from './broker-v2-help-drawer.service';

function setup() {
  const svc = new BrokerV2HelpDrawerService();
  TestBed.configureTestingModule({
    imports: [BrokerV2HelpDrawerComponent, RouterModule.forRoot([])],
    providers: [{ provide: BrokerV2HelpDrawerService, useValue: svc }],
  });
  const fixture = TestBed.createComponent(BrokerV2HelpDrawerComponent);
  fixture.detectChanges();
  return { fixture, svc };
}

describe('BrokerV2HelpDrawerComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  it('drawer is not visible initially', () => {
    const { svc } = setup();
    expect(svc.visible()).toBe(false);
  });

  it('after service.open with anchor, drawer becomes visible', () => {
    const { fixture, svc } = setup();

    svc.open('station-1-signal');
    fixture.detectChanges();

    expect(svc.visible()).toBe(true);
    expect(svc.anchor()).toBe('station-1-signal');
  });

  it('close button hides the drawer', () => {
    const { fixture, svc } = setup();

    svc.open('station-2-intent');
    fixture.detectChanges();

    const closeButton = fixture.nativeElement.querySelector(
      'button[aria-label="Close operator manual drawer"]',
    ) as HTMLButtonElement | null;
    closeButton?.click();
    fixture.detectChanges();

    expect(svc.visible()).toBe(false);
  });

  it('the component does not navigate away when the drawer is open', () => {
    const { fixture, svc } = setup();

    const initialUrl = window.location.href;
    svc.open('glossary');
    fixture.detectChanges();

    // Opening the drawer should not change the URL
    expect(window.location.href).toBe(initialUrl);
  });
});
