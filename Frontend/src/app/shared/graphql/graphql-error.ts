export interface GraphQLErrorPayload {
  message: string;
  path?: (string | number)[];
  extensions?: Record<string, unknown>;
}

export interface GraphQLResponse<TData> {
  data: TData;
  errors?: GraphQLErrorPayload[];
}

export class GraphqlError extends Error {
  readonly errors: GraphQLErrorPayload[];
  readonly context?: string;

  constructor(errors: GraphQLErrorPayload[], context?: string) {
    const summary = errors.map(e => e.message).join('; ');
    super(summary);
    this.name = 'GraphqlError';
    this.errors = errors;
    this.context = context;
  }
}
