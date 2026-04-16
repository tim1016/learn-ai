import { Component, signal } from "@angular/core";
import { RouterOutlet } from "@angular/router";
import { Menubar } from "primeng/menubar";
import { MenuItem } from "primeng/api";

@Component({
  selector: "app-root",
  standalone: true,
  imports: [RouterOutlet, Menubar],
  template: `
    <p-menubar [model]="items()" />
    <div class="px-4">
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
