import { Component, signal } from "@angular/core";
import { RouterOutlet } from "@angular/router";
import { Menubar } from "primeng/menubar";
import { MenuItem } from "primeng/api";

@Component({
  selector: "app-root",
  standalone: true,
  imports: [RouterOutlet, Menubar],
  styles: [`
    :host {
      display: block;
      min-height: 100vh;
      background: var(--bg-canvas);
      color: var(--text-primary);
    }

    .nav-shell {
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 50;
      display: flex;
      align-items: center;
      padding: 0 1.5rem;
      height: 52px;
      gap: 0.5rem;
    }

    .nav-brand {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-right: 1.5rem;
      text-decoration: none;
      flex-shrink: 0;
    }

    .nav-brand-wordmark {
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: -0.01em;
      color: var(--text-primary);
    }

    .nav-brand-wordmark .slash {
      color: var(--text-muted);
      font-weight: 500;
    }

    .nav-menubar {
      flex: 1;
      min-width: 0;
    }

    .nav-status {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text-secondary);
      padding: 5px 12px;
      border-radius: 6px;
      font-size: 12px;
      cursor: default;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      flex-shrink: 0;
    }

    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 9999px;
      background: var(--bull);
      flex-shrink: 0;
    }

    .page-container {
      max-width: 1200px;
      margin: 0 auto;
      padding: 1.5rem 1rem;
    }
  `],
  template: `
    <div class="nav-shell">
      <a class="nav-brand" href="/">
        <svg width="18" height="22" viewBox="0 0 22 26" aria-hidden="true">
          <rect x="9" y="0" width="4" height="26" fill="#5a6178"/>
          <rect x="4" y="5" width="14" height="14" fill="#00c896" rx="1"/>
        </svg>
        <span class="nav-brand-wordmark">quant<span class="slash">/</span>lab</span>
      </a>
      <div class="nav-menubar">
        <p-menubar [model]="items()" />
      </div>
      <div class="nav-status">
        <span class="status-dot"></span>
        polygon · live
      </div>
    </div>
    <div class="page-container">
      <router-outlet />
    </div>
  `,
})
export class AppComponent {
  items = signal<MenuItem[]>([
    {
      label: "Stocks",
      icon: "pi pi-chart-line",
      items: [
        { label: "Market Data", icon: "pi pi-chart-bar", routerLink: "/market-data" },
        { label: "Tickers", icon: "pi pi-list", routerLink: "/tickers" },
        { label: "Technical Analysis", icon: "pi pi-wave-pulse", routerLink: "/technical-analysis" },
        { label: "Stock Analysis", icon: "pi pi-search", routerLink: "/stock-analysis" },
        { label: "Snapshots", icon: "pi pi-camera", routerLink: "/snapshots" },
        { label: "Strategy Lab (deprecated)", icon: "pi pi-wrench", routerLink: "/strategy-lab" },
        { label: "Strategy Validation", icon: "pi pi-check-square", routerLink: "/strategy-lab-validation" },
        { label: "Strategy Docs", icon: "pi pi-book", routerLink: "/strategy-docs" },
        { label: "Indicator Validation", icon: "pi pi-verified", routerLink: "/indicator-validation" },
        { label: "Indicator Docs", icon: "pi pi-book", routerLink: "/indicator-docs" },
        { label: "Indicator Report", icon: "pi pi-chart-scatter", routerLink: "/indicator-report" },
        { label: "Data Lab", icon: "pi pi-database", routerLink: "/data-lab" },
        { label: "Data Lab Docs", icon: "pi pi-book", routerLink: "/data-lab-docs" },
      ],
    },
    {
      label: "Data Quality",
      icon: "pi pi-shield",
      items: [
        { label: "Quality Analysis", icon: "pi pi-check-circle", routerLink: "/data-quality" },
        { label: "Pipeline Docs", icon: "pi pi-book", routerLink: "/data-quality-docs" },
      ],
    },
    {
      label: "Options",
      icon: "pi pi-objects-column",
      items: [
        { label: "Options Chain", icon: "pi pi-table", routerLink: "/options-chain" },
        { label: "Strategy Builder", icon: "pi pi-th-large", routerLink: "/strategy-builder" },
        { label: "Options Strategy Lab", icon: "pi pi-calculator", routerLink: "/options-strategy-lab" },
        { label: "Options History", icon: "pi pi-history", routerLink: "/options-history" },
        { label: "Pricing Lab", icon: "pi pi-chart-bar", routerLink: "/pricing-lab" },
        { label: "Snapshots", icon: "pi pi-camera", routerLink: "/snapshots" },
      ],
    },
    {
      label: "Engine",
      icon: "pi pi-cog",
      items: [
        { label: "Engine Lab", icon: "pi pi-play", routerLink: "/engine" },
        { label: "Engine Docs", icon: "pi pi-book", routerLink: "/engine/docs" },
      ],
    },
    {
      label: "Portfolio",
      icon: "pi pi-wallet",
      routerLink: "/portfolio",
    },
    {
      label: "Research Lab",
      icon: "pi pi-search",
      routerLink: "/research-lab",
    },
    {
      label: "Tracked Instruments",
      icon: "pi pi-eye",
      routerLink: "/tracked-instruments",
    },
  ]);
}
