import { ChangeDetectionStrategy, Component, inject, OnInit, signal } from "@angular/core";
import { CommonModule } from "@angular/common";
import { BookService } from "../../services/book.service";
import { Book } from "../../graphql/types";
import { PageHeaderComponent } from "../../shared/page-header/page-header.component";

@Component({
  selector: "app-books",
  imports: [CommonModule, PageHeaderComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-page-header title="Books" />

    @if (loading()) {
      <div class="loading">Loading books...</div>
    }

    @if (error()) {
      <div class="error">{{ error() }}</div>
    }

    <div class="book-list">
      @for (book of books(); track book.id) {
        <div class="card">
          <h3>{{ book.title }}</h3>
          <p>Published: {{ book.publishedYear }}</p>
          @if (book.author) {
            <p>Author: {{ book.author.name }}</p>
          }
        </div>
      }
    </div>
  `,
})
export class BooksComponent implements OnInit {
  private bookService = inject(BookService);

  books = signal<Book[]>([]);
  loading = signal(true);
  error = signal("");

  ngOnInit() {
    this.bookService.getBooks().subscribe({
      next: (books) => {
        this.books.set(books);
        this.loading.set(false);
      },
      error: () => {
        this.error.set("Failed to load books. Is the backend running?");
        this.loading.set(false);
      },
    });
  }
}
