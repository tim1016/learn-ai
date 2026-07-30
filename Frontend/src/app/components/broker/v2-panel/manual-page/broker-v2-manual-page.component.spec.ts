import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterModule } from '@angular/router';
import { describe, expect, it, beforeEach } from 'vitest';
import { BrokerV2ManualPageComponent } from './broker-v2-manual-page.component';

function setup() {
  TestBed.configureTestingModule({
    imports: [BrokerV2ManualPageComponent, RouterModule.forRoot([])],
  });
  const fixture = TestBed.createComponent(BrokerV2ManualPageComponent);
  fixture.detectChanges();
  return { fixture };
}

describe('BrokerV2ManualPageComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  it('renders the page', () => {
    const { fixture } = setup();
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('renders the manual heading in the page header', () => {
    const { fixture } = setup();
    const header = fixture.nativeElement.querySelector('app-page-header') as HTMLElement | null;
    expect(header).toBeTruthy();
  });

  it('includes a markdown viewer targeting the operator manual', () => {
    const { fixture } = setup();
    const viewer = fixture.nativeElement.querySelector('app-markdown-viewer') as HTMLElement | null;
    expect(viewer).toBeTruthy();
  });
});
