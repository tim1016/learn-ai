import { effect, inject } from '@angular/core';
import { httpResource, HttpResourceRef } from '@angular/common/http';
import { MessageService } from 'primeng/api';
import { environment } from '../../../environments/environment';
import { GraphqlError, GraphQLResponse } from './graphql-error';

export interface GqlResourceOptions {
  errorContext?: string;
  suppressToast?: boolean;
}

/**
 * Signal-driven GraphQL resource. Wraps Angular's first-party `httpResource()`
 * so consumers get { value, status, error, isLoading, reload } as signals.
 *
 * The `params` getter is reactive — when it returns `undefined` the request is
 * skipped (idle), when it returns a value the resource fetches. Server errors
 * (`response.errors[]`) are surfaced as `GraphqlError` on `ref.error()` and
 * toasted via PrimeNG MessageService unless `suppressToast` is set.
 *
 * Must be called from an injection context.
 */
export function gqlResource<TData, TVars extends Record<string, unknown> = Record<string, unknown>>(
  query: string,
  params: () => TVars | undefined,
  options: GqlResourceOptions = {},
): HttpResourceRef<TData | undefined> {
  const messageService = inject(MessageService);
  const { errorContext, suppressToast } = options;

  const ref = httpResource<TData>(
    () => {
      const variables = params();
      if (variables === undefined) return undefined;
      return {
        url: environment.backendUrl,
        method: 'POST',
        body: { query, variables },
      };
    },
    {
      parse: (raw: unknown): TData => {
        const response = raw as GraphQLResponse<TData>;
        if (response.errors?.length) {
          throw new GraphqlError(response.errors, errorContext);
        }
        return response.data;
      },
    },
  );

  effect(() => {
    const err = ref.error();
    if (!err || suppressToast) return;
    const detail = err instanceof GraphqlError
      ? err.message
      : err instanceof Error
        ? err.message
        : String(err);
    messageService.add({
      severity: 'error',
      summary: errorContext ?? (err instanceof GraphqlError ? 'Server error' : 'Network error'),
      detail,
      life: 6000,
    });
  });

  return ref;
}
