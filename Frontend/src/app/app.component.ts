import { Component } from "@angular/core";
import { RouterLink, RouterOutlet } from "@angular/router";

@Component({
  selector: "app-root",
  standalone: true,
  imports: [RouterOutlet, RouterLink],
  template: `
    <nav>
      <a routerLink="/books">Books</a>
      <a routerLink="/authors">Authors</a>
      <a routerLink="/market-data">Market Data</a>
      <a routerLink="/tickers">Tickers</a>
      <a routerLink="/technical-analysis">Technical Analysis</a>
      <a routerLink="/stock-analysis">Stock Analysis</a>
      <a routerLink="/ticker-explorer">Ticker Explorer</a>
      <a routerLink="/strategy-lab">Strategy Lab</a>
      <a routerLink="/options-history">Options History</a>
    </nav>
    <div class="container">
      <router-outlet />
    </div>
  `,
})
export class AppComponent {}
