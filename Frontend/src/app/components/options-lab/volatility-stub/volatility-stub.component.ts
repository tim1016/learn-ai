import { ChangeDetectionStrategy, Component } from '@angular/core';

@Component({
  selector: 'app-volatility-stub',
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
      background: rgba(41, 98, 255, 0.15);
      color: #2962ff;
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
    .tracker {
      margin-top: 12px;
      font-size: 11px;
      color: #6b7077;
    }
    .tracker a {
      color: #6b7077;
      text-decoration: underline;
    }
  `],
  template: `
    <div class="panel">
      <span class="badge">COMING SOON</span>
      <h2>Volatility</h2>
      <p>
        Implied-volatility surface and term-structure visualizations across
        strikes and expirations. View shape and exact metrics still TBD.
      </p>
      <p class="tracker">
        Tracked in
        <a
          href="https://github.com/tim1016/learn-ai/issues?q=is%3Aissue+volatility-stub"
          target="_blank"
          rel="noopener noreferrer"
          >GitHub issues (label: volatility-stub)</a
        >. VCR-P3-N.
      </p>
    </div>
  `,
})
export class VolatilityStubComponent {}
