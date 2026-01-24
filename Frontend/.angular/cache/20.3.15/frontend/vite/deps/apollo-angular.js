import {
  Inject,
  Injectable,
  InjectionToken,
  NgZone,
  Optional,
  setClassMetadata,
  ɵɵdefineInjectable,
  ɵɵinject
} from "./chunk-ZAFN4FGN.js";
import "./chunk-BWIFR55H.js";
import {
  ApolloClient,
  gql
} from "./chunk-M4JOHYKZ.js";
import {
  queueScheduler
} from "./chunk-RXKYQXTI.js";
import {
  Observable,
  filter,
  from,
  map,
  observeOn,
  startWith
} from "./chunk-KXUNLAST.js";
import {
  __objRest,
  __spreadProps,
  __spreadValues
} from "./chunk-QZZYNKJP.js";

// node_modules/apollo-angular/fesm2022/apollo-angular.mjs
function fromLazyPromise(promiseFn) {
  return new Observable((subscriber) => {
    promiseFn().then((result) => {
      if (!subscriber.closed) {
        subscriber.next(result);
        subscriber.complete();
      }
    }, (error) => {
      if (!subscriber.closed) {
        subscriber.error(error);
      }
    });
    return () => subscriber.unsubscribe();
  });
}
function useMutationLoading(source, enabled) {
  if (!enabled) {
    return source.pipe(map((result) => __spreadProps(__spreadValues({}, result), {
      loading: false
    })));
  }
  return source.pipe(map((result) => __spreadProps(__spreadValues({}, result), {
    loading: false
  })), startWith({
    data: void 0,
    loading: true
  }));
}
var ZoneScheduler = class {
  zone;
  constructor(zone) {
    this.zone = zone;
  }
  now = Date.now;
  schedule(work, delay = 0, state) {
    return this.zone.run(() => queueScheduler.schedule(work, delay, state));
  }
};
function wrapWithZone(obs, ngZone) {
  return obs.pipe(observeOn(new ZoneScheduler(ngZone)));
}
var QueryRef = class {
  obsQuery;
  valueChanges;
  constructor(obsQuery, ngZone) {
    this.obsQuery = obsQuery;
    this.valueChanges = wrapWithZone(from(this.obsQuery), ngZone);
  }
  // ObservableQuery's methods
  get options() {
    return this.obsQuery.options;
  }
  get variables() {
    return this.obsQuery.variables;
  }
  getCurrentResult() {
    return this.obsQuery.getCurrentResult();
  }
  refetch(variables) {
    return this.obsQuery.refetch(variables);
  }
  fetchMore(fetchMoreOptions) {
    return this.obsQuery.fetchMore(fetchMoreOptions);
  }
  subscribeToMore(options) {
    return this.obsQuery.subscribeToMore(options);
  }
  updateQuery(mapFn) {
    return this.obsQuery.updateQuery(mapFn);
  }
  stopPolling() {
    return this.obsQuery.stopPolling();
  }
  startPolling(pollInterval) {
    return this.obsQuery.startPolling(pollInterval);
  }
  setVariables(variables) {
    return this.obsQuery.setVariables(variables);
  }
  reobserve(options) {
    return this.obsQuery.reobserve(options);
  }
};
var APOLLO_FLAGS = new InjectionToken("APOLLO_FLAGS");
var APOLLO_OPTIONS = new InjectionToken("APOLLO_OPTIONS");
var APOLLO_NAMED_OPTIONS = new InjectionToken("APOLLO_NAMED_OPTIONS");
var ApolloBase = class {
  ngZone;
  flags;
  _client;
  useMutationLoading;
  constructor(ngZone, flags, _client) {
    this.ngZone = ngZone;
    this.flags = flags;
    this._client = _client;
    this.useMutationLoading = flags?.useMutationLoading ?? false;
  }
  watchQuery(options) {
    return new QueryRef(this.ensureClient().watchQuery(__spreadValues({}, options)), this.ngZone);
  }
  query(options) {
    return fromLazyPromise(() => this.ensureClient().query(__spreadValues({}, options)));
  }
  mutate(options) {
    return useMutationLoading(fromLazyPromise(() => this.ensureClient().mutate(__spreadValues({}, options))), options.useMutationLoading ?? this.useMutationLoading);
  }
  watchFragment(options) {
    const _a = options, {
      useZone
    } = _a, opts = __objRest(_a, [
      "useZone"
    ]);
    const obs = this.ensureClient().watchFragment(__spreadValues({}, opts));
    return useZone !== true ? obs : wrapWithZone(obs, this.ngZone);
  }
  subscribe(options) {
    const _a = options, {
      useZone
    } = _a, opts = __objRest(_a, [
      "useZone"
    ]);
    const obs = this.ensureClient().subscribe(__spreadValues({}, opts));
    return useZone !== true ? obs : wrapWithZone(obs, this.ngZone);
  }
  /**
   * Get an instance of ApolloClient
   */
  get client() {
    return this.ensureClient();
  }
  /**
   * Set a new instance of ApolloClient
   * Remember to clean up the store before setting a new client.
   *
   * @param client ApolloClient instance
   */
  set client(client) {
    if (this._client) {
      throw new Error("Client has been already defined");
    }
    this._client = client;
  }
  ensureClient() {
    this.checkInstance();
    return this._client;
  }
  checkInstance() {
    if (this._client) {
      return true;
    } else {
      throw new Error("Client has not been defined yet");
    }
  }
};
var Apollo = class _Apollo extends ApolloBase {
  map = /* @__PURE__ */ new Map();
  constructor(ngZone, apolloOptions, apolloNamedOptions, flags) {
    super(ngZone, flags);
    if (apolloOptions) {
      this.createDefault(apolloOptions);
    }
    if (apolloNamedOptions && typeof apolloNamedOptions === "object") {
      for (let name in apolloNamedOptions) {
        if (apolloNamedOptions.hasOwnProperty(name)) {
          const options = apolloNamedOptions[name];
          this.create(options, name);
        }
      }
    }
  }
  /**
   * Create an instance of ApolloClient
   * @param options Options required to create ApolloClient
   * @param name client's name
   */
  create(options, name) {
    if (isNamed(name)) {
      this.createNamed(name, options);
    } else {
      this.createDefault(options);
    }
  }
  /**
   * Use a default ApolloClient
   */
  default() {
    return this;
  }
  /**
   * Use a named ApolloClient
   * @param name client's name
   */
  use(name) {
    if (isNamed(name)) {
      return this.map.get(name);
    } else {
      return this.default();
    }
  }
  /**
   * Create a default ApolloClient, same as `apollo.create(options)`
   * @param options ApolloClient's options
   */
  createDefault(options) {
    if (this._client) {
      throw new Error("Apollo has been already created.");
    }
    this.client = this.ngZone.runOutsideAngular(() => new ApolloClient(options));
  }
  /**
   * Create a named ApolloClient, same as `apollo.create(options, name)`
   * @param name client's name
   * @param options ApolloClient's options
   */
  createNamed(name, options) {
    if (this.map.has(name)) {
      throw new Error(`Client ${name} has been already created`);
    }
    this.map.set(name, new ApolloBase(this.ngZone, this.flags, this.ngZone.runOutsideAngular(() => new ApolloClient(options))));
  }
  /**
   * Remember to clean up the store before removing a client
   * @param name client's name
   */
  removeClient(name) {
    if (isNamed(name)) {
      this.map.delete(name);
    } else {
      this._client = void 0;
    }
  }
  static ɵfac = function Apollo_Factory(__ngFactoryType__) {
    return new (__ngFactoryType__ || _Apollo)(ɵɵinject(NgZone), ɵɵinject(APOLLO_OPTIONS, 8), ɵɵinject(APOLLO_NAMED_OPTIONS, 8), ɵɵinject(APOLLO_FLAGS, 8));
  };
  static ɵprov = ɵɵdefineInjectable({
    token: _Apollo,
    factory: _Apollo.ɵfac
  });
};
(() => {
  (typeof ngDevMode === "undefined" || ngDevMode) && setClassMetadata(Apollo, [{
    type: Injectable
  }], () => [{
    type: NgZone
  }, {
    type: void 0,
    decorators: [{
      type: Optional
    }, {
      type: Inject,
      args: [APOLLO_OPTIONS]
    }]
  }, {
    type: void 0,
    decorators: [{
      type: Inject,
      args: [APOLLO_NAMED_OPTIONS]
    }, {
      type: Optional
    }]
  }, {
    type: void 0,
    decorators: [{
      type: Inject,
      args: [APOLLO_FLAGS]
    }, {
      type: Optional
    }]
  }], null);
})();
function isNamed(name) {
  return !!name && name !== "default";
}
function provideApollo(optionsFactory, flags = {}) {
  return [Apollo, {
    provide: APOLLO_OPTIONS,
    useFactory: optionsFactory
  }, {
    provide: APOLLO_FLAGS,
    useValue: flags
  }];
}
function provideNamedApollo(optionsFactory, flags = {}) {
  return [Apollo, {
    provide: APOLLO_NAMED_OPTIONS,
    useFactory: optionsFactory
  }, {
    provide: APOLLO_FLAGS,
    useValue: flags
  }];
}
var Query = class _Query {
  apollo;
  client = "default";
  constructor(apollo) {
    this.apollo = apollo;
  }
  watch(...[options]) {
    return this.apollo.use(this.client).watchQuery(__spreadProps(__spreadValues({}, options), {
      query: this.document
    }));
  }
  fetch(...[options]) {
    return this.apollo.use(this.client).query(__spreadProps(__spreadValues({}, options), {
      query: this.document
    }));
  }
  static ɵfac = function Query_Factory(__ngFactoryType__) {
    return new (__ngFactoryType__ || _Query)(ɵɵinject(Apollo));
  };
  static ɵprov = ɵɵdefineInjectable({
    token: _Query,
    factory: _Query.ɵfac
  });
};
(() => {
  (typeof ngDevMode === "undefined" || ngDevMode) && setClassMetadata(Query, [{
    type: Injectable
  }], () => [{
    type: Apollo
  }], null);
})();
var Mutation = class _Mutation {
  apollo;
  client = "default";
  constructor(apollo) {
    this.apollo = apollo;
  }
  mutate(...[options]) {
    return this.apollo.use(this.client).mutate(__spreadProps(__spreadValues({}, options), {
      mutation: this.document
    }));
  }
  static ɵfac = function Mutation_Factory(__ngFactoryType__) {
    return new (__ngFactoryType__ || _Mutation)(ɵɵinject(Apollo));
  };
  static ɵprov = ɵɵdefineInjectable({
    token: _Mutation,
    factory: _Mutation.ɵfac
  });
};
(() => {
  (typeof ngDevMode === "undefined" || ngDevMode) && setClassMetadata(Mutation, [{
    type: Injectable
  }], () => [{
    type: Apollo
  }], null);
})();
var Subscription = class _Subscription {
  apollo;
  client = "default";
  constructor(apollo) {
    this.apollo = apollo;
  }
  subscribe(...[options]) {
    return this.apollo.use(this.client).subscribe(__spreadProps(__spreadValues({}, options), {
      query: this.document
    }));
  }
  static ɵfac = function Subscription_Factory(__ngFactoryType__) {
    return new (__ngFactoryType__ || _Subscription)(ɵɵinject(Apollo));
  };
  static ɵprov = ɵɵdefineInjectable({
    token: _Subscription,
    factory: _Subscription.ɵfac
  });
};
(() => {
  (typeof ngDevMode === "undefined" || ngDevMode) && setClassMetadata(Subscription, [{
    type: Injectable
  }], () => [{
    type: Apollo
  }], null);
})();
var typedGQLTag = gql;
var gql2 = typedGQLTag;
function onlyCompleteData() {
  return filter((result) => result.dataState === "complete");
}
var onlyComplete = onlyCompleteData;
function onlyCompleteFragment() {
  return filter((result) => result.dataState === "complete");
}
export {
  APOLLO_FLAGS,
  APOLLO_NAMED_OPTIONS,
  APOLLO_OPTIONS,
  Apollo,
  ApolloBase,
  Mutation,
  Query,
  QueryRef,
  Subscription,
  gql2 as gql,
  onlyComplete,
  onlyCompleteData,
  onlyCompleteFragment,
  provideApollo,
  provideNamedApollo
};
//# sourceMappingURL=apollo-angular.js.map
