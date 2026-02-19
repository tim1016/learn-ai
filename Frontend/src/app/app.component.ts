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
    <div class="container">
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
      ],
    },
    {
      label: "Options",
      icon: "pi pi-objects-column",
      items: [
        { label: "Options Chain", icon: "pi pi-table", routerLink: "/options-chain" },
        { label: "Strategy Lab", icon: "pi pi-wrench", routerLink: "/strategy-lab" },
        { label: "Options History", icon: "pi pi-history", routerLink: "/options-history" },
        { label: "Snapshots", icon: "pi pi-camera", routerLink: "/snapshots" },
      ],
    },
    {
      label: "Tracked Instruments",
      icon: "pi pi-eye",
      routerLink: "/tracked-instruments",
    },
  ]);
}
