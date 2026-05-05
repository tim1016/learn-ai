import { ChangeDetectionStrategy, Component, inject, OnInit, signal } from "@angular/core";
import { CommonModule } from "@angular/common";
import { AuthorService } from "../../services/author.service";
import { Author } from "../../graphql/types";
import { PageHeaderComponent } from "../../shared/page-header/page-header.component";

@Component({
  selector: "app-authors",
  imports: [CommonModule, PageHeaderComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-page-header title="Authors" />

    @if (loading()) {
      <div class="loading">Loading authors...</div>
    }

    @if (error()) {
      <div class="error">{{ error() }}</div>
    }

    <div class="author-list">
      @for (author of authors(); track author.id) {
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

  authors = signal<Author[]>([]);
  loading = signal(true);
  error = signal("");

  ngOnInit() {
    this.authorService.getAuthors().subscribe({
      next: (authors) => {
        this.authors.set(authors);
        this.loading.set(false);
      },
      error: () => {
        this.error.set("Failed to load authors. Is the backend running?");
        this.loading.set(false);
      },
    });
  }
}
