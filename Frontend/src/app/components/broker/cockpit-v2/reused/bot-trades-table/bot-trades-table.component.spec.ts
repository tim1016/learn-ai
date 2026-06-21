import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { BotTradesTableComponent } from './bot-trades-table.component';

afterEach(() => TestBed.resetTestingModule());

describe('BotTradesTableComponent', () => {
  it('renders the "Recent Trades" surface label', () => {
    // #565 PR 5 — the surface label moves from "Trade History" to
    // "Recent Trades" to match the operator-vocabulary refactor. The
    // component itself is reused; only the heading copy changes.
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(BotTradesTableComponent);
    fixture.componentRef.setInput('runId', null);
    fixture.detectChanges();

    const heading = (fixture.nativeElement as HTMLElement).querySelector(
      '#bot-trades-heading',
    );
    expect(heading?.textContent?.trim()).toBe('Recent Trades');
  });
});
