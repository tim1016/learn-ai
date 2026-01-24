import { Injectable, inject } from "@angular/core";
import { Apollo } from "apollo-angular";
import { map } from "rxjs/operators";
import { GET_BOOKS } from "../graphql/queries";
import { GetBooksResponse, Book } from "../graphql/types";

@Injectable({
  providedIn: "root",
})
export class BookService {
  private apollo = inject(Apollo);

  getBooks() {
    return this.apollo
      .watchQuery<GetBooksResponse>({
        query: GET_BOOKS,
      })
      .valueChanges.pipe(map((result) => (result.data?.books ?? []) as Book[]));
  }
}
