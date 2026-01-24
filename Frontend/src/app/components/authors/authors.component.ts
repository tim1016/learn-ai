import { Component, inject, OnInit } from "@angular/core";
import { CommonModule } from "@angular/common";
import { AuthorService } from "../../services/author.service";
import { Author } from "../../graphql/types";

@Component({
  selector: "app-authors",
  standalone: true,
  imports: [CommonModule],
  template: `
    <h1>Authors</h1>

    @if (loading) {
      <div class="loading">Loading authors...</div>
    }

    @if (error) {
      <div class="error">{{ error }}</div>
    }

    <div class="author-list">
      @for (author of authors; track author.id) {
        <div class="card">
          <h3>{{ author.name }}</h3>
          @if (author.bio) {
            <p>{{ author.bio }}</p>
          }
          @if (author.books && author.books.length > 0) {
            <p><strong>Books:</strong></p>
            <div>
              @for (book of author.books; track book.id) {
                <span class="tag">{{ book.title }} ({{ book.publishedYear }})</span>
              }
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class AuthorsComponent implements OnInit {
  private authorService = inject(AuthorService);

  authors: Author[] = [];
  loading = true;
  error = "";

  ngOnInit() {
    this.authorService.getAuthors().subscribe({
      next: (authors) => {
        this.authors = authors;
        this.loading = false;
      },
      error: (err) => {
        this.error = "Failed to load authors. Is the backend running?";
        this.loading = false;
        console.error(err);
      },
    });
  }
}
