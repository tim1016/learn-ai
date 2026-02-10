import {
  HttpClient,
  HttpContext,
  HttpHeaders
} from "./chunk-PUHCMRBG.js";
import "./chunk-7MP6VLV4.js";
import {
  Injectable,
  setClassMetadata,
  ɵɵdefineInjectable,
  ɵɵinject
} from "./chunk-QLSWU3WF.js";
import "./chunk-AX673Q27.js";
import {
  ApolloLink,
  print
} from "./chunk-4NH6SLHF.js";
import "./chunk-27TCQNGU.js";
import {
  EMPTY,
  Observable
} from "./chunk-M3M7EYZM.js";
import "./chunk-HSWANC32.js";
import {
  __spreadProps,
  __spreadValues
} from "./chunk-4BXWKWGJ.js";

// node_modules/@apollo/client/link/batch/batching.js
var OperationBatcher = class {
  // Queue on which the QueryBatcher will operate on a per-tick basis.
  batchesByKey = /* @__PURE__ */ new Map();
  scheduledBatchTimerByKey = /* @__PURE__ */ new Map();
  batchDebounce;
  batchInterval;
  batchMax;
  //This function is called to the queries in the queue to the server.
  batchHandler;
  batchKey;
  constructor({ batchDebounce, batchInterval, batchMax, batchHandler, batchKey }) {
    this.batchDebounce = batchDebounce;
    this.batchInterval = batchInterval;
    this.batchMax = batchMax || 0;
    this.batchHandler = batchHandler;
    this.batchKey = batchKey || (() => "");
  }
  enqueueRequest(request) {
    const requestCopy = __spreadProps(__spreadValues({}, request), {
      next: [],
      error: [],
      complete: [],
      subscribers: /* @__PURE__ */ new Set()
    });
    const key = this.batchKey(request.operation);
    if (!requestCopy.observable) {
      requestCopy.observable = new Observable((observer) => {
        let batch = this.batchesByKey.get(key);
        if (!batch)
          this.batchesByKey.set(key, batch = /* @__PURE__ */ new Set());
        const isFirstEnqueuedRequest = batch.size === 0;
        const isFirstSubscriber = requestCopy.subscribers.size === 0;
        requestCopy.subscribers.add(observer);
        if (isFirstSubscriber) {
          batch.add(requestCopy);
        }
        if (observer.next) {
          requestCopy.next.push(observer.next.bind(observer));
        }
        if (observer.error) {
          requestCopy.error.push(observer.error.bind(observer));
        }
        if (observer.complete) {
          requestCopy.complete.push(observer.complete.bind(observer));
        }
        if (isFirstEnqueuedRequest || this.batchDebounce) {
          this.scheduleQueueConsumption(key);
        }
        if (batch.size === this.batchMax) {
          this.consumeQueue(key);
        }
        return () => {
          if (requestCopy.subscribers.delete(observer) && requestCopy.subscribers.size < 1) {
            if (batch.delete(requestCopy) && batch.size < 1) {
              this.consumeQueue(key);
              batch.subscription?.unsubscribe();
            }
          }
        };
      });
    }
    return requestCopy.observable;
  }
  // Consumes the queue.
  // Returns a list of promises (one for each query).
  consumeQueue(key = "") {
    const batch = this.batchesByKey.get(key);
    this.batchesByKey.delete(key);
    if (!batch || !batch.size) {
      return;
    }
    const operations = [];
    const forwards = [];
    const observables = [];
    const nexts = [];
    const errors = [];
    const completes = [];
    batch.forEach((request) => {
      operations.push(request.operation);
      forwards.push(request.forward);
      observables.push(request.observable);
      nexts.push(request.next);
      errors.push(request.error);
      completes.push(request.complete);
    });
    const batchedObservable = this.batchHandler(operations, forwards);
    const onError = (error) => {
      errors.forEach((rejecters) => {
        if (rejecters) {
          rejecters.forEach((e) => e(error));
        }
      });
    };
    batch.subscription = batchedObservable.subscribe({
      next: (results) => {
        if (!Array.isArray(results)) {
          results = [results];
        }
        if (nexts.length !== results.length) {
          const error = new Error(`server returned results with length ${results.length}, expected length of ${nexts.length}`);
          error.result = results;
          return onError(error);
        }
        results.forEach((result, index) => {
          if (nexts[index]) {
            nexts[index].forEach((next) => next(result));
          }
        });
      },
      error: onError,
      complete: () => {
        completes.forEach((complete) => {
          if (complete) {
            complete.forEach((c) => c());
          }
        });
      }
    });
    return observables;
  }
  scheduleQueueConsumption(key) {
    clearTimeout(this.scheduledBatchTimerByKey.get(key));
    this.scheduledBatchTimerByKey.set(key, setTimeout(() => {
      this.consumeQueue(key);
      this.scheduledBatchTimerByKey.delete(key);
    }, this.batchInterval));
  }
};

