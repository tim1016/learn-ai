import { Component, inject, OnInit } from "@angular/core";
import { CommonModule } from "@angular/common";
import { BookService } from "../../services/book.service";
import { Book } from "../../graphql/types";

@Component({
  selector: "app-books",
  standalone: true,
  imports: [CommonModule],
  template: `
    <h1>Books</h1>

    @if (loading) {
      <div class="loading">Loading books...</div>
    }

    @if (error) {
      <div class="error">{{ error }}</div>
    }

    <div class="book-list">
      @for (book of books; track book.id) {
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

  books: Book[] = [];
  loading = true;
  error = "";

  ngOnInit() {
    this.bookService.getBooks().subscribe({
      next: (books) => {
        this.books = books;
        this.loading = false;
      },
      error: (err) => {
        this.error = "Failed to load books. Is the backend running?";
        this.loading = false;
        console.error(err);
      },
    });
  }
}
