import { inject } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { MessageService } from 'primeng/api';
import { environment } from '../../../environments/environment';
import { GraphqlError, GraphQLResponse } from './graphql-error';

export interface GqlPostOptions {
  errorContext?: string;
  suppressToast?: boolean;
}

export class GqlClient {
  private http = inject(HttpClient);
  private messageService = inject(MessageService);

  async post<TData, TVars extends Record<string, unknown> = Record<string, unknown>>(
    query: string,
    variables?: TVars,
    options: GqlPostOptions = {},
  ): Promise<TData> {
    const { errorContext, suppressToast } = options;
    try {
      const response = await firstValueFrom(
        this.http.post<GraphQLResponse<TData>>(environment.backendUrl, {
          query,
          variables: variables ?? {},
        }),
      );
      if (response.errors?.length) {
        const err = new GraphqlError(response.errors, errorContext);
        if (!suppressToast) this.toastError(err.message, errorContext);
        throw err;
      }
      return response.data;
    } catch (err) {
      if (err instanceof GraphqlError) throw err;
      const detail = err instanceof HttpErrorResponse
        ? `${err.status} ${err.statusText}`
        : err instanceof Error
          ? err.message
          : String(err);
      if (!suppressToast) this.toastError(detail, errorContext ?? 'Network error');
      throw err;
    }
  }

  private toastError(detail: string, summary?: string): void {
    this.messageService.add({
      severity: 'error',
      summary: summary ?? 'Server error',
      detail,
      life: 6000,
    });
  }
}
