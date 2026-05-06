import { ChangeDetectionStrategy, Component } from '@angular/core';

@Component({
  selector: 'app-strategy-finder-stub',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: [`
    :host {
      display: flex;
      flex: 1 1 auto;
      align-items: center;
      justify-content: center;
      padding: 48px 24px;
      color: #9aa0a6;
    }
    .panel {
      max-width: 520px;
      text-align: center;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      margin-bottom: 12px;
      border-radius: 3px;
      background: rgba(245, 158, 11, 0.15);
      color: #f59e0b;
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.5px;
      font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
    }
    h2 {
      margin: 0 0 8px;
      color: #e7e9ec;
      font-size: 18px;
      font-weight: 600;
    }
    p {
      margin: 0;
      font-size: 13px;
      line-height: 1.6;
    }
  `],
  template: `
    <div class="panel">
      <span class="badge">BETA · COMING SOON</span>
      <h2>Strategy Finder</h2>
      <p>
        A scanner that ranks pre-built strategies (spreads, condors, butterflies)
        by reward/risk, breakeven distance, and expected payoff across a price
        range. Design and ranking criteria still TBD.
      </p>
    </div>
  `,
})
export class StrategyFinderStubComponent {}