// node_modules/@apollo/client/link/batch/batchLink.js
var BatchLink = class extends ApolloLink {
  batcher;
  constructor(options) {
    super();
    const { batchDebounce, batchInterval = 10, batchMax = 0, batchHandler = () => EMPTY, batchKey = () => "" } = options || {};
    this.batcher = new OperationBatcher({
      batchDebounce,
      batchInterval,
      batchMax,
      batchHandler,
      batchKey
    });
  }
  request(operation, forward) {
    return this.batcher.enqueueRequest({ operation, forward });
  }
};

// node_modules/apollo-angular/fesm2022/apollo-angular-http.mjs
var fetch = (req, httpClient, extractFiles) => {
  const shouldUseBody = ["POST", "PUT", "PATCH"].indexOf(req.method.toUpperCase()) !== -1;
  const shouldStringify = (param) => ["variables", "extensions"].indexOf(param.toLowerCase()) !== -1;
  const isBatching = req.body.length;
  let shouldUseMultipart = req.options && req.options.useMultipart;
  let multipartInfo;
  if (shouldUseMultipart) {
    if (isBatching) {
      return new Observable((observer) => observer.error(new Error("File upload is not available when combined with Batching")));
    }
    if (!shouldUseBody) {
      return new Observable((observer) => observer.error(new Error("File upload is not available when GET is used")));
    }
    if (!extractFiles) {
      return new Observable((observer) => observer.error(new Error(`To use File upload you need to pass "extractFiles" function from "extract-files" library to HttpLink's options`)));
    }
    multipartInfo = extractFiles(req.body);
    shouldUseMultipart = !!multipartInfo.files.size;
  }
  let bodyOrParams = {};
  if (isBatching) {
    if (!shouldUseBody) {
      return new Observable((observer) => observer.error(new Error("Batching is not available for GET requests")));
    }
    bodyOrParams = {
      body: req.body
    };
  } else {
    const body = shouldUseMultipart ? multipartInfo.clone : req.body;
    if (shouldUseBody) {
      bodyOrParams = {
        body
      };
    } else {
      const params = Object.keys(req.body).reduce((obj, param) => {
        const value = req.body[param];
        obj[param] = shouldStringify(param) ? JSON.stringify(value) : value;
        return obj;
      }, {});
      bodyOrParams = {
        params
      };
    }
  }
  if (shouldUseMultipart && shouldUseBody) {
    const form = new FormData();
    form.append("operations", JSON.stringify(bodyOrParams.body));
    const map = {};
    const files = multipartInfo.files;
    let i = 0;
    files.forEach((paths) => {
      map[++i] = paths;
    });
    form.append("map", JSON.stringify(map));
    i = 0;
    files.forEach((_, file) => {
      form.append(++i + "", file, file.name);
    });
    bodyOrParams.body = form;
  }
  return httpClient.request(req.method, req.url, __spreadValues(__spreadValues({
    observe: "response",
    responseType: "json",
    reportProgress: false
  }, bodyOrParams), req.options));
};
var mergeHeaders = (source, destination) => {
  if (source && destination) {
    const merged = destination.keys().reduce((headers, name) => headers.set(name, destination.getAll(name)), source);
    return merged;
  }
  return destination || source;
};
var mergeHttpContext = (source, destination) => {
  if (source && destination) {
    return [...source.keys()].reduce((context, name) => context.set(name, source.get(name)), destination);
  }
  return destination || source;
};
function prioritize(...values) {
  return values.find((val) => typeof val !== "undefined");
}
function createHeadersWithClientAwareness(context) {
  let headers = context.headers && context.headers instanceof HttpHeaders ? context.headers : new HttpHeaders(context.headers);
  if (context.clientAwareness) {
    const {
      name,
      version
    } = context.clientAwareness;
    if (name && !headers.has("apollographql-client-name")) {
      headers = headers.set("apollographql-client-name", name);
    }
    if (version && !headers.has("apollographql-client-version")) {
      headers = headers.set("apollographql-client-version", version);
    }
  }
  return headers;
}
var defaults = {
  batchInterval: 10,
  batchMax: 10,
  uri: "graphql",
  method: "POST",
  withCredentials: false,
  includeQuery: true,
  includeExtensions: false,
  useMultipart: false
};
function pick(context, options, key) {
  return prioritize(context[key], options[key], defaults[key]);
}
var HttpBatchLinkHandler = class extends ApolloLink {
  httpClient;
  options;
  batcher;
  batchInterval;
  batchMax;
  print = print;
  constructor(httpClient, options) {
    super();
    this.httpClient = httpClient;
    this.options = options;
    this.batchInterval = options.batchInterval || defaults.batchInterval;
    this.batchMax = options.batchMax || defaults.batchMax;
    if (this.options.operationPrinter) {
      this.print = this.options.operationPrinter;
    }
    const batchHandler = (operations) => {
      return new Observable((observer) => {
        const body = this.createBody(operations);
        const headers = this.createHeaders(operations);
        const context = this.createHttpContext(operations);
        const {
          method,
          uri,
          withCredentials
        } = this.createOptions(operations);
        if (typeof uri === "function") {
          throw new Error(`Option 'uri' is a function, should be a string`);
        }
        const req = {
          method,
          url: uri,
          body,
          options: {
            withCredentials,
            headers,
            context
          }
        };
        const sub = fetch(req, this.httpClient, () => {
          throw new Error("File upload is not available when combined with Batching");
        }).subscribe({
          next: (result) => observer.next(result.body),
          error: (err) => observer.error(err),
          complete: () => observer.complete()
        });
        return () => {
          if (!sub.closed) {
            sub.unsubscribe();
          }
        };
      });
    };
    const batchKey = options.batchKey || ((operation) => {
      return this.createBatchKey(operation);
    });
    this.batcher = new BatchLink({
      batchInterval: this.batchInterval,
      batchMax: this.batchMax,
      batchKey,
      batchHandler
    });
  }
  createOptions(operations) {
    const context = operations[0].getContext();
    return {
      method: pick(context, this.options, "method"),
      uri: pick(context, this.options, "uri"),
      withCredentials: pick(context, this.options, "withCredentials")
    };
  }
  createBody(operations) {
    return operations.map((operation) => {
      const includeExtensions = prioritize(operation.getContext().includeExtensions, this.options.includeExtensions, false);
      const includeQuery = prioritize(operation.getContext().includeQuery, this.options.includeQuery, true);
      const body = {
        operationName: operation.operationName,
        variables: operation.variables
      };
      if (includeExtensions) {
        body.extensions = operation.extensions;
      }
      if (includeQuery) {
        body.query = this.print(operation.query);
      }
      return body;
    });
  }
  createHeaders(operations) {
    return operations.reduce((headers, operation) => {
      const {
        headers: contextHeaders
      } = operation.getContext();
      return contextHeaders ? mergeHeaders(headers, contextHeaders) : headers;
    }, createHeadersWithClientAwareness({
      headers: this.options.headers,
      clientAwareness: operations[0]?.getContext()?.clientAwareness
    }));
  }
  createHttpContext(operations) {
    return operations.reduce((context, operation) => {
      const {
        httpContext
      } = operation.getContext();
      return httpContext ? mergeHttpContext(httpContext, context) : context;
    }, mergeHttpContext(this.options.httpContext, new HttpContext()));
  }
  createBatchKey(operation) {
    const context = operation.getContext();
    if (context.skipBatching) {
      return Math.random().toString(36).substring(2, 11);
    }
    const headers = context.headers && context.headers.keys().map((k) => context.headers.get(k));
    const opts = JSON.stringify({
      includeQuery: context.includeQuery,
      includeExtensions: context.includeExtensions,
      headers
    });
    return prioritize(context.uri, this.options.uri, "") + opts;
  }
  request(op, forward) {
    return this.batcher.request(op, forward);
  }
};
var HttpBatchLink = class _HttpBatchLink {
  httpClient;
  constructor(httpClient) {
    this.httpClient = httpClient;
  }
  create(options) {
    return new HttpBatchLinkHandler(this.httpClient, options);
  }
  static ɵfac = function HttpBatchLink_Factory(__ngFactoryType__) {
    return new (__ngFactoryType__ || _HttpBatchLink)(ɵɵinject(HttpClient));
  };
  static ɵprov = ɵɵdefineInjectable({
    token: _HttpBatchLink,
    factory: _HttpBatchLink.ɵfac,
    providedIn: "root"
  });
};
(() => {
  (typeof ngDevMode === "undefined" || ngDevMode) && setClassMetadata(HttpBatchLink, [{
    type: Injectable,
    args: [{
      providedIn: "root"
    }]
  }], () => [{
    type: HttpClient
  }], null);
})();
var HttpLinkHandler = class extends ApolloLink {
  httpClient;
  options;
  requester;
  print = print;
  constructor(httpClient, options) {
    super();
    this.httpClient = httpClient;
    this.options = options;
    if (this.options.operationPrinter) {
      this.print = this.options.operationPrinter;
    }
    this.requester = (operation) => new Observable((observer) => {
      const context = operation.getContext();
      let method = pick(context, this.options, "method");
      const includeQuery = pick(context, this.options, "includeQuery");
      const includeExtensions = pick(context, this.options, "includeExtensions");
      const url = pick(context, this.options, "uri");
      const withCredentials = pick(context, this.options, "withCredentials");
      const useMultipart = pick(context, this.options, "useMultipart");
      const useGETForQueries = this.options.useGETForQueries === true;
      const httpContext = mergeHttpContext(context.httpContext, mergeHttpContext(this.options.httpContext, new HttpContext()));
      const isQuery = operation.query.definitions.some((def) => def.kind === "OperationDefinition" && def.operation === "query");
      if (useGETForQueries && isQuery) {
        method = "GET";
      }
      const req = {
        method,
        url: typeof url === "function" ? url(operation) : url,
        body: {
          operationName: operation.operationName,
          variables: operation.variables
        },
        options: {
          withCredentials,
          useMultipart,
          headers: this.options.headers,
          context: httpContext
        }
      };
      if (includeExtensions) {
        req.body.extensions = operation.extensions;
      }
      if (includeQuery) {
        req.body.query = this.print(operation.query);
      }
      const headers = createHeadersWithClientAwareness(context);
      req.options.headers = mergeHeaders(req.options.headers, headers);
      const sub = fetch(req, this.httpClient, this.options.extractFiles).subscribe({
        next: (response) => {
          operation.setContext({
            response
          });
          observer.next(response.body);
        },
        error: (err) => observer.error(err),
        complete: () => observer.complete()
      });
      return () => {
        if (!sub.closed) {
          sub.unsubscribe();
        }
      };
    });
  }
  request(op) {
    return this.requester(op);
  }
};
var HttpLink = class _HttpLink {
  httpClient;
  constructor(httpClient) {
    this.httpClient = httpClient;
  }
  create(options) {
    return new HttpLinkHandler(this.httpClient, options);
  }
  static ɵfac = function HttpLink_Factory(__ngFactoryType__) {
    return new (__ngFactoryType__ || _HttpLink)(ɵɵinject(HttpClient));
  };
  static ɵprov = ɵɵdefineInjectable({
    token: _HttpLink,
    factory: _HttpLink.ɵfac,
    providedIn: "root"
  });
};
(() => {
  (typeof ngDevMode === "undefined" || ngDevMode) && setClassMetadata(HttpLink, [{
    type: Injectable,
    args: [{
      providedIn: "root"
    }]
  }], () => [{
    type: HttpClient
  }], null);
})();
export {
  HttpBatchLink,
  HttpBatchLinkHandler,
  HttpLink,
  HttpLinkHandler
};
//# sourceMappingURL=apollo-angular_http.js.map
