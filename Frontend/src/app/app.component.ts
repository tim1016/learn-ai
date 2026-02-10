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
    </nav>
    <div class="container">
      <router-outlet />
    </div>
  `,
})
export class AppComponent {}
