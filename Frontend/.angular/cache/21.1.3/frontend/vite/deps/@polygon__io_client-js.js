import {
  __async,
  __asyncGenerator,
  __await,
  __commonJS,
  __export,
  __forAwait,
  __spreadProps,
  __spreadValues,
  __toESM,
  __yieldStar
} from "./chunk-4BXWKWGJ.js";

// node_modules/es5-ext/global.js
var require_global = __commonJS({
  "node_modules/es5-ext/global.js"(exports, module) {
    var naiveFallback = function() {
      if (typeof self === "object" && self) return self;
      if (typeof window === "object" && window) return window;
      throw new Error("Unable to resolve global `this`");
    };
    module.exports = (function() {
      if (this) return this;
      if (typeof globalThis === "object" && globalThis) return globalThis;
      try {
        Object.defineProperty(Object.prototype, "__global__", {
          get: function() {
            return this;
          },
          configurable: true
        });
      } catch (error) {
        return naiveFallback();
      }
      try {
        if (!__global__) return naiveFallback();
        return __global__;
      } finally {
        delete Object.prototype.__global__;
      }
    })();
  }
});

// node_modules/websocket/package.json
var require_package = __commonJS({
  "node_modules/websocket/package.json"(exports, module) {
    module.exports = {
      name: "websocket",
      description: "Websocket Client & Server Library implementing the WebSocket protocol as specified in RFC 6455.",
      keywords: [
        "websocket",
        "websockets",
        "socket",
        "networking",
        "comet",
        "push",
        "RFC-6455",
        "realtime",
        "server",
        "client"
      ],
      author: "Brian McKelvey <theturtle32@gmail.com> (https://github.com/theturtle32)",
      contributors: [
        "Iñaki Baz Castillo <ibc@aliax.net> (http://dev.sipdoc.net)"
      ],
      version: "1.0.35",
      repository: {
        type: "git",
        url: "https://github.com/theturtle32/WebSocket-Node.git"
      },
      homepage: "https://github.com/theturtle32/WebSocket-Node",
      engines: {
        node: ">=4.0.0"
      },
      dependencies: {
        bufferutil: "^4.0.1",
        debug: "^2.2.0",
        "es5-ext": "^0.10.63",
        "typedarray-to-buffer": "^3.1.5",
        "utf-8-validate": "^5.0.2",
        yaeti: "^0.0.6"
      },
      devDependencies: {
        "buffer-equal": "^1.0.0",
        gulp: "^4.0.2",
        "gulp-jshint": "^2.0.4",
        "jshint-stylish": "^2.2.1",
        jshint: "^2.0.0",
        tape: "^4.9.1"
      },
      config: {
        verbose: false
      },
      scripts: {
        test: "tape test/unit/*.js",
        gulp: "gulp"
      },
      main: "index",
      directories: {
        lib: "./lib"
      },
      browser: "lib/browser.js",
      license: "Apache-2.0"
    };
  }
});

// node_modules/websocket/lib/version.js
var require_version = __commonJS({
  "node_modules/websocket/lib/version.js"(exports, module) {
    module.exports = require_package().version;
  }
});

// node_modules/websocket/lib/browser.js
var require_browser = __commonJS({
  "node_modules/websocket/lib/browser.js"(exports, module) {
    var _globalThis;
    if (typeof globalThis === "object") {
      _globalThis = globalThis;
    } else {
      try {
        _globalThis = require_global();
      } catch (error) {
      } finally {
        if (!_globalThis && typeof window !== "undefined") {
          _globalThis = window;
        }
        if (!_globalThis) {
          throw new Error("Could not determine global this");
        }
      }
    }
    var NativeWebSocket = _globalThis.WebSocket || _globalThis.MozWebSocket;
    var websocket_version = require_version();
    function W3CWebSocket(uri, protocols) {
      var native_instance;
      if (protocols) {
        native_instance = new NativeWebSocket(uri, protocols);
      } else {
        native_instance = new NativeWebSocket(uri);
      }
      return native_instance;
    }
    if (NativeWebSocket) {
      ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function(prop) {
        Object.defineProperty(W3CWebSocket, prop, {
          get: function() {
            return NativeWebSocket[prop];
          }
        });
      });
    }
    module.exports = {
      "w3cwebsocket": NativeWebSocket ? W3CWebSocket : null,
      "version": websocket_version
    };
  }
});

// node_modules/axios/lib/helpers/bind.js
function bind(fn, thisArg) {
  return function wrap() {
    return fn.apply(thisArg, arguments);
  };
}

// node_modules/axios/lib/utils.js
var { toString } = Object.prototype;
var { getPrototypeOf } = Object;
var { iterator, toStringTag } = Symbol;
var kindOf = /* @__PURE__ */ ((cache) => (thing) => {
  const str = toString.call(thing);
  return cache[str] || (cache[str] = str.slice(8, -1).toLowerCase());
})(/* @__PURE__ */ Object.create(null));
var kindOfTest = (type) => {
  type = type.toLowerCase();
  return (thing) => kindOf(thing) === type;
};
var typeOfTest = (type) => (thing) => typeof thing === type;
var { isArray } = Array;
var isUndefined = typeOfTest("undefined");
function isBuffer(val) {
  return val !== null && !isUndefined(val) && val.constructor !== null && !isUndefined(val.constructor) && isFunction(val.constructor.isBuffer) && val.constructor.isBuffer(val);
}
var isArrayBuffer = kindOfTest("ArrayBuffer");
function isArrayBufferView(val) {
  let result;
  if (typeof ArrayBuffer !== "undefined" && ArrayBuffer.isView) {
    result = ArrayBuffer.isView(val);
  } else {
    result = val && val.buffer && isArrayBuffer(val.buffer);
  }
  return result;
}
var isString = typeOfTest("string");
var isFunction = typeOfTest("function");
var isNumber = typeOfTest("number");
var isObject = (thing) => thing !== null && typeof thing === "object";
var isBoolean = (thing) => thing === true || thing === false;
var isPlainObject = (val) => {
  if (kindOf(val) !== "object") {
    return false;
  }
  const prototype2 = getPrototypeOf(val);
  return (prototype2 === null || prototype2 === Object.prototype || Object.getPrototypeOf(prototype2) === null) && !(toStringTag in val) && !(iterator in val);
};
var isEmptyObject = (val) => {
  if (!isObject(val) || isBuffer(val)) {
    return false;
  }
  try {
    return Object.keys(val).length === 0 && Object.getPrototypeOf(val) === Object.prototype;
  } catch (e) {
    return false;
  }
};
var isDate = kindOfTest("Date");
var isFile = kindOfTest("File");
var isBlob = kindOfTest("Blob");
var isFileList = kindOfTest("FileList");
var isStream = (val) => isObject(val) && isFunction(val.pipe);
var isFormData = (thing) => {
  let kind;
  return thing && (typeof FormData === "function" && thing instanceof FormData || isFunction(thing.append) && ((kind = kindOf(thing)) === "formdata" || // detect form-data instance
  kind === "object" && isFunction(thing.toString) && thing.toString() === "[object FormData]"));
};
var isURLSearchParams = kindOfTest("URLSearchParams");
var [isReadableStream, isRequest, isResponse, isHeaders] = [
  "ReadableStream",
  "Request",
  "Response",
  "Headers"
].map(kindOfTest);
var trim = (str) => str.trim ? str.trim() : str.replace(/^[\s\uFEFF\xA0]+|[\s\uFEFF\xA0]+$/g, "");
function forEach(obj, fn, { allOwnKeys = false } = {}) {
  if (obj === null || typeof obj === "undefined") {
    return;
  }
  let i;
  let l;
  if (typeof obj !== "object") {
    obj = [obj];
  }
  if (isArray(obj)) {
    for (i = 0, l = obj.length; i < l; i++) {
      fn.call(null, obj[i], i, obj);
    }
  } else {
    if (isBuffer(obj)) {
      return;
    }
    const keys = allOwnKeys ? Object.getOwnPropertyNames(obj) : Object.keys(obj);
    const len = keys.length;
    let key;
    for (i = 0; i < len; i++) {
      key = keys[i];
      fn.call(null, obj[key], key, obj);
    }
  }
}
function findKey(obj, key) {
  if (isBuffer(obj)) {
    return null;
  }
  key = key.toLowerCase();
  const keys = Object.keys(obj);
  let i = keys.length;
  let _key;
  while (i-- > 0) {
    _key = keys[i];
    if (key === _key.toLowerCase()) {
      return _key;
    }
  }
  return null;
}
var _global = (() => {
  if (typeof globalThis !== "undefined") return globalThis;
  return typeof self !== "undefined" ? self : typeof window !== "undefined" ? window : global;
})();
var isContextDefined = (context) => !isUndefined(context) && context !== _global;
function merge() {
  const { caseless, skipUndefined } = isContextDefined(this) && this || {};
  const result = {};
  const assignValue = (val, key) => {
    if (key === "__proto__" || key === "constructor" || key === "prototype") {
      return;
    }
    const targetKey = caseless && findKey(result, key) || key;
    if (isPlainObject(result[targetKey]) && isPlainObject(val)) {
      result[targetKey] = merge(result[targetKey], val);
    } else if (isPlainObject(val)) {
      result[targetKey] = merge({}, val);
    } else if (isArray(val)) {
      result[targetKey] = val.slice();
    } else if (!skipUndefined || !isUndefined(val)) {
      result[targetKey] = val;
    }
  };
  for (let i = 0, l = arguments.length; i < l; i++) {
    arguments[i] && forEach(arguments[i], assignValue);
  }
  return result;
}
var extend = (a, b, thisArg, { allOwnKeys } = {}) => {
  forEach(
    b,
    (val, key) => {
      if (thisArg && isFunction(val)) {
        Object.defineProperty(a, key, {
          value: bind(val, thisArg),
          writable: true,
          enumerable: true,
          configurable: true
        });
      } else {
        Object.defineProperty(a, key, {
          value: val,
          writable: true,
          enumerable: true,
          configurable: true
        });
      }
    },
    { allOwnKeys }
  );
  return a;
};
var stripBOM = (content) => {
  if (content.charCodeAt(0) === 65279) {
    content = content.slice(1);
  }
  return content;
};
var inherits = (constructor, superConstructor, props, descriptors) => {
  constructor.prototype = Object.create(
    superConstructor.prototype,
    descriptors
  );
  Object.defineProperty(constructor.prototype, "constructor", {
    value: constructor,
    writable: true,
    enumerable: false,
    configurable: true
  });
  Object.defineProperty(constructor, "super", {
    value: superConstructor.prototype
  });
  props && Object.assign(constructor.prototype, props);
};
var toFlatObject = (sourceObj, destObj, filter2, propFilter) => {
  let props;
  let i;
  let prop;
  const merged = {};
  destObj = destObj || {};
  if (sourceObj == null) return destObj;
  do {
    props = Object.getOwnPropertyNames(sourceObj);
    i = props.length;
    while (i-- > 0) {
      prop = props[i];
      if ((!propFilter || propFilter(prop, sourceObj, destObj)) && !merged[prop]) {
        destObj[prop] = sourceObj[prop];
        merged[prop] = true;
      }
    }
    sourceObj = filter2 !== false && getPrototypeOf(sourceObj);
  } while (sourceObj && (!filter2 || filter2(sourceObj, destObj)) && sourceObj !== Object.prototype);
  return destObj;
};
var endsWith = (str, searchString, position) => {
  str = String(str);
  if (position === void 0 || position > str.length) {
    position = str.length;
  }
  position -= searchString.length;
  const lastIndex = str.indexOf(searchString, position);
  return lastIndex !== -1 && lastIndex === position;
};
var toArray = (thing) => {
  if (!thing) return null;
  if (isArray(thing)) return thing;
  let i = thing.length;
  if (!isNumber(i)) return null;
  const arr = new Array(i);
  while (i-- > 0) {
    arr[i] = thing[i];
  }
  return arr;
};
var isTypedArray = /* @__PURE__ */ ((TypedArray) => {
  return (thing) => {
    return TypedArray && thing instanceof TypedArray;
  };
})(typeof Uint8Array !== "undefined" && getPrototypeOf(Uint8Array));
var forEachEntry = (obj, fn) => {
  const generator = obj && obj[iterator];
  const _iterator = generator.call(obj);
  let result;
  while ((result = _iterator.next()) && !result.done) {
    const pair = result.value;
    fn.call(obj, pair[0], pair[1]);
  }
};
var matchAll = (regExp, str) => {
  let matches;
  const arr = [];
  while ((matches = regExp.exec(str)) !== null) {
    arr.push(matches);
  }
  return arr;
};
var isHTMLForm = kindOfTest("HTMLFormElement");
var toCamelCase = (str) => {
  return str.toLowerCase().replace(/[-_\s]([a-z\d])(\w*)/g, function replacer(m, p1, p2) {
    return p1.toUpperCase() + p2;
  });
};
var hasOwnProperty = (({ hasOwnProperty: hasOwnProperty2 }) => (obj, prop) => hasOwnProperty2.call(obj, prop))(Object.prototype);
var isRegExp = kindOfTest("RegExp");
var reduceDescriptors = (obj, reducer) => {
  const descriptors = Object.getOwnPropertyDescriptors(obj);
  const reducedDescriptors = {};
  forEach(descriptors, (descriptor, name) => {
    let ret;
    if ((ret = reducer(descriptor, name, obj)) !== false) {
      reducedDescriptors[name] = ret || descriptor;
    }
  });
  Object.defineProperties(obj, reducedDescriptors);
};
var freezeMethods = (obj) => {
  reduceDescriptors(obj, (descriptor, name) => {
    if (isFunction(obj) && ["arguments", "caller", "callee"].indexOf(name) !== -1) {
      return false;
    }
    const value = obj[name];
    if (!isFunction(value)) return;
    descriptor.enumerable = false;
    if ("writable" in descriptor) {
      descriptor.writable = false;
      return;
    }
    if (!descriptor.set) {
      descriptor.set = () => {
        throw Error("Can not rewrite read-only method '" + name + "'");
      };
    }
  });
};
var toObjectSet = (arrayOrString, delimiter) => {
  const obj = {};
  const define = (arr) => {
    arr.forEach((value) => {
      obj[value] = true;
    });
  };
  isArray(arrayOrString) ? define(arrayOrString) : define(String(arrayOrString).split(delimiter));
  return obj;
};
var noop = () => {
};
var toFiniteNumber = (value, defaultValue) => {
  return value != null && Number.isFinite(value = +value) ? value : defaultValue;
};
function isSpecCompliantForm(thing) {
  return !!(thing && isFunction(thing.append) && thing[toStringTag] === "FormData" && thing[iterator]);
}
var toJSONObject = (obj) => {
  const stack = new Array(10);
  const visit = (source, i) => {
    if (isObject(source)) {
      if (stack.indexOf(source) >= 0) {
        return;
      }
      if (isBuffer(source)) {
        return source;
      }
      if (!("toJSON" in source)) {
        stack[i] = source;
        const target = isArray(source) ? [] : {};
        forEach(source, (value, key) => {
          const reducedValue = visit(value, i + 1);
          !isUndefined(reducedValue) && (target[key] = reducedValue);
        });
        stack[i] = void 0;
        return target;
      }
    }
    return source;
  };
  return visit(obj, 0);
};
var isAsyncFn = kindOfTest("AsyncFunction");
var isThenable = (thing) => thing && (isObject(thing) || isFunction(thing)) && isFunction(thing.then) && isFunction(thing.catch);
var _setImmediate = ((setImmediateSupported, postMessageSupported) => {
  if (setImmediateSupported) {
    return setImmediate;
  }
  return postMessageSupported ? ((token, callbacks) => {
    _global.addEventListener(
      "message",
      ({ source, data }) => {
        if (source === _global && data === token) {
          callbacks.length && callbacks.shift()();
        }
      },
      false
    );
    return (cb) => {
      callbacks.push(cb);
      _global.postMessage(token, "*");
    };
  })(`axios@${Math.random()}`, []) : (cb) => setTimeout(cb);
})(typeof setImmediate === "function", isFunction(_global.postMessage));
var asap = typeof queueMicrotask !== "undefined" ? queueMicrotask.bind(_global) : typeof process !== "undefined" && process.nextTick || _setImmediate;
var isIterable = (thing) => thing != null && isFunction(thing[iterator]);
var utils_default = {
  isArray,
  isArrayBuffer,
  isBuffer,
  isFormData,
  isArrayBufferView,
  isString,
  isNumber,
  isBoolean,
  isObject,
  isPlainObject,
  isEmptyObject,
  isReadableStream,
  isRequest,
  isResponse,
  isHeaders,
  isUndefined,
  isDate,
  isFile,
  isBlob,
  isRegExp,
  isFunction,
  isStream,
  isURLSearchParams,
  isTypedArray,
  isFileList,
  forEach,
  merge,
  extend,
  trim,
  stripBOM,
  inherits,
  toFlatObject,
  kindOf,
  kindOfTest,
  endsWith,
  toArray,
  forEachEntry,
  matchAll,
  isHTMLForm,
  hasOwnProperty,
  hasOwnProp: hasOwnProperty,
  // an alias to avoid ESLint no-prototype-builtins detection
  reduceDescriptors,
  freezeMethods,
  toObjectSet,
  toCamelCase,
  noop,
  toFiniteNumber,
  findKey,
  global: _global,
  isContextDefined,
  isSpecCompliantForm,
  toJSONObject,
  isAsyncFn,
  isThenable,
  setImmediate: _setImmediate,
  asap,
  isIterable
};

// node_modules/axios/lib/core/AxiosError.js
var AxiosError = class _AxiosError extends Error {
  static from(error, code, config, request, response, customProps) {
    const axiosError = new _AxiosError(error.message, code || error.code, config, request, response);
    axiosError.cause = error;
    axiosError.name = error.name;
    customProps && Object.assign(axiosError, customProps);
    return axiosError;
  }
  /**
   * Create an Error with the specified message, config, error code, request and response.
   *
   * @param {string} message The error message.
   * @param {string} [code] The error code (for example, 'ECONNABORTED').
   * @param {Object} [config] The config.
   * @param {Object} [request] The request.
   * @param {Object} [response] The response.
   *
   * @returns {Error} The created error.
   */
  constructor(message, code, config, request, response) {
    super(message);
    this.name = "AxiosError";
    this.isAxiosError = true;
    code && (this.code = code);
    config && (this.config = config);
    request && (this.request = request);
    if (response) {
      this.response = response;
      this.status = response.status;
    }
  }
  toJSON() {
    return {
      // Standard
      message: this.message,
      name: this.name,
      // Microsoft
      description: this.description,
      number: this.number,
      // Mozilla
      fileName: this.fileName,
      lineNumber: this.lineNumber,
      columnNumber: this.columnNumber,
      stack: this.stack,
      // Axios
      config: utils_default.toJSONObject(this.config),
      code: this.code,
      status: this.status
    };
  }
};
AxiosError.ERR_BAD_OPTION_VALUE = "ERR_BAD_OPTION_VALUE";
AxiosError.ERR_BAD_OPTION = "ERR_BAD_OPTION";
AxiosError.ECONNABORTED = "ECONNABORTED";
AxiosError.ETIMEDOUT = "ETIMEDOUT";
AxiosError.ERR_NETWORK = "ERR_NETWORK";
AxiosError.ERR_FR_TOO_MANY_REDIRECTS = "ERR_FR_TOO_MANY_REDIRECTS";
AxiosError.ERR_DEPRECATED = "ERR_DEPRECATED";
AxiosError.ERR_BAD_RESPONSE = "ERR_BAD_RESPONSE";
AxiosError.ERR_BAD_REQUEST = "ERR_BAD_REQUEST";
AxiosError.ERR_CANCELED = "ERR_CANCELED";
AxiosError.ERR_NOT_SUPPORT = "ERR_NOT_SUPPORT";
AxiosError.ERR_INVALID_URL = "ERR_INVALID_URL";
var AxiosError_default = AxiosError;

// node_modules/axios/lib/helpers/null.js
var null_default = null;

// node_modules/axios/lib/helpers/toFormData.js
function isVisitable(thing) {
  return utils_default.isPlainObject(thing) || utils_default.isArray(thing);
}
function removeBrackets(key) {
  return utils_default.endsWith(key, "[]") ? key.slice(0, -2) : key;
}
function renderKey(path, key, dots) {
  if (!path) return key;
  return path.concat(key).map(function each(token, i) {
    token = removeBrackets(token);
    return !dots && i ? "[" + token + "]" : token;
  }).join(dots ? "." : "");
}
function isFlatArray(arr) {
  return utils_default.isArray(arr) && !arr.some(isVisitable);
}
var predicates = utils_default.toFlatObject(utils_default, {}, null, function filter(prop) {
  return /^is[A-Z]/.test(prop);
});
function toFormData(obj, formData, options) {
  if (!utils_default.isObject(obj)) {
    throw new TypeError("target must be an object");
  }
  formData = formData || new (null_default || FormData)();
  options = utils_default.toFlatObject(options, {
    metaTokens: true,
    dots: false,
    indexes: false
  }, false, function defined(option, source) {
    return !utils_default.isUndefined(source[option]);
  });
  const metaTokens = options.metaTokens;
  const visitor = options.visitor || defaultVisitor;
  const dots = options.dots;
  const indexes = options.indexes;
  const _Blob = options.Blob || typeof Blob !== "undefined" && Blob;
  const useBlob = _Blob && utils_default.isSpecCompliantForm(formData);
  if (!utils_default.isFunction(visitor)) {
    throw new TypeError("visitor must be a function");
  }
  function convertValue(value) {
    if (value === null) return "";
    if (utils_default.isDate(value)) {
      return value.toISOString();
    }
    if (utils_default.isBoolean(value)) {
      return value.toString();
    }
    if (!useBlob && utils_default.isBlob(value)) {
      throw new AxiosError_default("Blob is not supported. Use a Buffer instead.");
    }
    if (utils_default.isArrayBuffer(value) || utils_default.isTypedArray(value)) {
      return useBlob && typeof Blob === "function" ? new Blob([value]) : Buffer.from(value);
    }
    return value;
  }
  function defaultVisitor(value, key, path) {
    let arr = value;
    if (value && !path && typeof value === "object") {
      if (utils_default.endsWith(key, "{}")) {
        key = metaTokens ? key : key.slice(0, -2);
        value = JSON.stringify(value);
      } else if (utils_default.isArray(value) && isFlatArray(value) || (utils_default.isFileList(value) || utils_default.endsWith(key, "[]")) && (arr = utils_default.toArray(value))) {
        key = removeBrackets(key);
        arr.forEach(function each(el, index) {
          !(utils_default.isUndefined(el) || el === null) && formData.append(
            // eslint-disable-next-line no-nested-ternary
            indexes === true ? renderKey([key], index, dots) : indexes === null ? key : key + "[]",
            convertValue(el)
          );
        });
        return false;
      }
    }
    if (isVisitable(value)) {
      return true;
    }
    formData.append(renderKey(path, key, dots), convertValue(value));
    return false;
  }
  const stack = [];
  const exposedHelpers = Object.assign(predicates, {
    defaultVisitor,
    convertValue,
    isVisitable
  });
  function build(value, path) {
    if (utils_default.isUndefined(value)) return;
    if (stack.indexOf(value) !== -1) {
      throw Error("Circular reference detected in " + path.join("."));
    }
    stack.push(value);
    utils_default.forEach(value, function each(el, key) {
      const result = !(utils_default.isUndefined(el) || el === null) && visitor.call(
        formData,
        el,
        utils_default.isString(key) ? key.trim() : key,
        path,
        exposedHelpers
      );
      if (result === true) {
        build(el, path ? path.concat(key) : [key]);
      }
    });
    stack.pop();
  }
  if (!utils_default.isObject(obj)) {
    throw new TypeError("data must be an object");
  }
  build(obj);
  return formData;
}
var toFormData_default = toFormData;

// node_modules/axios/lib/helpers/AxiosURLSearchParams.js
function encode(str) {
  const charMap = {
    "!": "%21",
    "'": "%27",
    "(": "%28",
    ")": "%29",
    "~": "%7E",
    "%20": "+",
    "%00": "\0"
  };
  return encodeURIComponent(str).replace(/[!'()~]|%20|%00/g, function replacer(match) {
    return charMap[match];
  });
}
function AxiosURLSearchParams(params, options) {
  this._pairs = [];
  params && toFormData_default(params, this, options);
}
var prototype = AxiosURLSearchParams.prototype;
prototype.append = function append(name, value) {
  this._pairs.push([name, value]);
};
prototype.toString = function toString2(encoder) {
  const _encode = encoder ? function(value) {
    return encoder.call(this, value, encode);
  } : encode;
  return this._pairs.map(function each(pair) {
    return _encode(pair[0]) + "=" + _encode(pair[1]);
  }, "").join("&");
};
var AxiosURLSearchParams_default = AxiosURLSearchParams;

// node_modules/axios/lib/helpers/buildURL.js
function encode2(val) {
  return encodeURIComponent(val).replace(/%3A/gi, ":").replace(/%24/g, "$").replace(/%2C/gi, ",").replace(/%20/g, "+");
}
function buildURL(url, params, options) {
  if (!params) {
    return url;
  }
  const _encode = options && options.encode || encode2;
  const _options = utils_default.isFunction(options) ? {
    serialize: options
  } : options;
  const serializeFn = _options && _options.serialize;
  let serializedParams;
  if (serializeFn) {
    serializedParams = serializeFn(params, _options);
  } else {
    serializedParams = utils_default.isURLSearchParams(params) ? params.toString() : new AxiosURLSearchParams_default(params, _options).toString(_encode);
  }
  if (serializedParams) {
    const hashmarkIndex = url.indexOf("#");
    if (hashmarkIndex !== -1) {
      url = url.slice(0, hashmarkIndex);
    }
    url += (url.indexOf("?") === -1 ? "?" : "&") + serializedParams;
  }
  return url;
}

// node_modules/axios/lib/core/InterceptorManager.js
var InterceptorManager = class {
  constructor() {
    this.handlers = [];
  }
  /**
   * Add a new interceptor to the stack
   *
   * @param {Function} fulfilled The function to handle `then` for a `Promise`
   * @param {Function} rejected The function to handle `reject` for a `Promise`
   * @param {Object} options The options for the interceptor, synchronous and runWhen
   *
   * @return {Number} An ID used to remove interceptor later
   */
  use(fulfilled, rejected, options) {
    this.handlers.push({
      fulfilled,
      rejected,
      synchronous: options ? options.synchronous : false,
      runWhen: options ? options.runWhen : null
    });
    return this.handlers.length - 1;
  }
  /**
   * Remove an interceptor from the stack
   *
   * @param {Number} id The ID that was returned by `use`
   *
   * @returns {void}
   */
  eject(id) {
    if (this.handlers[id]) {
      this.handlers[id] = null;
    }
  }
  /**
   * Clear all interceptors from the stack
   *
   * @returns {void}
   */
  clear() {
    if (this.handlers) {
      this.handlers = [];
    }
  }
  /**
   * Iterate over all the registered interceptors
   *
   * This method is particularly useful for skipping over any
   * interceptors that may have become `null` calling `eject`.
   *
   * @param {Function} fn The function to call for each interceptor
   *
   * @returns {void}
   */
  forEach(fn) {
    utils_default.forEach(this.handlers, function forEachHandler(h) {
      if (h !== null) {
        fn(h);
      }
    });
  }
};
var InterceptorManager_default = InterceptorManager;

// node_modules/axios/lib/defaults/transitional.js
var transitional_default = {
  silentJSONParsing: true,
  forcedJSONParsing: true,
  clarifyTimeoutError: false,
  legacyInterceptorReqResOrdering: true
};

// node_modules/axios/lib/platform/browser/classes/URLSearchParams.js
var URLSearchParams_default = typeof URLSearchParams !== "undefined" ? URLSearchParams : AxiosURLSearchParams_default;

// node_modules/axios/lib/platform/browser/classes/FormData.js
var FormData_default = typeof FormData !== "undefined" ? FormData : null;

// node_modules/axios/lib/platform/browser/classes/Blob.js
var Blob_default = typeof Blob !== "undefined" ? Blob : null;

// node_modules/axios/lib/platform/browser/index.js
var browser_default = {
  isBrowser: true,
  classes: {
    URLSearchParams: URLSearchParams_default,
    FormData: FormData_default,
    Blob: Blob_default
  },
  protocols: ["http", "https", "file", "blob", "url", "data"]
};

// node_modules/axios/lib/platform/common/utils.js
var utils_exports = {};
__export(utils_exports, {
  hasBrowserEnv: () => hasBrowserEnv,
  hasStandardBrowserEnv: () => hasStandardBrowserEnv,
  hasStandardBrowserWebWorkerEnv: () => hasStandardBrowserWebWorkerEnv,
  navigator: () => _navigator,
  origin: () => origin
});
var hasBrowserEnv = typeof window !== "undefined" && typeof document !== "undefined";
var _navigator = typeof navigator === "object" && navigator || void 0;
var hasStandardBrowserEnv = hasBrowserEnv && (!_navigator || ["ReactNative", "NativeScript", "NS"].indexOf(_navigator.product) < 0);
var hasStandardBrowserWebWorkerEnv = (() => {
  return typeof WorkerGlobalScope !== "undefined" && // eslint-disable-next-line no-undef
  self instanceof WorkerGlobalScope && typeof self.importScripts === "function";
})();
var origin = hasBrowserEnv && window.location.href || "http://localhost";

// node_modules/axios/lib/platform/index.js
var platform_default = __spreadValues(__spreadValues({}, utils_exports), browser_default);

// node_modules/axios/lib/helpers/toURLEncodedForm.js
function toURLEncodedForm(data, options) {
  return toFormData_default(data, new platform_default.classes.URLSearchParams(), __spreadValues({
    visitor: function(value, key, path, helpers) {
      if (platform_default.isNode && utils_default.isBuffer(value)) {
        this.append(key, value.toString("base64"));
        return false;
      }
      return helpers.defaultVisitor.apply(this, arguments);
    }
  }, options));
}

// node_modules/axios/lib/helpers/formDataToJSON.js
function parsePropPath(name) {
  return utils_default.matchAll(/\w+|\[(\w*)]/g, name).map((match) => {
    return match[0] === "[]" ? "" : match[1] || match[0];
  });
}
function arrayToObject(arr) {
  const obj = {};
  const keys = Object.keys(arr);
  let i;
  const len = keys.length;
  let key;
  for (i = 0; i < len; i++) {
    key = keys[i];
    obj[key] = arr[key];
  }
  return obj;
}
function formDataToJSON(formData) {
  function buildPath(path, value, target, index) {
    let name = path[index++];
    if (name === "__proto__") return true;
    const isNumericKey = Number.isFinite(+name);
    const isLast = index >= path.length;
    name = !name && utils_default.isArray(target) ? target.length : name;
    if (isLast) {
      if (utils_default.hasOwnProp(target, name)) {
        target[name] = [target[name], value];
      } else {
        target[name] = value;
      }
      return !isNumericKey;
    }
    if (!target[name] || !utils_default.isObject(target[name])) {
      target[name] = [];
    }
    const result = buildPath(path, value, target[name], index);
    if (result && utils_default.isArray(target[name])) {
      target[name] = arrayToObject(target[name]);
    }
    return !isNumericKey;
  }
  if (utils_default.isFormData(formData) && utils_default.isFunction(formData.entries)) {
    const obj = {};
    utils_default.forEachEntry(formData, (name, value) => {
      buildPath(parsePropPath(name), value, obj, 0);
    });
    return obj;
  }
  return null;
}
var formDataToJSON_default = formDataToJSON;

// node_modules/axios/lib/defaults/index.js
function stringifySafely(rawValue, parser, encoder) {
  if (utils_default.isString(rawValue)) {
    try {
      (parser || JSON.parse)(rawValue);
      return utils_default.trim(rawValue);
    } catch (e) {
      if (e.name !== "SyntaxError") {
        throw e;
      }
    }
  }
  return (encoder || JSON.stringify)(rawValue);
}
var defaults = {
  transitional: transitional_default,
  adapter: ["xhr", "http", "fetch"],
  transformRequest: [function transformRequest(data, headers) {
    const contentType = headers.getContentType() || "";
    const hasJSONContentType = contentType.indexOf("application/json") > -1;
    const isObjectPayload = utils_default.isObject(data);
    if (isObjectPayload && utils_default.isHTMLForm(data)) {
      data = new FormData(data);
    }
    const isFormData2 = utils_default.isFormData(data);
    if (isFormData2) {
      return hasJSONContentType ? JSON.stringify(formDataToJSON_default(data)) : data;
    }
    if (utils_default.isArrayBuffer(data) || utils_default.isBuffer(data) || utils_default.isStream(data) || utils_default.isFile(data) || utils_default.isBlob(data) || utils_default.isReadableStream(data)) {
      return data;
    }
    if (utils_default.isArrayBufferView(data)) {
      return data.buffer;
    }
    if (utils_default.isURLSearchParams(data)) {
      headers.setContentType("application/x-www-form-urlencoded;charset=utf-8", false);
      return data.toString();
    }
    let isFileList2;
    if (isObjectPayload) {
      if (contentType.indexOf("application/x-www-form-urlencoded") > -1) {
        return toURLEncodedForm(data, this.formSerializer).toString();
      }
      if ((isFileList2 = utils_default.isFileList(data)) || contentType.indexOf("multipart/form-data") > -1) {
        const _FormData = this.env && this.env.FormData;
        return toFormData_default(
          isFileList2 ? { "files[]": data } : data,
          _FormData && new _FormData(),
          this.formSerializer
        );
      }
    }
    if (isObjectPayload || hasJSONContentType) {
      headers.setContentType("application/json", false);
      return stringifySafely(data);
    }
    return data;
  }],
  transformResponse: [function transformResponse(data) {
    const transitional2 = this.transitional || defaults.transitional;
    const forcedJSONParsing = transitional2 && transitional2.forcedJSONParsing;
    const JSONRequested = this.responseType === "json";
    if (utils_default.isResponse(data) || utils_default.isReadableStream(data)) {
      return data;
    }
    if (data && utils_default.isString(data) && (forcedJSONParsing && !this.responseType || JSONRequested)) {
      const silentJSONParsing = transitional2 && transitional2.silentJSONParsing;
      const strictJSONParsing = !silentJSONParsing && JSONRequested;
      try {
        return JSON.parse(data, this.parseReviver);
      } catch (e) {
        if (strictJSONParsing) {
          if (e.name === "SyntaxError") {
            throw AxiosError_default.from(e, AxiosError_default.ERR_BAD_RESPONSE, this, null, this.response);
          }
          throw e;
        }
      }
    }
    return data;
  }],
  /**
   * A timeout in milliseconds to abort a request. If set to 0 (default) a
   * timeout is not created.
   */
  timeout: 0,
  xsrfCookieName: "XSRF-TOKEN",
  xsrfHeaderName: "X-XSRF-TOKEN",
  maxContentLength: -1,
  maxBodyLength: -1,
  env: {
    FormData: platform_default.classes.FormData,
    Blob: platform_default.classes.Blob
  },
  validateStatus: function validateStatus(status) {
    return status >= 200 && status < 300;
  },
  headers: {
    common: {
      "Accept": "application/json, text/plain, */*",
      "Content-Type": void 0
    }
  }
};
utils_default.forEach(["delete", "get", "head", "post", "put", "patch"], (method) => {
  defaults.headers[method] = {};
});
var defaults_default = defaults;

// node_modules/axios/lib/helpers/parseHeaders.js
var ignoreDuplicateOf = utils_default.toObjectSet([
  "age",
  "authorization",
  "content-length",
  "content-type",
  "etag",
  "expires",
  "from",
  "host",
  "if-modified-since",
  "if-unmodified-since",
  "last-modified",
  "location",
  "max-forwards",
  "proxy-authorization",
  "referer",
  "retry-after",
  "user-agent"
]);
var parseHeaders_default = (rawHeaders) => {
  const parsed = {};
  let key;
  let val;
  let i;
  rawHeaders && rawHeaders.split("\n").forEach(function parser(line) {
    i = line.indexOf(":");
    key = line.substring(0, i).trim().toLowerCase();
    val = line.substring(i + 1).trim();
    if (!key || parsed[key] && ignoreDuplicateOf[key]) {
      return;
    }
    if (key === "set-cookie") {
      if (parsed[key]) {
        parsed[key].push(val);
      } else {
        parsed[key] = [val];
      }
    } else {
      parsed[key] = parsed[key] ? parsed[key] + ", " + val : val;
    }
  });
  return parsed;
};

// node_modules/axios/lib/core/AxiosHeaders.js
var $internals = /* @__PURE__ */ Symbol("internals");
function normalizeHeader(header) {
  return header && String(header).trim().toLowerCase();
}
function normalizeValue(value) {
  if (value === false || value == null) {
    return value;
  }
  return utils_default.isArray(value) ? value.map(normalizeValue) : String(value);
}
function parseTokens(str) {
  const tokens = /* @__PURE__ */ Object.create(null);
  const tokensRE = /([^\s,;=]+)\s*(?:=\s*([^,;]+))?/g;
  let match;
  while (match = tokensRE.exec(str)) {
    tokens[match[1]] = match[2];
  }
  return tokens;
}
var isValidHeaderName = (str) => /^[-_a-zA-Z0-9^`|~,!#$%&'*+.]+$/.test(str.trim());
function matchHeaderValue(context, value, header, filter2, isHeaderNameFilter) {
  if (utils_default.isFunction(filter2)) {
    return filter2.call(this, value, header);
  }
  if (isHeaderNameFilter) {
    value = header;
  }
  if (!utils_default.isString(value)) return;
  if (utils_default.isString(filter2)) {
    return value.indexOf(filter2) !== -1;
  }
  if (utils_default.isRegExp(filter2)) {
    return filter2.test(value);
  }
}
function formatHeader(header) {
  return header.trim().toLowerCase().replace(/([a-z\d])(\w*)/g, (w, char, str) => {
    return char.toUpperCase() + str;
  });
}
function buildAccessors(obj, header) {
  const accessorName = utils_default.toCamelCase(" " + header);
  ["get", "set", "has"].forEach((methodName) => {
    Object.defineProperty(obj, methodName + accessorName, {
      value: function(arg1, arg2, arg3) {
        return this[methodName].call(this, header, arg1, arg2, arg3);
      },
      configurable: true
    });
  });
}
var AxiosHeaders = class {
  constructor(headers) {
    headers && this.set(headers);
  }
  set(header, valueOrRewrite, rewrite) {
    const self2 = this;
    function setHeader(_value, _header, _rewrite) {
      const lHeader = normalizeHeader(_header);
      if (!lHeader) {
        throw new Error("header name must be a non-empty string");
      }
      const key = utils_default.findKey(self2, lHeader);
      if (!key || self2[key] === void 0 || _rewrite === true || _rewrite === void 0 && self2[key] !== false) {
        self2[key || _header] = normalizeValue(_value);
      }
    }
    const setHeaders = (headers, _rewrite) => utils_default.forEach(headers, (_value, _header) => setHeader(_value, _header, _rewrite));
    if (utils_default.isPlainObject(header) || header instanceof this.constructor) {
      setHeaders(header, valueOrRewrite);
    } else if (utils_default.isString(header) && (header = header.trim()) && !isValidHeaderName(header)) {
      setHeaders(parseHeaders_default(header), valueOrRewrite);
    } else if (utils_default.isObject(header) && utils_default.isIterable(header)) {
      let obj = {}, dest, key;
      for (const entry of header) {
        if (!utils_default.isArray(entry)) {
          throw TypeError("Object iterator must return a key-value pair");
        }
        obj[key = entry[0]] = (dest = obj[key]) ? utils_default.isArray(dest) ? [...dest, entry[1]] : [dest, entry[1]] : entry[1];
      }
      setHeaders(obj, valueOrRewrite);
    } else {
      header != null && setHeader(valueOrRewrite, header, rewrite);
    }
    return this;
  }
  get(header, parser) {
    header = normalizeHeader(header);
    if (header) {
      const key = utils_default.findKey(this, header);
      if (key) {
        const value = this[key];
        if (!parser) {
          return value;
        }
        if (parser === true) {
          return parseTokens(value);
        }
        if (utils_default.isFunction(parser)) {
          return parser.call(this, value, key);
        }
        if (utils_default.isRegExp(parser)) {
          return parser.exec(value);
        }
        throw new TypeError("parser must be boolean|regexp|function");
      }
    }
  }
  has(header, matcher) {
    header = normalizeHeader(header);
    if (header) {
      const key = utils_default.findKey(this, header);
      return !!(key && this[key] !== void 0 && (!matcher || matchHeaderValue(this, this[key], key, matcher)));
    }
    return false;
  }
  delete(header, matcher) {
    const self2 = this;
    let deleted = false;
    function deleteHeader(_header) {
      _header = normalizeHeader(_header);
      if (_header) {
        const key = utils_default.findKey(self2, _header);
        if (key && (!matcher || matchHeaderValue(self2, self2[key], key, matcher))) {
          delete self2[key];
          deleted = true;
        }
      }
    }
    if (utils_default.isArray(header)) {
      header.forEach(deleteHeader);
    } else {
      deleteHeader(header);
    }
    return deleted;
  }
  clear(matcher) {
    const keys = Object.keys(this);
    let i = keys.length;
    let deleted = false;
    while (i--) {
      const key = keys[i];
      if (!matcher || matchHeaderValue(this, this[key], key, matcher, true)) {
        delete this[key];
        deleted = true;
      }
    }
    return deleted;
  }
  normalize(format) {
    const self2 = this;
    const headers = {};
    utils_default.forEach(this, (value, header) => {
      const key = utils_default.findKey(headers, header);
      if (key) {
        self2[key] = normalizeValue(value);
        delete self2[header];
        return;
      }
      const normalized = format ? formatHeader(header) : String(header).trim();
      if (normalized !== header) {
        delete self2[header];
      }
      self2[normalized] = normalizeValue(value);
      headers[normalized] = true;
    });
    return this;
  }
  concat(...targets) {
    return this.constructor.concat(this, ...targets);
  }
  toJSON(asStrings) {
    const obj = /* @__PURE__ */ Object.create(null);
    utils_default.forEach(this, (value, header) => {
      value != null && value !== false && (obj[header] = asStrings && utils_default.isArray(value) ? value.join(", ") : value);
    });
    return obj;
  }
  [Symbol.iterator]() {
    return Object.entries(this.toJSON())[Symbol.iterator]();
  }
  toString() {
    return Object.entries(this.toJSON()).map(([header, value]) => header + ": " + value).join("\n");
  }
  getSetCookie() {
    return this.get("set-cookie") || [];
  }
  get [Symbol.toStringTag]() {
    return "AxiosHeaders";
  }
  static from(thing) {
    return thing instanceof this ? thing : new this(thing);
  }
  static concat(first, ...targets) {
    const computed = new this(first);
    targets.forEach((target) => computed.set(target));
    return computed;
  }
  static accessor(header) {
    const internals = this[$internals] = this[$internals] = {
      accessors: {}
    };
    const accessors = internals.accessors;
    const prototype2 = this.prototype;
    function defineAccessor(_header) {
      const lHeader = normalizeHeader(_header);
      if (!accessors[lHeader]) {
        buildAccessors(prototype2, _header);
        accessors[lHeader] = true;
      }
    }
    utils_default.isArray(header) ? header.forEach(defineAccessor) : defineAccessor(header);
    return this;
  }
};
AxiosHeaders.accessor(["Content-Type", "Content-Length", "Accept", "Accept-Encoding", "User-Agent", "Authorization"]);
utils_default.reduceDescriptors(AxiosHeaders.prototype, ({ value }, key) => {
  let mapped = key[0].toUpperCase() + key.slice(1);
  return {
    get: () => value,
    set(headerValue) {
      this[mapped] = headerValue;
    }
  };
});
utils_default.freezeMethods(AxiosHeaders);
var AxiosHeaders_default = AxiosHeaders;

// node_modules/axios/lib/core/transformData.js
function transformData(fns, response) {
  const config = this || defaults_default;
  const context = response || config;
  const headers = AxiosHeaders_default.from(context.headers);
  let data = context.data;
  utils_default.forEach(fns, function transform(fn) {
    data = fn.call(config, data, headers.normalize(), response ? response.status : void 0);
  });
  headers.normalize();
  return data;
}

// node_modules/axios/lib/cancel/isCancel.js
function isCancel(value) {
  return !!(value && value.__CANCEL__);
}

// node_modules/axios/lib/cancel/CanceledError.js
var CanceledError = class extends AxiosError_default {
  /**
   * A `CanceledError` is an object that is thrown when an operation is canceled.
   *
   * @param {string=} message The message.
   * @param {Object=} config The config.
   * @param {Object=} request The request.
   *
   * @returns {CanceledError} The created error.
   */
  constructor(message, config, request) {
    super(message == null ? "canceled" : message, AxiosError_default.ERR_CANCELED, config, request);
    this.name = "CanceledError";
    this.__CANCEL__ = true;
  }
};
var CanceledError_default = CanceledError;

// node_modules/axios/lib/core/settle.js
function settle(resolve, reject, response) {
  const validateStatus2 = response.config.validateStatus;
  if (!response.status || !validateStatus2 || validateStatus2(response.status)) {
    resolve(response);
  } else {
    reject(new AxiosError_default(
      "Request failed with status code " + response.status,
      [AxiosError_default.ERR_BAD_REQUEST, AxiosError_default.ERR_BAD_RESPONSE][Math.floor(response.status / 100) - 4],
      response.config,
      response.request,
      response
    ));
  }
}

// node_modules/axios/lib/helpers/parseProtocol.js
function parseProtocol(url) {
  const match = /^([-+\w]{1,25})(:?\/\/|:)/.exec(url);
  return match && match[1] || "";
}

// node_modules/axios/lib/helpers/speedometer.js
function speedometer(samplesCount, min) {
  samplesCount = samplesCount || 10;
  const bytes = new Array(samplesCount);
  const timestamps = new Array(samplesCount);
  let head = 0;
  let tail = 0;
  let firstSampleTS;
  min = min !== void 0 ? min : 1e3;
  return function push(chunkLength) {
    const now = Date.now();
    const startedAt = timestamps[tail];
    if (!firstSampleTS) {
      firstSampleTS = now;
    }
    bytes[head] = chunkLength;
    timestamps[head] = now;
    let i = tail;
    let bytesCount = 0;
    while (i !== head) {
      bytesCount += bytes[i++];
      i = i % samplesCount;
    }
    head = (head + 1) % samplesCount;
    if (head === tail) {
      tail = (tail + 1) % samplesCount;
    }
    if (now - firstSampleTS < min) {
      return;
    }
    const passed = startedAt && now - startedAt;
    return passed ? Math.round(bytesCount * 1e3 / passed) : void 0;
  };
}
var speedometer_default = speedometer;

// node_modules/axios/lib/helpers/throttle.js
function throttle(fn, freq) {
  let timestamp = 0;
  let threshold = 1e3 / freq;
  let lastArgs;
  let timer;
  const invoke = (args, now = Date.now()) => {
    timestamp = now;
    lastArgs = null;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    fn(...args);
  };
  const throttled = (...args) => {
    const now = Date.now();
    const passed = now - timestamp;
    if (passed >= threshold) {
      invoke(args, now);
    } else {
      lastArgs = args;
      if (!timer) {
        timer = setTimeout(() => {
          timer = null;
          invoke(lastArgs);
        }, threshold - passed);
      }
    }
  };
  const flush = () => lastArgs && invoke(lastArgs);
  return [throttled, flush];
}
var throttle_default = throttle;

// node_modules/axios/lib/helpers/progressEventReducer.js
var progressEventReducer = (listener, isDownloadStream, freq = 3) => {
  let bytesNotified = 0;
  const _speedometer = speedometer_default(50, 250);
  return throttle_default((e) => {
    const loaded = e.loaded;
    const total = e.lengthComputable ? e.total : void 0;
    const progressBytes = loaded - bytesNotified;
    const rate = _speedometer(progressBytes);
    const inRange = loaded <= total;
    bytesNotified = loaded;
    const data = {
      loaded,
      total,
      progress: total ? loaded / total : void 0,
      bytes: progressBytes,
      rate: rate ? rate : void 0,
      estimated: rate && total && inRange ? (total - loaded) / rate : void 0,
      event: e,
      lengthComputable: total != null,
      [isDownloadStream ? "download" : "upload"]: true
    };
    listener(data);
  }, freq);
};
var progressEventDecorator = (total, throttled) => {
  const lengthComputable = total != null;
  return [(loaded) => throttled[0]({
    lengthComputable,
    total,
    loaded
  }), throttled[1]];
};
var asyncDecorator = (fn) => (...args) => utils_default.asap(() => fn(...args));

// node_modules/axios/lib/helpers/isURLSameOrigin.js
var isURLSameOrigin_default = platform_default.hasStandardBrowserEnv ? /* @__PURE__ */ ((origin2, isMSIE) => (url) => {
  url = new URL(url, platform_default.origin);
  return origin2.protocol === url.protocol && origin2.host === url.host && (isMSIE || origin2.port === url.port);
})(
  new URL(platform_default.origin),
  platform_default.navigator && /(msie|trident)/i.test(platform_default.navigator.userAgent)
) : () => true;

// node_modules/axios/lib/helpers/cookies.js
var cookies_default = platform_default.hasStandardBrowserEnv ? (
  // Standard browser envs support document.cookie
  {
    write(name, value, expires, path, domain, secure, sameSite) {
      if (typeof document === "undefined") return;
      const cookie = [`${name}=${encodeURIComponent(value)}`];
      if (utils_default.isNumber(expires)) {
        cookie.push(`expires=${new Date(expires).toUTCString()}`);
      }
      if (utils_default.isString(path)) {
        cookie.push(`path=${path}`);
      }
      if (utils_default.isString(domain)) {
        cookie.push(`domain=${domain}`);
      }
      if (secure === true) {
        cookie.push("secure");
      }
      if (utils_default.isString(sameSite)) {
        cookie.push(`SameSite=${sameSite}`);
      }
      document.cookie = cookie.join("; ");
    },
    read(name) {
      if (typeof document === "undefined") return null;
      const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
      return match ? decodeURIComponent(match[1]) : null;
    },
    remove(name) {
      this.write(name, "", Date.now() - 864e5, "/");
    }
  }
) : (
  // Non-standard browser env (web workers, react-native) lack needed support.
  {
    write() {
    },
    read() {
      return null;
    },
    remove() {
    }
  }
);

// node_modules/axios/lib/helpers/isAbsoluteURL.js
function isAbsoluteURL(url) {
  if (typeof url !== "string") {
    return false;
  }
  return /^([a-z][a-z\d+\-.]*:)?\/\//i.test(url);
}

// node_modules/axios/lib/helpers/combineURLs.js
function combineURLs(baseURL, relativeURL) {
  return relativeURL ? baseURL.replace(/\/?\/$/, "") + "/" + relativeURL.replace(/^\/+/, "") : baseURL;
}

// node_modules/axios/lib/core/buildFullPath.js
function buildFullPath(baseURL, requestedURL, allowAbsoluteUrls) {
  let isRelativeUrl = !isAbsoluteURL(requestedURL);
  if (baseURL && (isRelativeUrl || allowAbsoluteUrls == false)) {
    return combineURLs(baseURL, requestedURL);
  }
  return requestedURL;
}

// node_modules/axios/lib/core/mergeConfig.js
var headersToObject = (thing) => thing instanceof AxiosHeaders_default ? __spreadValues({}, thing) : thing;
function mergeConfig(config1, config2) {
  config2 = config2 || {};
  const config = {};
  function getMergedValue(target, source, prop, caseless) {
    if (utils_default.isPlainObject(target) && utils_default.isPlainObject(source)) {
      return utils_default.merge.call({ caseless }, target, source);
    } else if (utils_default.isPlainObject(source)) {
      return utils_default.merge({}, source);
    } else if (utils_default.isArray(source)) {
      return source.slice();
    }
    return source;
  }
  function mergeDeepProperties(a, b, prop, caseless) {
    if (!utils_default.isUndefined(b)) {
      return getMergedValue(a, b, prop, caseless);
    } else if (!utils_default.isUndefined(a)) {
      return getMergedValue(void 0, a, prop, caseless);
    }
  }
  function valueFromConfig2(a, b) {
    if (!utils_default.isUndefined(b)) {
      return getMergedValue(void 0, b);
    }
  }
  function defaultToConfig2(a, b) {
    if (!utils_default.isUndefined(b)) {
      return getMergedValue(void 0, b);
    } else if (!utils_default.isUndefined(a)) {
      return getMergedValue(void 0, a);
    }
  }
  function mergeDirectKeys(a, b, prop) {
    if (prop in config2) {
      return getMergedValue(a, b);
    } else if (prop in config1) {
      return getMergedValue(void 0, a);
    }
  }
  const mergeMap = {
    url: valueFromConfig2,
    method: valueFromConfig2,
    data: valueFromConfig2,
    baseURL: defaultToConfig2,
    transformRequest: defaultToConfig2,
    transformResponse: defaultToConfig2,
    paramsSerializer: defaultToConfig2,
    timeout: defaultToConfig2,
    timeoutMessage: defaultToConfig2,
    withCredentials: defaultToConfig2,
    withXSRFToken: defaultToConfig2,
    adapter: defaultToConfig2,
    responseType: defaultToConfig2,
    xsrfCookieName: defaultToConfig2,
    xsrfHeaderName: defaultToConfig2,
    onUploadProgress: defaultToConfig2,
    onDownloadProgress: defaultToConfig2,
    decompress: defaultToConfig2,
    maxContentLength: defaultToConfig2,
    maxBodyLength: defaultToConfig2,
    beforeRedirect: defaultToConfig2,
    transport: defaultToConfig2,
    httpAgent: defaultToConfig2,
    httpsAgent: defaultToConfig2,
    cancelToken: defaultToConfig2,
    socketPath: defaultToConfig2,
    responseEncoding: defaultToConfig2,
    validateStatus: mergeDirectKeys,
    headers: (a, b, prop) => mergeDeepProperties(headersToObject(a), headersToObject(b), prop, true)
  };
  utils_default.forEach(
    Object.keys(__spreadValues(__spreadValues({}, config1), config2)),
    function computeConfigValue(prop) {
      if (prop === "__proto__" || prop === "constructor" || prop === "prototype")
        return;
      const merge2 = utils_default.hasOwnProp(mergeMap, prop) ? mergeMap[prop] : mergeDeepProperties;
      const configValue = merge2(config1[prop], config2[prop], prop);
      utils_default.isUndefined(configValue) && merge2 !== mergeDirectKeys || (config[prop] = configValue);
    }
  );
  return config;
}

// node_modules/axios/lib/helpers/resolveConfig.js
var resolveConfig_default = (config) => {
  const newConfig = mergeConfig({}, config);
  let { data, withXSRFToken, xsrfHeaderName, xsrfCookieName, headers, auth } = newConfig;
  newConfig.headers = headers = AxiosHeaders_default.from(headers);
  newConfig.url = buildURL(buildFullPath(newConfig.baseURL, newConfig.url, newConfig.allowAbsoluteUrls), config.params, config.paramsSerializer);
  if (auth) {
    headers.set(
      "Authorization",
      "Basic " + btoa((auth.username || "") + ":" + (auth.password ? unescape(encodeURIComponent(auth.password)) : ""))
    );
  }
  if (utils_default.isFormData(data)) {
    if (platform_default.hasStandardBrowserEnv || platform_default.hasStandardBrowserWebWorkerEnv) {
      headers.setContentType(void 0);
    } else if (utils_default.isFunction(data.getHeaders)) {
      const formHeaders = data.getHeaders();
      const allowedHeaders = ["content-type", "content-length"];
      Object.entries(formHeaders).forEach(([key, val]) => {
        if (allowedHeaders.includes(key.toLowerCase())) {
          headers.set(key, val);
        }
      });
    }
  }
  if (platform_default.hasStandardBrowserEnv) {
    withXSRFToken && utils_default.isFunction(withXSRFToken) && (withXSRFToken = withXSRFToken(newConfig));
    if (withXSRFToken || withXSRFToken !== false && isURLSameOrigin_default(newConfig.url)) {
      const xsrfValue = xsrfHeaderName && xsrfCookieName && cookies_default.read(xsrfCookieName);
      if (xsrfValue) {
        headers.set(xsrfHeaderName, xsrfValue);
      }
    }
  }
  return newConfig;
};

// node_modules/axios/lib/adapters/xhr.js
var isXHRAdapterSupported = typeof XMLHttpRequest !== "undefined";
var xhr_default = isXHRAdapterSupported && function(config) {
  return new Promise(function dispatchXhrRequest(resolve, reject) {
    const _config = resolveConfig_default(config);
    let requestData = _config.data;
    const requestHeaders = AxiosHeaders_default.from(_config.headers).normalize();
    let { responseType, onUploadProgress, onDownloadProgress } = _config;
    let onCanceled;
    let uploadThrottled, downloadThrottled;
    let flushUpload, flushDownload;
    function done() {
      flushUpload && flushUpload();
      flushDownload && flushDownload();
      _config.cancelToken && _config.cancelToken.unsubscribe(onCanceled);
      _config.signal && _config.signal.removeEventListener("abort", onCanceled);
    }
    let request = new XMLHttpRequest();
    request.open(_config.method.toUpperCase(), _config.url, true);
    request.timeout = _config.timeout;
    function onloadend() {
      if (!request) {
        return;
      }
      const responseHeaders = AxiosHeaders_default.from(
        "getAllResponseHeaders" in request && request.getAllResponseHeaders()
      );
      const responseData = !responseType || responseType === "text" || responseType === "json" ? request.responseText : request.response;
      const response = {
        data: responseData,
        status: request.status,
        statusText: request.statusText,
        headers: responseHeaders,
        config,
        request
      };
      settle(function _resolve(value) {
        resolve(value);
        done();
      }, function _reject(err) {
        reject(err);
        done();
      }, response);
      request = null;
    }
    if ("onloadend" in request) {
      request.onloadend = onloadend;
    } else {
      request.onreadystatechange = function handleLoad() {
        if (!request || request.readyState !== 4) {
          return;
        }
        if (request.status === 0 && !(request.responseURL && request.responseURL.indexOf("file:") === 0)) {
          return;
        }
        setTimeout(onloadend);
      };
    }
    request.onabort = function handleAbort() {
      if (!request) {
        return;
      }
      reject(new AxiosError_default("Request aborted", AxiosError_default.ECONNABORTED, config, request));
      request = null;
    };
    request.onerror = function handleError(event) {
      const msg = event && event.message ? event.message : "Network Error";
      const err = new AxiosError_default(msg, AxiosError_default.ERR_NETWORK, config, request);
      err.event = event || null;
      reject(err);
      request = null;
    };
    request.ontimeout = function handleTimeout() {
      let timeoutErrorMessage = _config.timeout ? "timeout of " + _config.timeout + "ms exceeded" : "timeout exceeded";
      const transitional2 = _config.transitional || transitional_default;
      if (_config.timeoutErrorMessage) {
        timeoutErrorMessage = _config.timeoutErrorMessage;
      }
      reject(new AxiosError_default(
        timeoutErrorMessage,
        transitional2.clarifyTimeoutError ? AxiosError_default.ETIMEDOUT : AxiosError_default.ECONNABORTED,
        config,
        request
      ));
      request = null;
    };
    requestData === void 0 && requestHeaders.setContentType(null);
    if ("setRequestHeader" in request) {
      utils_default.forEach(requestHeaders.toJSON(), function setRequestHeader(val, key) {
        request.setRequestHeader(key, val);
      });
    }
    if (!utils_default.isUndefined(_config.withCredentials)) {
      request.withCredentials = !!_config.withCredentials;
    }
    if (responseType && responseType !== "json") {
      request.responseType = _config.responseType;
    }
    if (onDownloadProgress) {
      [downloadThrottled, flushDownload] = progressEventReducer(onDownloadProgress, true);
      request.addEventListener("progress", downloadThrottled);
    }
    if (onUploadProgress && request.upload) {
      [uploadThrottled, flushUpload] = progressEventReducer(onUploadProgress);
      request.upload.addEventListener("progress", uploadThrottled);
      request.upload.addEventListener("loadend", flushUpload);
    }
    if (_config.cancelToken || _config.signal) {
      onCanceled = (cancel) => {
        if (!request) {
          return;
        }
        reject(!cancel || cancel.type ? new CanceledError_default(null, config, request) : cancel);
        request.abort();
        request = null;
      };
      _config.cancelToken && _config.cancelToken.subscribe(onCanceled);
      if (_config.signal) {
        _config.signal.aborted ? onCanceled() : _config.signal.addEventListener("abort", onCanceled);
      }
    }
    const protocol = parseProtocol(_config.url);
    if (protocol && platform_default.protocols.indexOf(protocol) === -1) {
      reject(new AxiosError_default("Unsupported protocol " + protocol + ":", AxiosError_default.ERR_BAD_REQUEST, config));
      return;
    }
    request.send(requestData || null);
  });
};

// node_modules/axios/lib/helpers/composeSignals.js
var composeSignals = (signals, timeout) => {
  const { length } = signals = signals ? signals.filter(Boolean) : [];
  if (timeout || length) {
    let controller = new AbortController();
    let aborted;
    const onabort = function(reason) {
      if (!aborted) {
        aborted = true;
        unsubscribe();
        const err = reason instanceof Error ? reason : this.reason;
        controller.abort(err instanceof AxiosError_default ? err : new CanceledError_default(err instanceof Error ? err.message : err));
      }
    };
    let timer = timeout && setTimeout(() => {
      timer = null;
      onabort(new AxiosError_default(`timeout of ${timeout}ms exceeded`, AxiosError_default.ETIMEDOUT));
    }, timeout);
    const unsubscribe = () => {
      if (signals) {
        timer && clearTimeout(timer);
        timer = null;
        signals.forEach((signal2) => {
          signal2.unsubscribe ? signal2.unsubscribe(onabort) : signal2.removeEventListener("abort", onabort);
        });
        signals = null;
      }
    };
    signals.forEach((signal2) => signal2.addEventListener("abort", onabort));
    const { signal } = controller;
    signal.unsubscribe = () => utils_default.asap(unsubscribe);
    return signal;
  }
};
var composeSignals_default = composeSignals;

// node_modules/axios/lib/helpers/trackStream.js
var streamChunk = function* (chunk, chunkSize) {
  let len = chunk.byteLength;
  if (!chunkSize || len < chunkSize) {
    yield chunk;
    return;
  }
  let pos = 0;
  let end;
  while (pos < len) {
    end = pos + chunkSize;
    yield chunk.slice(pos, end);
    pos = end;
  }
};
var readBytes = function(iterable, chunkSize) {
  return __asyncGenerator(this, null, function* () {
    try {
      for (var iter = __forAwait(readStream(iterable)), more, temp, error; more = !(temp = yield new __await(iter.next())).done; more = false) {
        const chunk = temp.value;
        yield* __yieldStar(streamChunk(chunk, chunkSize));
      }
    } catch (temp) {
      error = [temp];
    } finally {
      try {
        more && (temp = iter.return) && (yield new __await(temp.call(iter)));
      } finally {
        if (error)
          throw error[0];
      }
    }
  });
};
var readStream = function(stream) {
  return __asyncGenerator(this, null, function* () {
    if (stream[Symbol.asyncIterator]) {
      yield* __yieldStar(stream);
      return;
    }
    const reader = stream.getReader();
    try {
      for (; ; ) {
        const { done, value } = yield new __await(reader.read());
        if (done) {
          break;
        }
        yield value;
      }
    } finally {
      yield new __await(reader.cancel());
    }
  });
};
var trackStream = (stream, chunkSize, onProgress, onFinish) => {
  const iterator2 = readBytes(stream, chunkSize);
  let bytes = 0;
  let done;
  let _onFinish = (e) => {
    if (!done) {
      done = true;
      onFinish && onFinish(e);
    }
  };
  return new ReadableStream({
    pull(controller) {
      return __async(this, null, function* () {
        try {
          const { done: done2, value } = yield iterator2.next();
          if (done2) {
            _onFinish();
            controller.close();
            return;
          }
          let len = value.byteLength;
          if (onProgress) {
            let loadedBytes = bytes += len;
            onProgress(loadedBytes);
          }
          controller.enqueue(new Uint8Array(value));
        } catch (err) {
          _onFinish(err);
          throw err;
        }
      });
    },
    cancel(reason) {
      _onFinish(reason);
      return iterator2.return();
    }
  }, {
    highWaterMark: 2
  });
};

// node_modules/axios/lib/adapters/fetch.js
var DEFAULT_CHUNK_SIZE = 64 * 1024;
var { isFunction: isFunction2 } = utils_default;
var globalFetchAPI = (({ Request, Response }) => ({
  Request,
  Response
}))(utils_default.global);
var {
  ReadableStream: ReadableStream2,
  TextEncoder
} = utils_default.global;
var test = (fn, ...args) => {
  try {
    return !!fn(...args);
  } catch (e) {
    return false;
  }
};
var factory = (env) => {
  env = utils_default.merge.call({
    skipUndefined: true
  }, globalFetchAPI, env);
  const { fetch: envFetch, Request, Response } = env;
  const isFetchSupported = envFetch ? isFunction2(envFetch) : typeof fetch === "function";
  const isRequestSupported = isFunction2(Request);
  const isResponseSupported = isFunction2(Response);
  if (!isFetchSupported) {
    return false;
  }
  const isReadableStreamSupported = isFetchSupported && isFunction2(ReadableStream2);
  const encodeText = isFetchSupported && (typeof TextEncoder === "function" ? /* @__PURE__ */ ((encoder) => (str) => encoder.encode(str))(new TextEncoder()) : (str) => __async(null, null, function* () {
    return new Uint8Array(yield new Request(str).arrayBuffer());
  }));
  const supportsRequestStream = isRequestSupported && isReadableStreamSupported && test(() => {
    let duplexAccessed = false;
    const hasContentType = new Request(platform_default.origin, {
      body: new ReadableStream2(),
      method: "POST",
      get duplex() {
        duplexAccessed = true;
        return "half";
      }
    }).headers.has("Content-Type");
    return duplexAccessed && !hasContentType;
  });
  const supportsResponseStream = isResponseSupported && isReadableStreamSupported && test(() => utils_default.isReadableStream(new Response("").body));
  const resolvers = {
    stream: supportsResponseStream && ((res) => res.body)
  };
  isFetchSupported && (() => {
    ["text", "arrayBuffer", "blob", "formData", "stream"].forEach((type) => {
      !resolvers[type] && (resolvers[type] = (res, config) => {
        let method = res && res[type];
        if (method) {
          return method.call(res);
        }
        throw new AxiosError_default(`Response type '${type}' is not supported`, AxiosError_default.ERR_NOT_SUPPORT, config);
      });
    });
  })();
  const getBodyLength = (body) => __async(null, null, function* () {
    if (body == null) {
      return 0;
    }
    if (utils_default.isBlob(body)) {
      return body.size;
    }
    if (utils_default.isSpecCompliantForm(body)) {
      const _request = new Request(platform_default.origin, {
        method: "POST",
        body
      });
      return (yield _request.arrayBuffer()).byteLength;
    }
    if (utils_default.isArrayBufferView(body) || utils_default.isArrayBuffer(body)) {
      return body.byteLength;
    }
    if (utils_default.isURLSearchParams(body)) {
      body = body + "";
    }
    if (utils_default.isString(body)) {
      return (yield encodeText(body)).byteLength;
    }
  });
  const resolveBodyLength = (headers, body) => __async(null, null, function* () {
    const length = utils_default.toFiniteNumber(headers.getContentLength());
    return length == null ? getBodyLength(body) : length;
  });
  return (config) => __async(null, null, function* () {
    let {
      url,
      method,
      data,
      signal,
      cancelToken,
      timeout,
      onDownloadProgress,
      onUploadProgress,
      responseType,
      headers,
      withCredentials = "same-origin",
      fetchOptions
    } = resolveConfig_default(config);
    let _fetch = envFetch || fetch;
    responseType = responseType ? (responseType + "").toLowerCase() : "text";
    let composedSignal = composeSignals_default([signal, cancelToken && cancelToken.toAbortSignal()], timeout);
    let request = null;
    const unsubscribe = composedSignal && composedSignal.unsubscribe && (() => {
      composedSignal.unsubscribe();
    });
    let requestContentLength;
    try {
      if (onUploadProgress && supportsRequestStream && method !== "get" && method !== "head" && (requestContentLength = yield resolveBodyLength(headers, data)) !== 0) {
        let _request = new Request(url, {
          method: "POST",
          body: data,
          duplex: "half"
        });
        let contentTypeHeader;
        if (utils_default.isFormData(data) && (contentTypeHeader = _request.headers.get("content-type"))) {
          headers.setContentType(contentTypeHeader);
        }
        if (_request.body) {
          const [onProgress, flush] = progressEventDecorator(
            requestContentLength,
            progressEventReducer(asyncDecorator(onUploadProgress))
          );
          data = trackStream(_request.body, DEFAULT_CHUNK_SIZE, onProgress, flush);
        }
      }
      if (!utils_default.isString(withCredentials)) {
        withCredentials = withCredentials ? "include" : "omit";
      }
      const isCredentialsSupported = isRequestSupported && "credentials" in Request.prototype;
      const resolvedOptions = __spreadProps(__spreadValues({}, fetchOptions), {
        signal: composedSignal,
        method: method.toUpperCase(),
        headers: headers.normalize().toJSON(),
        body: data,
        duplex: "half",
        credentials: isCredentialsSupported ? withCredentials : void 0
      });
      request = isRequestSupported && new Request(url, resolvedOptions);
      let response = yield isRequestSupported ? _fetch(request, fetchOptions) : _fetch(url, resolvedOptions);
      const isStreamResponse = supportsResponseStream && (responseType === "stream" || responseType === "response");
      if (supportsResponseStream && (onDownloadProgress || isStreamResponse && unsubscribe)) {
        const options = {};
        ["status", "statusText", "headers"].forEach((prop) => {
          options[prop] = response[prop];
        });
        const responseContentLength = utils_default.toFiniteNumber(response.headers.get("content-length"));
        const [onProgress, flush] = onDownloadProgress && progressEventDecorator(
          responseContentLength,
          progressEventReducer(asyncDecorator(onDownloadProgress), true)
        ) || [];
        response = new Response(
          trackStream(response.body, DEFAULT_CHUNK_SIZE, onProgress, () => {
            flush && flush();
            unsubscribe && unsubscribe();
          }),
          options
        );
      }
      responseType = responseType || "text";
      let responseData = yield resolvers[utils_default.findKey(resolvers, responseType) || "text"](response, config);
      !isStreamResponse && unsubscribe && unsubscribe();
      return yield new Promise((resolve, reject) => {
        settle(resolve, reject, {
          data: responseData,
          headers: AxiosHeaders_default.from(response.headers),
          status: response.status,
          statusText: response.statusText,
          config,
          request
        });
      });
    } catch (err) {
      unsubscribe && unsubscribe();
      if (err && err.name === "TypeError" && /Load failed|fetch/i.test(err.message)) {
        throw Object.assign(
          new AxiosError_default("Network Error", AxiosError_default.ERR_NETWORK, config, request, err && err.response),
          {
            cause: err.cause || err
          }
        );
      }
      throw AxiosError_default.from(err, err && err.code, config, request, err && err.response);
    }
  });
};
var seedCache = /* @__PURE__ */ new Map();
var getFetch = (config) => {
  let env = config && config.env || {};
  const { fetch: fetch2, Request, Response } = env;
  const seeds = [
    Request,
    Response,
    fetch2
  ];
  let len = seeds.length, i = len, seed, target, map = seedCache;
  while (i--) {
    seed = seeds[i];
    target = map.get(seed);
    target === void 0 && map.set(seed, target = i ? /* @__PURE__ */ new Map() : factory(env));
    map = target;
  }
  return target;
};
var adapter = getFetch();

// node_modules/axios/lib/adapters/adapters.js
var knownAdapters = {
  http: null_default,
  xhr: xhr_default,
  fetch: {
    get: getFetch
  }
};
utils_default.forEach(knownAdapters, (fn, value) => {
  if (fn) {
    try {
      Object.defineProperty(fn, "name", { value });
    } catch (e) {
    }
    Object.defineProperty(fn, "adapterName", { value });
  }
});
var renderReason = (reason) => `- ${reason}`;
var isResolvedHandle = (adapter2) => utils_default.isFunction(adapter2) || adapter2 === null || adapter2 === false;
function getAdapter(adapters, config) {
  adapters = utils_default.isArray(adapters) ? adapters : [adapters];
  const { length } = adapters;
  let nameOrAdapter;
  let adapter2;
  const rejectedReasons = {};
  for (let i = 0; i < length; i++) {
    nameOrAdapter = adapters[i];
    let id;
    adapter2 = nameOrAdapter;
    if (!isResolvedHandle(nameOrAdapter)) {
      adapter2 = knownAdapters[(id = String(nameOrAdapter)).toLowerCase()];
      if (adapter2 === void 0) {
        throw new AxiosError_default(`Unknown adapter '${id}'`);
      }
    }
    if (adapter2 && (utils_default.isFunction(adapter2) || (adapter2 = adapter2.get(config)))) {
      break;
    }
    rejectedReasons[id || "#" + i] = adapter2;
  }
  if (!adapter2) {
    const reasons = Object.entries(rejectedReasons).map(
      ([id, state]) => `adapter ${id} ` + (state === false ? "is not supported by the environment" : "is not available in the build")
    );
    let s = length ? reasons.length > 1 ? "since :\n" + reasons.map(renderReason).join("\n") : " " + renderReason(reasons[0]) : "as no adapter specified";
    throw new AxiosError_default(
      `There is no suitable adapter to dispatch the request ` + s,
      "ERR_NOT_SUPPORT"
    );
  }
  return adapter2;
}
var adapters_default = {
  /**
   * Resolve an adapter from a list of adapter names or functions.
   * @type {Function}
   */
  getAdapter,
  /**
   * Exposes all known adapters
   * @type {Object<string, Function|Object>}
   */
  adapters: knownAdapters
};

// node_modules/axios/lib/core/dispatchRequest.js
function throwIfCancellationRequested(config) {
  if (config.cancelToken) {
    config.cancelToken.throwIfRequested();
  }
  if (config.signal && config.signal.aborted) {
    throw new CanceledError_default(null, config);
  }
}
function dispatchRequest(config) {
  throwIfCancellationRequested(config);
  config.headers = AxiosHeaders_default.from(config.headers);
  config.data = transformData.call(
    config,
    config.transformRequest
  );
  if (["post", "put", "patch"].indexOf(config.method) !== -1) {
    config.headers.setContentType("application/x-www-form-urlencoded", false);
  }
  const adapter2 = adapters_default.getAdapter(config.adapter || defaults_default.adapter, config);
  return adapter2(config).then(function onAdapterResolution(response) {
    throwIfCancellationRequested(config);
    response.data = transformData.call(
      config,
      config.transformResponse,
      response
    );
    response.headers = AxiosHeaders_default.from(response.headers);
    return response;
  }, function onAdapterRejection(reason) {
    if (!isCancel(reason)) {
      throwIfCancellationRequested(config);
      if (reason && reason.response) {
        reason.response.data = transformData.call(
          config,
          config.transformResponse,
          reason.response
        );
        reason.response.headers = AxiosHeaders_default.from(reason.response.headers);
      }
    }
    return Promise.reject(reason);
  });
}

// node_modules/axios/lib/env/data.js
var VERSION = "1.13.5";

// node_modules/axios/lib/helpers/validator.js
var validators = {};
["object", "boolean", "number", "function", "string", "symbol"].forEach((type, i) => {
  validators[type] = function validator(thing) {
    return typeof thing === type || "a" + (i < 1 ? "n " : " ") + type;
  };
});
var deprecatedWarnings = {};
validators.transitional = function transitional(validator, version, message) {
  function formatMessage(opt, desc) {
    return "[Axios v" + VERSION + "] Transitional option '" + opt + "'" + desc + (message ? ". " + message : "");
  }
  return (value, opt, opts) => {
    if (validator === false) {
      throw new AxiosError_default(
        formatMessage(opt, " has been removed" + (version ? " in " + version : "")),
        AxiosError_default.ERR_DEPRECATED
      );
    }
    if (version && !deprecatedWarnings[opt]) {
      deprecatedWarnings[opt] = true;
      console.warn(
        formatMessage(
          opt,
          " has been deprecated since v" + version + " and will be removed in the near future"
        )
      );
    }
    return validator ? validator(value, opt, opts) : true;
  };
};
validators.spelling = function spelling(correctSpelling) {
  return (value, opt) => {
    console.warn(`${opt} is likely a misspelling of ${correctSpelling}`);
    return true;
  };
};
function assertOptions(options, schema, allowUnknown) {
  if (typeof options !== "object") {
    throw new AxiosError_default("options must be an object", AxiosError_default.ERR_BAD_OPTION_VALUE);
  }
  const keys = Object.keys(options);
  let i = keys.length;
  while (i-- > 0) {
    const opt = keys[i];
    const validator = schema[opt];
    if (validator) {
      const value = options[opt];
      const result = value === void 0 || validator(value, opt, options);
      if (result !== true) {
        throw new AxiosError_default("option " + opt + " must be " + result, AxiosError_default.ERR_BAD_OPTION_VALUE);
      }
      continue;
    }
    if (allowUnknown !== true) {
      throw new AxiosError_default("Unknown option " + opt, AxiosError_default.ERR_BAD_OPTION);
    }
  }
}
var validator_default = {
  assertOptions,
  validators
};

// node_modules/axios/lib/core/Axios.js
var validators2 = validator_default.validators;
var Axios = class {
  constructor(instanceConfig) {
    this.defaults = instanceConfig || {};
    this.interceptors = {
      request: new InterceptorManager_default(),
      response: new InterceptorManager_default()
    };
  }
  /**
   * Dispatch a request
   *
   * @param {String|Object} configOrUrl The config specific for this request (merged with this.defaults)
   * @param {?Object} config
   *
   * @returns {Promise} The Promise to be fulfilled
   */
  request(configOrUrl, config) {
    return __async(this, null, function* () {
      try {
        return yield this._request(configOrUrl, config);
      } catch (err) {
        if (err instanceof Error) {
          let dummy = {};
          Error.captureStackTrace ? Error.captureStackTrace(dummy) : dummy = new Error();
          const stack = dummy.stack ? dummy.stack.replace(/^.+\n/, "") : "";
          try {
            if (!err.stack) {
              err.stack = stack;
            } else if (stack && !String(err.stack).endsWith(stack.replace(/^.+\n.+\n/, ""))) {
              err.stack += "\n" + stack;
            }
          } catch (e) {
          }
        }
        throw err;
      }
    });
  }
  _request(configOrUrl, config) {
    if (typeof configOrUrl === "string") {
      config = config || {};
      config.url = configOrUrl;
    } else {
      config = configOrUrl || {};
    }
    config = mergeConfig(this.defaults, config);
    const { transitional: transitional2, paramsSerializer, headers } = config;
    if (transitional2 !== void 0) {
      validator_default.assertOptions(transitional2, {
        silentJSONParsing: validators2.transitional(validators2.boolean),
        forcedJSONParsing: validators2.transitional(validators2.boolean),
        clarifyTimeoutError: validators2.transitional(validators2.boolean),
        legacyInterceptorReqResOrdering: validators2.transitional(validators2.boolean)
      }, false);
    }
    if (paramsSerializer != null) {
      if (utils_default.isFunction(paramsSerializer)) {
        config.paramsSerializer = {
          serialize: paramsSerializer
        };
      } else {
        validator_default.assertOptions(paramsSerializer, {
          encode: validators2.function,
          serialize: validators2.function
        }, true);
      }
    }
    if (config.allowAbsoluteUrls !== void 0) {
    } else if (this.defaults.allowAbsoluteUrls !== void 0) {
      config.allowAbsoluteUrls = this.defaults.allowAbsoluteUrls;
    } else {
      config.allowAbsoluteUrls = true;
    }
    validator_default.assertOptions(config, {
      baseUrl: validators2.spelling("baseURL"),
      withXsrfToken: validators2.spelling("withXSRFToken")
    }, true);
    config.method = (config.method || this.defaults.method || "get").toLowerCase();
    let contextHeaders = headers && utils_default.merge(
      headers.common,
      headers[config.method]
    );
    headers && utils_default.forEach(
      ["delete", "get", "head", "post", "put", "patch", "common"],
      (method) => {
        delete headers[method];
      }
    );
    config.headers = AxiosHeaders_default.concat(contextHeaders, headers);
    const requestInterceptorChain = [];
    let synchronousRequestInterceptors = true;
    this.interceptors.request.forEach(function unshiftRequestInterceptors(interceptor) {
      if (typeof interceptor.runWhen === "function" && interceptor.runWhen(config) === false) {
        return;
      }
      synchronousRequestInterceptors = synchronousRequestInterceptors && interceptor.synchronous;
      const transitional3 = config.transitional || transitional_default;
      const legacyInterceptorReqResOrdering = transitional3 && transitional3.legacyInterceptorReqResOrdering;
      if (legacyInterceptorReqResOrdering) {
        requestInterceptorChain.unshift(interceptor.fulfilled, interceptor.rejected);
      } else {
        requestInterceptorChain.push(interceptor.fulfilled, interceptor.rejected);
      }
    });
    const responseInterceptorChain = [];
    this.interceptors.response.forEach(function pushResponseInterceptors(interceptor) {
      responseInterceptorChain.push(interceptor.fulfilled, interceptor.rejected);
    });
    let promise;
    let i = 0;
    let len;
    if (!synchronousRequestInterceptors) {
      const chain = [dispatchRequest.bind(this), void 0];
      chain.unshift(...requestInterceptorChain);
      chain.push(...responseInterceptorChain);
      len = chain.length;
      promise = Promise.resolve(config);
      while (i < len) {
        promise = promise.then(chain[i++], chain[i++]);
      }
      return promise;
    }
    len = requestInterceptorChain.length;
    let newConfig = config;
    while (i < len) {
      const onFulfilled = requestInterceptorChain[i++];
      const onRejected = requestInterceptorChain[i++];
      try {
        newConfig = onFulfilled(newConfig);
      } catch (error) {
        onRejected.call(this, error);
        break;
      }
    }
    try {
      promise = dispatchRequest.call(this, newConfig);
    } catch (error) {
      return Promise.reject(error);
    }
    i = 0;
    len = responseInterceptorChain.length;
    while (i < len) {
      promise = promise.then(responseInterceptorChain[i++], responseInterceptorChain[i++]);
    }
    return promise;
  }
  getUri(config) {
    config = mergeConfig(this.defaults, config);
    const fullPath = buildFullPath(config.baseURL, config.url, config.allowAbsoluteUrls);
    return buildURL(fullPath, config.params, config.paramsSerializer);
  }
};
utils_default.forEach(["delete", "get", "head", "options"], function forEachMethodNoData(method) {
  Axios.prototype[method] = function(url, config) {
    return this.request(mergeConfig(config || {}, {
      method,
      url,
      data: (config || {}).data
    }));
  };
});
utils_default.forEach(["post", "put", "patch"], function forEachMethodWithData(method) {
  function generateHTTPMethod(isForm) {
    return function httpMethod(url, data, config) {
      return this.request(mergeConfig(config || {}, {
        method,
        headers: isForm ? {
          "Content-Type": "multipart/form-data"
        } : {},
        url,
        data
      }));
    };
  }
  Axios.prototype[method] = generateHTTPMethod();
  Axios.prototype[method + "Form"] = generateHTTPMethod(true);
});
var Axios_default = Axios;

// node_modules/axios/lib/cancel/CancelToken.js
var CancelToken = class _CancelToken {
  constructor(executor) {
    if (typeof executor !== "function") {
      throw new TypeError("executor must be a function.");
    }
    let resolvePromise;
    this.promise = new Promise(function promiseExecutor(resolve) {
      resolvePromise = resolve;
    });
    const token = this;
    this.promise.then((cancel) => {
      if (!token._listeners) return;
      let i = token._listeners.length;
      while (i-- > 0) {
        token._listeners[i](cancel);
      }
      token._listeners = null;
    });
    this.promise.then = (onfulfilled) => {
      let _resolve;
      const promise = new Promise((resolve) => {
        token.subscribe(resolve);
        _resolve = resolve;
      }).then(onfulfilled);
      promise.cancel = function reject() {
        token.unsubscribe(_resolve);
      };
      return promise;
    };
    executor(function cancel(message, config, request) {
      if (token.reason) {
        return;
      }
      token.reason = new CanceledError_default(message, config, request);
      resolvePromise(token.reason);
    });
  }
  /**
   * Throws a `CanceledError` if cancellation has been requested.
   */
  throwIfRequested() {
    if (this.reason) {
      throw this.reason;
    }
  }
  /**
   * Subscribe to the cancel signal
   */
  subscribe(listener) {
    if (this.reason) {
      listener(this.reason);
      return;
    }
    if (this._listeners) {
      this._listeners.push(listener);
    } else {
      this._listeners = [listener];
    }
  }
  /**
   * Unsubscribe from the cancel signal
   */
  unsubscribe(listener) {
    if (!this._listeners) {
      return;
    }
    const index = this._listeners.indexOf(listener);
    if (index !== -1) {
      this._listeners.splice(index, 1);
    }
  }
  toAbortSignal() {
    const controller = new AbortController();
    const abort = (err) => {
      controller.abort(err);
    };
    this.subscribe(abort);
    controller.signal.unsubscribe = () => this.unsubscribe(abort);
    return controller.signal;
  }
  /**
   * Returns an object that contains a new `CancelToken` and a function that, when called,
   * cancels the `CancelToken`.
   */
  static source() {
    let cancel;
    const token = new _CancelToken(function executor(c) {
      cancel = c;
    });
    return {
      token,
      cancel
    };
  }
};
var CancelToken_default = CancelToken;

// node_modules/axios/lib/helpers/spread.js
function spread(callback) {
  return function wrap(arr) {
    return callback.apply(null, arr);
  };
}

// node_modules/axios/lib/helpers/isAxiosError.js
function isAxiosError(payload) {
  return utils_default.isObject(payload) && payload.isAxiosError === true;
}

// node_modules/axios/lib/helpers/HttpStatusCode.js
var HttpStatusCode = {
  Continue: 100,
  SwitchingProtocols: 101,
  Processing: 102,
  EarlyHints: 103,
  Ok: 200,
  Created: 201,
  Accepted: 202,
  NonAuthoritativeInformation: 203,
  NoContent: 204,
  ResetContent: 205,
  PartialContent: 206,
  MultiStatus: 207,
  AlreadyReported: 208,
  ImUsed: 226,
  MultipleChoices: 300,
  MovedPermanently: 301,
  Found: 302,
  SeeOther: 303,
  NotModified: 304,
  UseProxy: 305,
  Unused: 306,
  TemporaryRedirect: 307,
  PermanentRedirect: 308,
  BadRequest: 400,
  Unauthorized: 401,
  PaymentRequired: 402,
  Forbidden: 403,
  NotFound: 404,
  MethodNotAllowed: 405,
  NotAcceptable: 406,
  ProxyAuthenticationRequired: 407,
  RequestTimeout: 408,
  Conflict: 409,
  Gone: 410,
  LengthRequired: 411,
  PreconditionFailed: 412,
  PayloadTooLarge: 413,
  UriTooLong: 414,
  UnsupportedMediaType: 415,
  RangeNotSatisfiable: 416,
  ExpectationFailed: 417,
  ImATeapot: 418,
  MisdirectedRequest: 421,
  UnprocessableEntity: 422,
  Locked: 423,
  FailedDependency: 424,
  TooEarly: 425,
  UpgradeRequired: 426,
  PreconditionRequired: 428,
  TooManyRequests: 429,
  RequestHeaderFieldsTooLarge: 431,
  UnavailableForLegalReasons: 451,
  InternalServerError: 500,
  NotImplemented: 501,
  BadGateway: 502,
  ServiceUnavailable: 503,
  GatewayTimeout: 504,
  HttpVersionNotSupported: 505,
  VariantAlsoNegotiates: 506,
  InsufficientStorage: 507,
  LoopDetected: 508,
  NotExtended: 510,
  NetworkAuthenticationRequired: 511,
  WebServerIsDown: 521,
  ConnectionTimedOut: 522,
  OriginIsUnreachable: 523,
  TimeoutOccurred: 524,
  SslHandshakeFailed: 525,
  InvalidSslCertificate: 526
};
Object.entries(HttpStatusCode).forEach(([key, value]) => {
  HttpStatusCode[value] = key;
});
var HttpStatusCode_default = HttpStatusCode;

// node_modules/axios/lib/axios.js
function createInstance(defaultConfig) {
  const context = new Axios_default(defaultConfig);
  const instance = bind(Axios_default.prototype.request, context);
  utils_default.extend(instance, Axios_default.prototype, context, { allOwnKeys: true });
  utils_default.extend(instance, context, null, { allOwnKeys: true });
  instance.create = function create(instanceConfig) {
    return createInstance(mergeConfig(defaultConfig, instanceConfig));
  };
  return instance;
}
var axios = createInstance(defaults_default);
axios.Axios = Axios_default;
axios.CanceledError = CanceledError_default;
axios.CancelToken = CancelToken_default;
axios.isCancel = isCancel;
axios.VERSION = VERSION;
axios.toFormData = toFormData_default;
axios.AxiosError = AxiosError_default;
axios.Cancel = axios.CanceledError;
axios.all = function all(promises) {
  return Promise.all(promises);
};
axios.spread = spread;
axios.isAxiosError = isAxiosError;
axios.mergeConfig = mergeConfig;
axios.AxiosHeaders = AxiosHeaders_default;
axios.formToJSON = (thing) => formDataToJSON_default(utils_default.isHTMLForm(thing) ? new FormData(thing) : thing);
axios.getAdapter = adapters_default.getAdapter;
axios.HttpStatusCode = HttpStatusCode_default;
axios.default = axios;
var axios_default = axios;

// node_modules/axios/index.js
var {
  Axios: Axios2,
  AxiosError: AxiosError2,
  CanceledError: CanceledError2,
  isCancel: isCancel2,
  CancelToken: CancelToken2,
  VERSION: VERSION2,
  all: all2,
  Cancel,
  isAxiosError: isAxiosError2,
  spread: spread2,
  toFormData: toFormData2,
  AxiosHeaders: AxiosHeaders2,
  HttpStatusCode: HttpStatusCode2,
  formToJSON,
  getAdapter: getAdapter2,
  mergeConfig: mergeConfig2
} = axios_default;

// node_modules/@polygon.io/client-js/dist/main.js
var import_websocket = __toESM(require_browser());
var I = "https://api.polygon.io".replace(/\/+$/, "");
var Ae = class {
  constructor(a, e = I, i = axios_default) {
    this.basePath = e;
    this.axios = i;
    a && (this.configuration = a, this.basePath = a.basePath ?? e);
  }
};
var _e = class extends Error {
  constructor(e, i) {
    super(i);
    this.field = e;
    this.name = "RequiredError";
  }
};
var v = {};
var q = "https://example.com";
var T = function(l, a, e) {
  if (e == null) throw new _e(a, `Required parameter ${a} was null or undefined when calling ${l}.`);
};
var F = function(l, a, e) {
  return __async(this, null, function* () {
    if (e && e.apiKey) {
      let i = typeof e.apiKey == "function" ? yield e.apiKey(a) : yield e.apiKey;
      l[a] = i;
    }
  });
};
function Pe(l, a, e = "") {
  a != null && (typeof a == "object" ? Array.isArray(a) ? a.forEach((i) => Pe(l, i, e)) : Object.keys(a).forEach((i) => Pe(l, a[i], `${e}${e !== "" ? "." : ""}${i}`)) : l.has(e) ? l.append(e, a) : l.set(e, a));
}
var G = function(l, ...a) {
  let e = new URLSearchParams(l.search);
  Pe(e, a), l.search = e.toString();
};
var B = function(l) {
  return l.pathname + l.search + l.hash;
};
var U = function(l, a, e, i) {
  return (t = a, r = e) => {
    let n = __spreadProps(__spreadValues({}, l.options), { url: (t.defaults.baseURL ? "" : i?.basePath ?? r) + l.url });
    return t.request(n);
  };
};
var Ge = ((n) => (n.Q = "Q", n.T = "T", n.Qa = "QA", n.Ta = "TA", n.Y = "Y", n.Ya = "YA", n))(Ge || {});
var Be = ((a) => (a.Ok = "OK", a))(Be || {});
var Ue = ((a) => (a.Error = "ERROR", a))(Ue || {});
var De = ((a) => (a.Ok = "OK", a))(De || {});
var Me = ((a) => (a.Ok = "OK", a))(Me || {});
var Qe = ((a) => (a.Ok = "OK", a))(Qe || {});
var He = ((a) => (a.Ok = "OK", a))(He || {});
var Ee = ((a) => (a.Ok = "OK", a))(Ee || {});
var ze = ((a) => (a.Ok = "OK", a))(ze || {});
var je = ((a) => (a.Ok = "OK", a))(je || {});
var Ke = ((a) => (a.Ok = "OK", a))(Ke || {});
var $e = ((a) => (a.Ok = "OK", a))($e || {});
var Ne = ((a) => (a.Ok = "OK", a))(Ne || {});
var Le = ((r) => (r.PreOpen = "pre_open", r.Open = "open", r.Close = "close", r.Pause = "pause", r.PostClosePreOpen = "post_close_pre_open", r))(Le || {});
var We = ((r) => (r.PreOpen = "pre_open", r.Open = "open", r.Close = "close", r.Pause = "pause", r.PostClosePreOpen = "post_close_pre_open", r))(We || {});
var Ye = ((e) => (e.ExchangeOnly = "exchange_only", e.ExchangeAndOtc = "exchange_and_otc", e))(Ye || {});
var Xe = ((e) => (e.Delayed = "DELAYED", e.RealTime = "REAL-TIME", e))(Xe || {});
var Je = ((a) => (a.Indices = "indices", a))(Je || {});
var Ze = ((i) => (i.Put = "put", i.Call = "call", i.Other = "other", i))(Ze || {});
var et = ((i) => (i.American = "american", i.European = "european", i.Bermudan = "bermudan", i))(et || {});
var tt = ((e) => (e.Delayed = "DELAYED", e.RealTime = "REAL-TIME", e))(tt || {});
var st = ((e) => (e.Delayed = "DELAYED", e.RealTime = "REAL-TIME", e))(st || {});
var rt = ((t) => (t.Stocks = "stocks", t.Crypto = "crypto", t.Options = "options", t.Fx = "fx", t))(rt || {});
var nt = ((i) => (i.Put = "put", i.Call = "call", i.Other = "other", i))(nt || {});
var it = ((i) => (i.American = "american", i.European = "european", i.Bermudan = "bermudan", i))(it || {});
var at = ((e) => (e.Delayed = "DELAYED", e.RealTime = "REAL-TIME", e))(at || {});
var ot = ((r) => (r.Stocks = "stocks", r.Options = "options", r.Fx = "fx", r.Crypto = "crypto", r.Indices = "indices", r))(ot || {});
var gt = ((i) => (i.Put = "put", i.Call = "call", i.Other = "other", i))(gt || {});
var ut = ((i) => (i.American = "american", i.European = "european", i.Bermudan = "bermudan", i))(ut || {});
var ct = ((e) => (e.Delayed = "DELAYED", e.RealTime = "REAL-TIME", e))(ct || {});
var lt = ((e) => (e.Delayed = "DELAYED", e.RealTime = "REAL-TIME", e))(lt || {});
var dt = ((e) => (e.Delayed = "DELAYED", e.RealTime = "REAL-TIME", e))(dt || {});
var pt = ((a) => (a.Ok = "OK", a))(pt || {});
var mt = ((a) => (a.Ok = "OK", a))(mt || {});
var ft = ((e) => (e.Us = "us", e.Global = "global", e))(ft || {});
var bt = ((r) => (r.Stocks = "stocks", r.Crypto = "crypto", r.Fx = "fx", r.Otc = "otc", r.Indices = "indices", r))(bt || {});
var ht = ((a) => (a.Ok = "OK", a))(ht || {});
var Rt = ((t) => (t.Stocks = "stocks", t.Options = "options", t.Crypto = "crypto", t.Fx = "fx", t))(Rt || {});
var xt = ((i) => (i.Trade = "trade", i.Bbo = "bbo", i.Nbbo = "nbbo", i))(xt || {});
var yt = ((o) => (o.SaleCondition = "sale_condition", o.QuoteCondition = "quote_condition", o.SipGeneratedFlag = "sip_generated_flag", o.FinancialStatusIndicator = "financial_status_indicator", o.ShortSaleRestrictionIndicator = "short_sale_restriction_indicator", o.SettlementCondition = "settlement_condition", o.MarketCondition = "market_condition", o.TradeThruExempt = "trade_thru_exempt", o))(yt || {});
var At = ((t) => (t.Cd = "CD", t.Sc = "SC", t.Lt = "LT", t.St = "ST", t))(At || {});
var _t = ((r) => (r.Stocks = "stocks", r.Options = "options", r.Crypto = "crypto", r.Fx = "fx", r.Futures = "futures", r))(_t || {});
var Ct = ((e) => (e.Us = "us", e.Global = "global", e))(Ct || {});
var Ot = ((i) => (i.Exchange = "exchange", i.Trf = "TRF", i.Sip = "SIP", i))(Ot || {});
var St = ((s) => (s.DirectListingProcess = "direct_listing_process", s.History = "history", s.New = "new", s.Pending = "pending", s.Postponed = "postponed", s.Rumor = "rumor", s.Withdrawn = "withdrawn", s))(St || {});
var Pt = ((i) => (i.Positive = "positive", i.Neutral = "neutral", i.Negative = "negative", i))(Pt || {});
var kt = ((i) => (i.American = "american", i.European = "european", i.Bermudan = "bermudan", i))(kt || {});
var Vt = ((r) => (r.Stocks = "stocks", r.Options = "options", r.Crypto = "crypto", r.Fx = "fx", r.Indices = "indices", r))(Vt || {});
var wt = ((e) => (e.Us = "us", e.Global = "global", e))(wt || {});
var It = ((e) => (e.Us = "us", e.Global = "global", e))(It || {});
var vt = ((r) => (r.Stocks = "stocks", r.Crypto = "crypto", r.Fx = "fx", r.Otc = "otc", r.Indices = "indices", r))(vt || {});
var qt = ((t) => (t.String = "string", t.Int = "int", t.Int64 = "int64", t.Float64 = "float64", t))(qt || {});
var Tt = function(l) {
  return { deprecatedGetCryptoSnapshotTickerBook: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("deprecatedGetCryptoSnapshotTickerBook", "ticker", a);
    let i = "/v2/snapshot/locale/global/markets/crypto/tickers/{ticker}/book".replace("{ticker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), deprecatedGetHistoricCryptoTrades: (_0, _1, _2, _3, _4, ..._5) => __async(null, [_0, _1, _2, _3, _4, ..._5], function* (a, e, i, t, r, n = {}) {
    T("deprecatedGetHistoricCryptoTrades", "from", a), T("deprecatedGetHistoricCryptoTrades", "to", e), T("deprecatedGetHistoricCryptoTrades", "date", i);
    let s = "/v1/historic/crypto/{from}/{to}/{date}".replace("{from}", encodeURIComponent(String(a))).replace("{to}", encodeURIComponent(String(e))).replace("{date}", encodeURIComponent(String(i))), o = new URL(s, q), g;
    l && (g = l.baseOptions);
    let u = __spreadValues(__spreadValues({ method: "GET" }, g), n), c = {}, d = {};
    yield F(d, "apiKey", l), t !== void 0 && (d.offset = t), r !== void 0 && (d.limit = r), G(o, d);
    let p = g && g.headers ? g.headers : {};
    return u.headers = __spreadValues(__spreadValues(__spreadValues({}, c), p), n.headers), { url: B(o), options: u };
  }), deprecatedGetHistoricForexQuotes: (_0, _1, _2, _3, _4, ..._5) => __async(null, [_0, _1, _2, _3, _4, ..._5], function* (a, e, i, t, r, n = {}) {
    T("deprecatedGetHistoricForexQuotes", "from", a), T("deprecatedGetHistoricForexQuotes", "to", e), T("deprecatedGetHistoricForexQuotes", "date", i);
    let s = "/v1/historic/forex/{from}/{to}/{date}".replace("{from}", encodeURIComponent(String(a))).replace("{to}", encodeURIComponent(String(e))).replace("{date}", encodeURIComponent(String(i))), o = new URL(s, q), g;
    l && (g = l.baseOptions);
    let u = __spreadValues(__spreadValues({ method: "GET" }, g), n), c = {}, d = {};
    yield F(d, "apiKey", l), t !== void 0 && (d.offset = t), r !== void 0 && (d.limit = r), G(o, d);
    let p = g && g.headers ? g.headers : {};
    return u.headers = __spreadValues(__spreadValues(__spreadValues({}, c), p), n.headers), { url: B(o), options: u };
  }), deprecatedGetHistoricStocksQuotes: (_0, _1, _2, _3, _4, _5, ..._6) => __async(null, [_0, _1, _2, _3, _4, _5, ..._6], function* (a, e, i, t, r, n, s = {}) {
    T("deprecatedGetHistoricStocksQuotes", "ticker", a), T("deprecatedGetHistoricStocksQuotes", "date", e);
    let o = "/v2/ticks/stocks/nbbo/{ticker}/{date}".replace("{ticker}", encodeURIComponent(String(a))).replace("{date}", encodeURIComponent(String(e))), g = new URL(o, q), u;
    l && (u = l.baseOptions);
    let c = __spreadValues(__spreadValues({ method: "GET" }, u), s), d = {}, p = {};
    yield F(p, "apiKey", l), i !== void 0 && (p.timestamp = i), t !== void 0 && (p.timestampLimit = t), r !== void 0 && (p.reverse = r), n !== void 0 && (p.limit = n), G(g, p);
    let m = u && u.headers ? u.headers : {};
    return c.headers = __spreadValues(__spreadValues(__spreadValues({}, d), m), s.headers), { url: B(g), options: c };
  }), deprecatedGetHistoricStocksTrades: (_0, _1, _2, _3, _4, _5, ..._6) => __async(null, [_0, _1, _2, _3, _4, _5, ..._6], function* (a, e, i, t, r, n, s = {}) {
    T("deprecatedGetHistoricStocksTrades", "ticker", a), T("deprecatedGetHistoricStocksTrades", "date", e);
    let o = "/v2/ticks/stocks/trades/{ticker}/{date}".replace("{ticker}", encodeURIComponent(String(a))).replace("{date}", encodeURIComponent(String(e))), g = new URL(o, q), u;
    l && (u = l.baseOptions);
    let c = __spreadValues(__spreadValues({ method: "GET" }, u), s), d = {}, p = {};
    yield F(p, "apiKey", l), i !== void 0 && (p.timestamp = i), t !== void 0 && (p.timestampLimit = t), r !== void 0 && (p.reverse = r), n !== void 0 && (p.limit = n), G(g, p);
    let m = u && u.headers ? u.headers : {};
    return c.headers = __spreadValues(__spreadValues(__spreadValues({}, d), m), s.headers), { url: B(g), options: c };
  }), getBenzingaV1AnalystInsights: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, ..._44) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, ..._44], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie = {}) {
    let ae = "/benzinga/v1/analyst-insights", oe = new URL(ae, q), re;
    l && (re = l.baseOptions);
    let ge = __spreadValues(__spreadValues({ method: "GET" }, re), ie), ue = {}, S = {};
    yield F(S, "apiKey", l), a !== void 0 && (S.date = a), e !== void 0 && (S["date.any_of"] = e), i !== void 0 && (S["date.gt"] = i), t !== void 0 && (S["date.gte"] = t), r !== void 0 && (S["date.lt"] = r), n !== void 0 && (S["date.lte"] = n), s !== void 0 && (S.ticker = s), o !== void 0 && (S["ticker.any_of"] = o), g !== void 0 && (S["ticker.gt"] = g), u !== void 0 && (S["ticker.gte"] = u), c !== void 0 && (S["ticker.lt"] = c), d !== void 0 && (S["ticker.lte"] = d), p !== void 0 && (S.last_updated = p), m !== void 0 && (S["last_updated.any_of"] = m), f !== void 0 && (S["last_updated.gt"] = f), b !== void 0 && (S["last_updated.gte"] = b), R !== void 0 && (S["last_updated.lt"] = R), x !== void 0 && (S["last_updated.lte"] = x), y !== void 0 && (S.firm = y), h !== void 0 && (S["firm.any_of"] = h), _ !== void 0 && (S["firm.gt"] = _), A !== void 0 && (S["firm.gte"] = A), C !== void 0 && (S["firm.lt"] = C), V !== void 0 && (S["firm.lte"] = V), Q !== void 0 && (S.rating_action = Q), H !== void 0 && (S["rating_action.any_of"] = H), P !== void 0 && (S["rating_action.gt"] = P), z !== void 0 && (S["rating_action.gte"] = z), w !== void 0 && (S["rating_action.lt"] = w), j !== void 0 && (S["rating_action.lte"] = j), N !== void 0 && (S.benzinga_firm_id = N), L !== void 0 && (S["benzinga_firm_id.any_of"] = L), O !== void 0 && (S["benzinga_firm_id.gt"] = O), E !== void 0 && (S["benzinga_firm_id.gte"] = E), Y !== void 0 && (S["benzinga_firm_id.lt"] = Y), K !== void 0 && (S["benzinga_firm_id.lte"] = K), X !== void 0 && (S.benzinga_rating_id = X), Z !== void 0 && (S["benzinga_rating_id.any_of"] = Z), J !== void 0 && (S["benzinga_rating_id.gt"] = J), ee !== void 0 && (S["benzinga_rating_id.gte"] = ee), te !== void 0 && (S["benzinga_rating_id.lt"] = te), $ !== void 0 && (S["benzinga_rating_id.lte"] = $), se !== void 0 && (S.limit = se), ne !== void 0 && (S.sort = ne), G(oe, S);
    let ce = re && re.headers ? re.headers : {};
    return ge.headers = __spreadValues(__spreadValues(__spreadValues({}, ue), ce), ie.headers), { url: B(oe), options: ge };
  }), getBenzingaV1Analysts: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, ..._26) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, ..._26], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P = {}) {
    let z = "/benzinga/v1/analysts", w = new URL(z, q), j;
    l && (j = l.baseOptions);
    let N = __spreadValues(__spreadValues({ method: "GET" }, j), P), L = {}, O = {};
    yield F(O, "apiKey", l), a !== void 0 && (O.benzinga_id = a), e !== void 0 && (O["benzinga_id.any_of"] = e), i !== void 0 && (O["benzinga_id.gt"] = i), t !== void 0 && (O["benzinga_id.gte"] = t), r !== void 0 && (O["benzinga_id.lt"] = r), n !== void 0 && (O["benzinga_id.lte"] = n), s !== void 0 && (O.benzinga_firm_id = s), o !== void 0 && (O["benzinga_firm_id.any_of"] = o), g !== void 0 && (O["benzinga_firm_id.gt"] = g), u !== void 0 && (O["benzinga_firm_id.gte"] = u), c !== void 0 && (O["benzinga_firm_id.lt"] = c), d !== void 0 && (O["benzinga_firm_id.lte"] = d), p !== void 0 && (O.firm_name = p), m !== void 0 && (O["firm_name.any_of"] = m), f !== void 0 && (O["firm_name.gt"] = f), b !== void 0 && (O["firm_name.gte"] = b), R !== void 0 && (O["firm_name.lt"] = R), x !== void 0 && (O["firm_name.lte"] = x), y !== void 0 && (O.full_name = y), h !== void 0 && (O["full_name.any_of"] = h), _ !== void 0 && (O["full_name.gt"] = _), A !== void 0 && (O["full_name.gte"] = A), C !== void 0 && (O["full_name.lt"] = C), V !== void 0 && (O["full_name.lte"] = V), Q !== void 0 && (O.limit = Q), H !== void 0 && (O.sort = H), G(w, O);
    let E = j && j.headers ? j.headers : {};
    return N.headers = __spreadValues(__spreadValues(__spreadValues({}, L), E), P.headers), { url: B(w), options: N };
  }), getBenzingaV1ConsensusRatings: (_0, _1, _2, _3, _4, _5, _6, ..._7) => __async(null, [_0, _1, _2, _3, _4, _5, _6, ..._7], function* (a, e, i, t, r, n, s, o = {}) {
    T("getBenzingaV1ConsensusRatings", "ticker", a);
    let g = "/benzinga/v1/consensus-ratings/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), u = new URL(g, q), c;
    l && (c = l.baseOptions);
    let d = __spreadValues(__spreadValues({ method: "GET" }, c), o), p = {}, m = {};
    yield F(m, "apiKey", l), e !== void 0 && (m.date = e), i !== void 0 && (m["date.gt"] = i), t !== void 0 && (m["date.gte"] = t), r !== void 0 && (m["date.lt"] = r), n !== void 0 && (m["date.lte"] = n), s !== void 0 && (m.limit = s), G(u, m);
    let f = c && c.headers ? c.headers : {};
    return d.headers = __spreadValues(__spreadValues(__spreadValues({}, p), f), o.headers), { url: B(u), options: d };
  }), getBenzingaV1Earnings: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, _44, _45, _46, _47, _48, _49, _50, _51, _52, _53, _54, _55, ..._56) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, _44, _45, _46, _47, _48, _49, _50, _51, _52, _53, _54, _55, ..._56], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W = {}) {
    let be = "/benzinga/v1/earnings", he = new URL(be, q), fe;
    l && (fe = l.baseOptions);
    let Re = __spreadValues(__spreadValues({ method: "GET" }, fe), W), ye = {}, k = {};
    yield F(k, "apiKey", l), a !== void 0 && (k.date = a), e !== void 0 && (k["date.any_of"] = e), i !== void 0 && (k["date.gt"] = i), t !== void 0 && (k["date.gte"] = t), r !== void 0 && (k["date.lt"] = r), n !== void 0 && (k["date.lte"] = n), s !== void 0 && (k.ticker = s), o !== void 0 && (k["ticker.any_of"] = o), g !== void 0 && (k["ticker.gt"] = g), u !== void 0 && (k["ticker.gte"] = u), c !== void 0 && (k["ticker.lt"] = c), d !== void 0 && (k["ticker.lte"] = d), p !== void 0 && (k.importance = p), m !== void 0 && (k["importance.any_of"] = m), f !== void 0 && (k["importance.gt"] = f), b !== void 0 && (k["importance.gte"] = b), R !== void 0 && (k["importance.lt"] = R), x !== void 0 && (k["importance.lte"] = x), y !== void 0 && (k.last_updated = y), h !== void 0 && (k["last_updated.any_of"] = h), _ !== void 0 && (k["last_updated.gt"] = _), A !== void 0 && (k["last_updated.gte"] = A), C !== void 0 && (k["last_updated.lt"] = C), V !== void 0 && (k["last_updated.lte"] = V), Q !== void 0 && (k.date_status = Q), H !== void 0 && (k["date_status.any_of"] = H), P !== void 0 && (k["date_status.gt"] = P), z !== void 0 && (k["date_status.gte"] = z), w !== void 0 && (k["date_status.lt"] = w), j !== void 0 && (k["date_status.lte"] = j), N !== void 0 && (k.eps_surprise_percent = N), L !== void 0 && (k["eps_surprise_percent.any_of"] = L), O !== void 0 && (k["eps_surprise_percent.gt"] = O), E !== void 0 && (k["eps_surprise_percent.gte"] = E), Y !== void 0 && (k["eps_surprise_percent.lt"] = Y), K !== void 0 && (k["eps_surprise_percent.lte"] = K), X !== void 0 && (k.revenue_surprise_percent = X), Z !== void 0 && (k["revenue_surprise_percent.any_of"] = Z), J !== void 0 && (k["revenue_surprise_percent.gt"] = J), ee !== void 0 && (k["revenue_surprise_percent.gte"] = ee), te !== void 0 && (k["revenue_surprise_percent.lt"] = te), $ !== void 0 && (k["revenue_surprise_percent.lte"] = $), se !== void 0 && (k.fiscal_year = se), ne !== void 0 && (k["fiscal_year.any_of"] = ne), ie !== void 0 && (k["fiscal_year.gt"] = ie), ae !== void 0 && (k["fiscal_year.gte"] = ae), oe !== void 0 && (k["fiscal_year.lt"] = oe), re !== void 0 && (k["fiscal_year.lte"] = re), ge !== void 0 && (k.fiscal_period = ge), ue !== void 0 && (k["fiscal_period.any_of"] = ue), S !== void 0 && (k["fiscal_period.gt"] = S), ce !== void 0 && (k["fiscal_period.gte"] = ce), de !== void 0 && (k["fiscal_period.lt"] = de), le !== void 0 && (k["fiscal_period.lte"] = le), pe !== void 0 && (k.limit = pe), me !== void 0 && (k.sort = me), G(he, k);
    let Se = fe && fe.headers ? fe.headers : {};
    return Re.headers = __spreadValues(__spreadValues(__spreadValues({}, ye), Se), W.headers), { url: B(he), options: Re };
  }), getBenzingaV1Firms: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    let u = "/benzinga/v1/firms", c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), a !== void 0 && (f.benzinga_id = a), e !== void 0 && (f["benzinga_id.any_of"] = e), i !== void 0 && (f["benzinga_id.gt"] = i), t !== void 0 && (f["benzinga_id.gte"] = t), r !== void 0 && (f["benzinga_id.lt"] = r), n !== void 0 && (f["benzinga_id.lte"] = n), s !== void 0 && (f.limit = s), o !== void 0 && (f.sort = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getBenzingaV1Guidance: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, ..._44) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, ..._44], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie = {}) {
    let ae = "/benzinga/v1/guidance", oe = new URL(ae, q), re;
    l && (re = l.baseOptions);
    let ge = __spreadValues(__spreadValues({ method: "GET" }, re), ie), ue = {}, S = {};
    yield F(S, "apiKey", l), a !== void 0 && (S.date = a), e !== void 0 && (S["date.any_of"] = e), i !== void 0 && (S["date.gt"] = i), t !== void 0 && (S["date.gte"] = t), r !== void 0 && (S["date.lt"] = r), n !== void 0 && (S["date.lte"] = n), s !== void 0 && (S.ticker = s), o !== void 0 && (S["ticker.any_of"] = o), g !== void 0 && (S["ticker.gt"] = g), u !== void 0 && (S["ticker.gte"] = u), c !== void 0 && (S["ticker.lt"] = c), d !== void 0 && (S["ticker.lte"] = d), p !== void 0 && (S.positioning = p), m !== void 0 && (S["positioning.any_of"] = m), f !== void 0 && (S["positioning.gt"] = f), b !== void 0 && (S["positioning.gte"] = b), R !== void 0 && (S["positioning.lt"] = R), x !== void 0 && (S["positioning.lte"] = x), y !== void 0 && (S.importance = y), h !== void 0 && (S["importance.any_of"] = h), _ !== void 0 && (S["importance.gt"] = _), A !== void 0 && (S["importance.gte"] = A), C !== void 0 && (S["importance.lt"] = C), V !== void 0 && (S["importance.lte"] = V), Q !== void 0 && (S.last_updated = Q), H !== void 0 && (S["last_updated.any_of"] = H), P !== void 0 && (S["last_updated.gt"] = P), z !== void 0 && (S["last_updated.gte"] = z), w !== void 0 && (S["last_updated.lt"] = w), j !== void 0 && (S["last_updated.lte"] = j), N !== void 0 && (S.fiscal_year = N), L !== void 0 && (S["fiscal_year.any_of"] = L), O !== void 0 && (S["fiscal_year.gt"] = O), E !== void 0 && (S["fiscal_year.gte"] = E), Y !== void 0 && (S["fiscal_year.lt"] = Y), K !== void 0 && (S["fiscal_year.lte"] = K), X !== void 0 && (S.fiscal_period = X), Z !== void 0 && (S["fiscal_period.any_of"] = Z), J !== void 0 && (S["fiscal_period.gt"] = J), ee !== void 0 && (S["fiscal_period.gte"] = ee), te !== void 0 && (S["fiscal_period.lt"] = te), $ !== void 0 && (S["fiscal_period.lte"] = $), se !== void 0 && (S.limit = se), ne !== void 0 && (S.sort = ne), G(oe, S);
    let ce = re && re.headers ? re.headers : {};
    return ge.headers = __spreadValues(__spreadValues(__spreadValues({}, ue), ce), ie.headers), { url: B(oe), options: ge };
  }), getBenzingaV1News: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, ..._29) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, ..._29], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j = {}) {
    let N = "/benzinga/v1/news", L = new URL(N, q), O;
    l && (O = l.baseOptions);
    let E = __spreadValues(__spreadValues({ method: "GET" }, O), j), Y = {}, K = {};
    yield F(K, "apiKey", l), a !== void 0 && (K.published = a), e !== void 0 && (K["published.any_of"] = e), i !== void 0 && (K["published.gt"] = i), t !== void 0 && (K["published.gte"] = t), r !== void 0 && (K["published.lt"] = r), n !== void 0 && (K["published.lte"] = n), s !== void 0 && (K.last_updated = s), o !== void 0 && (K["last_updated.any_of"] = o), g !== void 0 && (K["last_updated.gt"] = g), u !== void 0 && (K["last_updated.gte"] = u), c !== void 0 && (K["last_updated.lt"] = c), d !== void 0 && (K["last_updated.lte"] = d), p !== void 0 && (K.tickers = p), m !== void 0 && (K["tickers.all_of"] = m), f !== void 0 && (K["tickers.any_of"] = f), b !== void 0 && (K.channels = b), R !== void 0 && (K["channels.all_of"] = R), x !== void 0 && (K["channels.any_of"] = x), y !== void 0 && (K.tags = y), h !== void 0 && (K["tags.all_of"] = h), _ !== void 0 && (K["tags.any_of"] = _), A !== void 0 && (K.author = A), C !== void 0 && (K["author.any_of"] = C), V !== void 0 && (K["author.gt"] = V), Q !== void 0 && (K["author.gte"] = Q), H !== void 0 && (K["author.lt"] = H), P !== void 0 && (K["author.lte"] = P), z !== void 0 && (K.limit = z), w !== void 0 && (K.sort = w), G(L, K);
    let X = O && O.headers ? O.headers : {};
    return E.headers = __spreadValues(__spreadValues(__spreadValues({}, Y), X), j.headers), { url: B(L), options: E };
  }), getBenzingaV1Ratings: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, _44, _45, _46, _47, _48, _49, _50, _51, _52, _53, _54, _55, ..._56) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, _44, _45, _46, _47, _48, _49, _50, _51, _52, _53, _54, _55, ..._56], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W = {}) {
    let be = "/benzinga/v1/ratings", he = new URL(be, q), fe;
    l && (fe = l.baseOptions);
    let Re = __spreadValues(__spreadValues({ method: "GET" }, fe), W), ye = {}, k = {};
    yield F(k, "apiKey", l), a !== void 0 && (k.date = a), e !== void 0 && (k["date.any_of"] = e), i !== void 0 && (k["date.gt"] = i), t !== void 0 && (k["date.gte"] = t), r !== void 0 && (k["date.lt"] = r), n !== void 0 && (k["date.lte"] = n), s !== void 0 && (k.ticker = s), o !== void 0 && (k["ticker.any_of"] = o), g !== void 0 && (k["ticker.gt"] = g), u !== void 0 && (k["ticker.gte"] = u), c !== void 0 && (k["ticker.lt"] = c), d !== void 0 && (k["ticker.lte"] = d), p !== void 0 && (k.importance = p), m !== void 0 && (k["importance.any_of"] = m), f !== void 0 && (k["importance.gt"] = f), b !== void 0 && (k["importance.gte"] = b), R !== void 0 && (k["importance.lt"] = R), x !== void 0 && (k["importance.lte"] = x), y !== void 0 && (k.last_updated = y), h !== void 0 && (k["last_updated.any_of"] = h), _ !== void 0 && (k["last_updated.gt"] = _), A !== void 0 && (k["last_updated.gte"] = A), C !== void 0 && (k["last_updated.lt"] = C), V !== void 0 && (k["last_updated.lte"] = V), Q !== void 0 && (k.rating_action = Q), H !== void 0 && (k["rating_action.any_of"] = H), P !== void 0 && (k["rating_action.gt"] = P), z !== void 0 && (k["rating_action.gte"] = z), w !== void 0 && (k["rating_action.lt"] = w), j !== void 0 && (k["rating_action.lte"] = j), N !== void 0 && (k.price_target_action = N), L !== void 0 && (k["price_target_action.any_of"] = L), O !== void 0 && (k["price_target_action.gt"] = O), E !== void 0 && (k["price_target_action.gte"] = E), Y !== void 0 && (k["price_target_action.lt"] = Y), K !== void 0 && (k["price_target_action.lte"] = K), X !== void 0 && (k.benzinga_id = X), Z !== void 0 && (k["benzinga_id.any_of"] = Z), J !== void 0 && (k["benzinga_id.gt"] = J), ee !== void 0 && (k["benzinga_id.gte"] = ee), te !== void 0 && (k["benzinga_id.lt"] = te), $ !== void 0 && (k["benzinga_id.lte"] = $), se !== void 0 && (k.benzinga_analyst_id = se), ne !== void 0 && (k["benzinga_analyst_id.any_of"] = ne), ie !== void 0 && (k["benzinga_analyst_id.gt"] = ie), ae !== void 0 && (k["benzinga_analyst_id.gte"] = ae), oe !== void 0 && (k["benzinga_analyst_id.lt"] = oe), re !== void 0 && (k["benzinga_analyst_id.lte"] = re), ge !== void 0 && (k.benzinga_firm_id = ge), ue !== void 0 && (k["benzinga_firm_id.any_of"] = ue), S !== void 0 && (k["benzinga_firm_id.gt"] = S), ce !== void 0 && (k["benzinga_firm_id.gte"] = ce), de !== void 0 && (k["benzinga_firm_id.lt"] = de), le !== void 0 && (k["benzinga_firm_id.lte"] = le), pe !== void 0 && (k.limit = pe), me !== void 0 && (k.sort = me), G(he, k);
    let Se = fe && fe.headers ? fe.headers : {};
    return Re.headers = __spreadValues(__spreadValues(__spreadValues({}, ye), Se), W.headers), { url: B(he), options: Re };
  }), getCryptoAggregates: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    T("getCryptoAggregates", "cryptoTicker", a), T("getCryptoAggregates", "multiplier", e), T("getCryptoAggregates", "timespan", i), T("getCryptoAggregates", "from", t), T("getCryptoAggregates", "to", r);
    let u = "/v2/aggs/ticker/{cryptoTicker}/range/{multiplier}/{timespan}/{from}/{to}".replace("{cryptoTicker}", encodeURIComponent(String(a))).replace("{multiplier}", encodeURIComponent(String(e))).replace("{timespan}", encodeURIComponent(String(i))).replace("{from}", encodeURIComponent(String(t))).replace("{to}", encodeURIComponent(String(r))), c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), n !== void 0 && (f.adjusted = n), s !== void 0 && (f.sort = s), o !== void 0 && (f.limit = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getCryptoEMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12], function* (a, e, i, t, r, n, s, o, g, u, c, d, p = {}) {
    T("getCryptoEMA", "cryptoTicker", a);
    let m = "/v1/indicators/ema/{cryptoTicker}".replace("{cryptoTicker}", encodeURIComponent(String(a))), f = new URL(m, q), b;
    l && (b = l.baseOptions);
    let R = __spreadValues(__spreadValues({ method: "GET" }, b), p), x = {}, y = {};
    yield F(y, "apiKey", l), e !== void 0 && (y.timestamp = e), i !== void 0 && (y.timespan = i), t !== void 0 && (y.window = t), r !== void 0 && (y.series_type = r), n !== void 0 && (y.expand_underlying = n), s !== void 0 && (y.order = s), o !== void 0 && (y.limit = o), g !== void 0 && (y["timestamp.gte"] = g), u !== void 0 && (y["timestamp.gt"] = u), c !== void 0 && (y["timestamp.lte"] = c), d !== void 0 && (y["timestamp.lt"] = d), G(f, y);
    let h = b && b.headers ? b.headers : {};
    return R.headers = __spreadValues(__spreadValues(__spreadValues({}, x), h), p.headers), { url: B(f), options: R };
  }), getCryptoMACD: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, ..._14) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, ..._14], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f = {}) {
    T("getCryptoMACD", "cryptoTicker", a);
    let b = "/v1/indicators/macd/{cryptoTicker}".replace("{cryptoTicker}", encodeURIComponent(String(a))), R = new URL(b, q), x;
    l && (x = l.baseOptions);
    let y = __spreadValues(__spreadValues({ method: "GET" }, x), f), h = {}, _ = {};
    yield F(_, "apiKey", l), e !== void 0 && (_.timestamp = e), i !== void 0 && (_.timespan = i), t !== void 0 && (_.short_window = t), r !== void 0 && (_.long_window = r), n !== void 0 && (_.signal_window = n), s !== void 0 && (_.series_type = s), o !== void 0 && (_.expand_underlying = o), g !== void 0 && (_.order = g), u !== void 0 && (_.limit = u), c !== void 0 && (_["timestamp.gte"] = c), d !== void 0 && (_["timestamp.gt"] = d), p !== void 0 && (_["timestamp.lte"] = p), m !== void 0 && (_["timestamp.lt"] = m), G(R, _);
    let A = x && x.headers ? x.headers : {};
    return y.headers = __spreadValues(__spreadValues(__spreadValues({}, h), A), f.headers), { url: B(R), options: y };
  }), getCryptoOpenClose: (_0, _1, _2, _3, ..._4) => __async(null, [_0, _1, _2, _3, ..._4], function* (a, e, i, t, r = {}) {
    T("getCryptoOpenClose", "from", a), T("getCryptoOpenClose", "to", e), T("getCryptoOpenClose", "date", i);
    let n = "/v1/open-close/crypto/{from}/{to}/{date}".replace("{from}", encodeURIComponent(String(a))).replace("{to}", encodeURIComponent(String(e))).replace("{date}", encodeURIComponent(String(i))), s = new URL(n, q), o;
    l && (o = l.baseOptions);
    let g = __spreadValues(__spreadValues({ method: "GET" }, o), r), u = {}, c = {};
    yield F(c, "apiKey", l), t !== void 0 && (c.adjusted = t), G(s, c);
    let d = o && o.headers ? o.headers : {};
    return g.headers = __spreadValues(__spreadValues(__spreadValues({}, u), d), r.headers), { url: B(s), options: g };
  }), getCryptoRSI: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12], function* (a, e, i, t, r, n, s, o, g, u, c, d, p = {}) {
    T("getCryptoRSI", "cryptoTicker", a);
    let m = "/v1/indicators/rsi/{cryptoTicker}".replace("{cryptoTicker}", encodeURIComponent(String(a))), f = new URL(m, q), b;
    l && (b = l.baseOptions);
    let R = __spreadValues(__spreadValues({ method: "GET" }, b), p), x = {}, y = {};
    yield F(y, "apiKey", l), e !== void 0 && (y.timestamp = e), i !== void 0 && (y.timespan = i), t !== void 0 && (y.window = t), r !== void 0 && (y.series_type = r), n !== void 0 && (y.expand_underlying = n), s !== void 0 && (y.order = s), o !== void 0 && (y.limit = o), g !== void 0 && (y["timestamp.gte"] = g), u !== void 0 && (y["timestamp.gt"] = u), c !== void 0 && (y["timestamp.lte"] = c), d !== void 0 && (y["timestamp.lt"] = d), G(f, y);
    let h = b && b.headers ? b.headers : {};
    return R.headers = __spreadValues(__spreadValues(__spreadValues({}, x), h), p.headers), { url: B(f), options: R };
  }), getCryptoSMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12], function* (a, e, i, t, r, n, s, o, g, u, c, d, p = {}) {
    T("getCryptoSMA", "cryptoTicker", a);
    let m = "/v1/indicators/sma/{cryptoTicker}".replace("{cryptoTicker}", encodeURIComponent(String(a))), f = new URL(m, q), b;
    l && (b = l.baseOptions);
    let R = __spreadValues(__spreadValues({ method: "GET" }, b), p), x = {}, y = {};
    yield F(y, "apiKey", l), e !== void 0 && (y.timestamp = e), i !== void 0 && (y.timespan = i), t !== void 0 && (y.window = t), r !== void 0 && (y.series_type = r), n !== void 0 && (y.expand_underlying = n), s !== void 0 && (y.order = s), o !== void 0 && (y.limit = o), g !== void 0 && (y["timestamp.gte"] = g), u !== void 0 && (y["timestamp.gt"] = u), c !== void 0 && (y["timestamp.lte"] = c), d !== void 0 && (y["timestamp.lt"] = d), G(f, y);
    let h = b && b.headers ? b.headers : {};
    return R.headers = __spreadValues(__spreadValues(__spreadValues({}, x), h), p.headers), { url: B(f), options: R };
  }), getCryptoSnapshotDirection: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getCryptoSnapshotDirection", "direction", a);
    let i = "/v2/snapshot/locale/global/markets/crypto/{direction}".replace("{direction}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getCryptoSnapshotTicker: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getCryptoSnapshotTicker", "ticker", a);
    let i = "/v2/snapshot/locale/global/markets/crypto/tickers/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getCryptoSnapshotTickers: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    let i = "/v2/snapshot/locale/global/markets/crypto/tickers", t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), a && (o.tickers = a), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getCryptoTrades: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    T("getCryptoTrades", "cryptoTicker", a);
    let c = "/v3/trades/{cryptoTicker}".replace("{cryptoTicker}", encodeURIComponent(String(a))), d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), e !== void 0 && (b.timestamp = e), i !== void 0 && (b["timestamp.gte"] = i), t !== void 0 && (b["timestamp.gt"] = t), r !== void 0 && (b["timestamp.lte"] = r), n !== void 0 && (b["timestamp.lt"] = n), s !== void 0 && (b.order = s), o !== void 0 && (b.limit = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getCurrencyConversion: (_0, _1, _2, _3, ..._4) => __async(null, [_0, _1, _2, _3, ..._4], function* (a, e, i, t, r = {}) {
    T("getCurrencyConversion", "from", a), T("getCurrencyConversion", "to", e);
    let n = "/v1/conversion/{from}/{to}".replace("{from}", encodeURIComponent(String(a))).replace("{to}", encodeURIComponent(String(e))), s = new URL(n, q), o;
    l && (o = l.baseOptions);
    let g = __spreadValues(__spreadValues({ method: "GET" }, o), r), u = {}, c = {};
    yield F(c, "apiKey", l), i !== void 0 && (c.amount = i), t !== void 0 && (c.precision = t), G(s, c);
    let d = o && o.headers ? o.headers : {};
    return g.headers = __spreadValues(__spreadValues(__spreadValues({}, u), d), r.headers), { url: B(s), options: g };
  }), getEvents: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getEvents", "id", a);
    let t = "/vX/reference/tickers/{id}/events".replace("{id}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.types = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getFedV1Inflation: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    let u = "/fed/v1/inflation", c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), a !== void 0 && (f.date = a), e !== void 0 && (f["date.any_of"] = e), i !== void 0 && (f["date.gt"] = i), t !== void 0 && (f["date.gte"] = t), r !== void 0 && (f["date.lt"] = r), n !== void 0 && (f["date.lte"] = n), s !== void 0 && (f.limit = s), o !== void 0 && (f.sort = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getFedV1InflationExpectations: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    let u = "/fed/v1/inflation-expectations", c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), a !== void 0 && (f.date = a), e !== void 0 && (f["date.any_of"] = e), i !== void 0 && (f["date.gt"] = i), t !== void 0 && (f["date.gte"] = t), r !== void 0 && (f["date.lt"] = r), n !== void 0 && (f["date.lte"] = n), s !== void 0 && (f.limit = s), o !== void 0 && (f.sort = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getFedV1TreasuryYields: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    let u = "/fed/v1/treasury-yields", c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), a !== void 0 && (f.date = a), e !== void 0 && (f["date.any_of"] = e), i !== void 0 && (f["date.gt"] = i), t !== void 0 && (f["date.gte"] = t), r !== void 0 && (f["date.lt"] = r), n !== void 0 && (f["date.lte"] = n), s !== void 0 && (f.limit = s), o !== void 0 && (f.sort = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getForexAggregates: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    T("getForexAggregates", "forexTicker", a), T("getForexAggregates", "multiplier", e), T("getForexAggregates", "timespan", i), T("getForexAggregates", "from", t), T("getForexAggregates", "to", r);
    let u = "/v2/aggs/ticker/{forexTicker}/range/{multiplier}/{timespan}/{from}/{to}".replace("{forexTicker}", encodeURIComponent(String(a))).replace("{multiplier}", encodeURIComponent(String(e))).replace("{timespan}", encodeURIComponent(String(i))).replace("{from}", encodeURIComponent(String(t))).replace("{to}", encodeURIComponent(String(r))), c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), n !== void 0 && (f.adjusted = n), s !== void 0 && (f.sort = s), o !== void 0 && (f.limit = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getForexEMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getForexEMA", "fxTicker", a);
    let f = "/v1/indicators/ema/{fxTicker}".replace("{fxTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getForexMACD: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b = {}) {
    T("getForexMACD", "fxTicker", a);
    let R = "/v1/indicators/macd/{fxTicker}".replace("{fxTicker}", encodeURIComponent(String(a))), x = new URL(R, q), y;
    l && (y = l.baseOptions);
    let h = __spreadValues(__spreadValues({ method: "GET" }, y), b), _ = {}, A = {};
    yield F(A, "apiKey", l), e !== void 0 && (A.timestamp = e), i !== void 0 && (A.timespan = i), t !== void 0 && (A.adjusted = t), r !== void 0 && (A.short_window = r), n !== void 0 && (A.long_window = n), s !== void 0 && (A.signal_window = s), o !== void 0 && (A.series_type = o), g !== void 0 && (A.expand_underlying = g), u !== void 0 && (A.order = u), c !== void 0 && (A.limit = c), d !== void 0 && (A["timestamp.gte"] = d), p !== void 0 && (A["timestamp.gt"] = p), m !== void 0 && (A["timestamp.lte"] = m), f !== void 0 && (A["timestamp.lt"] = f), G(x, A);
    let C = y && y.headers ? y.headers : {};
    return h.headers = __spreadValues(__spreadValues(__spreadValues({}, _), C), b.headers), { url: B(x), options: h };
  }), getForexQuotes: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    T("getForexQuotes", "fxTicker", a);
    let c = "/v3/quotes/{fxTicker}".replace("{fxTicker}", encodeURIComponent(String(a))), d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), e !== void 0 && (b.timestamp = e), i !== void 0 && (b["timestamp.gte"] = i), t !== void 0 && (b["timestamp.gt"] = t), r !== void 0 && (b["timestamp.lte"] = r), n !== void 0 && (b["timestamp.lt"] = n), s !== void 0 && (b.order = s), o !== void 0 && (b.limit = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getForexRSI: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getForexRSI", "fxTicker", a);
    let f = "/v1/indicators/rsi/{fxTicker}".replace("{fxTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getForexSMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getForexSMA", "fxTicker", a);
    let f = "/v1/indicators/sma/{fxTicker}".replace("{fxTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getForexSnapshotDirection: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getForexSnapshotDirection", "direction", a);
    let i = "/v2/snapshot/locale/global/markets/forex/{direction}".replace("{direction}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getForexSnapshotTicker: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getForexSnapshotTicker", "ticker", a);
    let i = "/v2/snapshot/locale/global/markets/forex/tickers/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getForexSnapshotTickers: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    let i = "/v2/snapshot/locale/global/markets/forex/tickers", t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), a && (o.tickers = a), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getFuturesAggregates: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    T("getFuturesAggregates", "ticker", a);
    let c = "/futures/vX/aggs/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), e !== void 0 && (b.resolution = e), i !== void 0 && (b.window_start = i), t !== void 0 && (b.limit = t), r !== void 0 && (b["window_start.gte"] = r), n !== void 0 && (b["window_start.gt"] = n), s !== void 0 && (b["window_start.lte"] = s), o !== void 0 && (b["window_start.lt"] = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getFuturesContractDetails: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getFuturesContractDetails", "ticker", a);
    let t = "/futures/vX/contracts/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.as_of = e instanceof Date ? e.toISOString().substring(0, 10) : e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getFuturesContracts: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    let u = "/futures/vX/contracts", c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), a !== void 0 && (f.product_code = a), e !== void 0 && (f.first_trade_date = e instanceof Date ? e.toISOString().substring(0, 10) : e), i !== void 0 && (f.last_trade_date = i instanceof Date ? i.toISOString().substring(0, 10) : i), t !== void 0 && (f.as_of = t instanceof Date ? t.toISOString().substring(0, 10) : t), r !== void 0 && (f.active = r), n !== void 0 && (f.type = n), s !== void 0 && (f.limit = s), o !== void 0 && (f.sort = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getFuturesDailySchedules: (_0, _1, _2, _3, ..._4) => __async(null, [_0, _1, _2, _3, ..._4], function* (a, e, i, t, r = {}) {
    let n = "/futures/vX/schedules", s = new URL(n, q), o;
    l && (o = l.baseOptions);
    let g = __spreadValues(__spreadValues({ method: "GET" }, o), r), u = {}, c = {};
    yield F(c, "apiKey", l), a !== void 0 && (c.session_end_date = a instanceof Date ? a.toISOString().substring(0, 10) : a), e !== void 0 && (c.trading_venue = e), i !== void 0 && (c.limit = i), t !== void 0 && (c.sort = t), G(s, c);
    let d = o && o.headers ? o.headers : {};
    return g.headers = __spreadValues(__spreadValues(__spreadValues({}, u), d), r.headers), { url: B(s), options: g };
  }), getFuturesMarketStatuses: (_0, _1, _2, _3, ..._4) => __async(null, [_0, _1, _2, _3, ..._4], function* (a, e, i, t, r = {}) {
    let n = "/futures/vX/market-status", s = new URL(n, q), o;
    l && (o = l.baseOptions);
    let g = __spreadValues(__spreadValues({ method: "GET" }, o), r), u = {}, c = {};
    yield F(c, "apiKey", l), a !== void 0 && (c["product_code.any_of"] = a), e !== void 0 && (c.product_code = e), i !== void 0 && (c.limit = i), t !== void 0 && (c.sort = t), G(s, c);
    let d = o && o.headers ? o.headers : {};
    return g.headers = __spreadValues(__spreadValues(__spreadValues({}, u), d), r.headers), { url: B(s), options: g };
  }), getFuturesProductDetails: (_0, _1, _2, ..._3) => __async(null, [_0, _1, _2, ..._3], function* (a, e, i, t = {}) {
    T("getFuturesProductDetails", "productCode", a);
    let r = "/futures/vX/products/{product_code}".replace("{product_code}", encodeURIComponent(String(a))), n = new URL(r, q), s;
    l && (s = l.baseOptions);
    let o = __spreadValues(__spreadValues({ method: "GET" }, s), t), g = {}, u = {};
    yield F(u, "apiKey", l), e !== void 0 && (u.type = e), i !== void 0 && (u.as_of = i instanceof Date ? i.toISOString().substring(0, 10) : i), G(n, u);
    let c = s && s.headers ? s.headers : {};
    return o.headers = __spreadValues(__spreadValues(__spreadValues({}, g), c), t.headers), { url: B(n), options: o };
  }), getFuturesProductSchedules: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    T("getFuturesProductSchedules", "productCode", a);
    let u = "/futures/vX/products/{product_code}/schedules".replace("{product_code}", encodeURIComponent(String(a))), c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), e !== void 0 && (f.session_end_date = e instanceof Date ? e.toISOString().substring(0, 10) : e), i !== void 0 && (f.limit = i), t !== void 0 && (f["session_end_date.gte"] = t instanceof Date ? t.toISOString().substring(0, 10) : t), r !== void 0 && (f["session_end_date.gt"] = r instanceof Date ? r.toISOString().substring(0, 10) : r), n !== void 0 && (f["session_end_date.lte"] = n instanceof Date ? n.toISOString().substring(0, 10) : n), s !== void 0 && (f["session_end_date.lt"] = s instanceof Date ? s.toISOString().substring(0, 10) : s), o !== void 0 && (f.sort = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getFuturesProducts: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, ..._11) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, ..._11], function* (a, e, i, t, r, n, s, o, g, u, c, d = {}) {
    let p = "/futures/vX/products", m = new URL(p, q), f;
    l && (f = l.baseOptions);
    let b = __spreadValues(__spreadValues({ method: "GET" }, f), d), R = {}, x = {};
    yield F(x, "apiKey", l), a !== void 0 && (x.name = a), e !== void 0 && (x.as_of = e instanceof Date ? e.toISOString().substring(0, 10) : e), i !== void 0 && (x.trading_venue = i), t !== void 0 && (x.sector = t), r !== void 0 && (x.sub_sector = r), n !== void 0 && (x.asset_class = n), s !== void 0 && (x.asset_sub_class = s), o !== void 0 && (x.type = o), g !== void 0 && (x.limit = g), u !== void 0 && (x["name.search"] = u), c !== void 0 && (x.sort = c), G(m, x);
    let y = f && f.headers ? f.headers : {};
    return b.headers = __spreadValues(__spreadValues(__spreadValues({}, R), y), d.headers), { url: B(m), options: b };
  }), getFuturesQuotes: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getFuturesQuotes", "ticker", a);
    let f = "/futures/vX/quotes/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.session_end_date = i), t !== void 0 && (h.limit = t), r !== void 0 && (h["timestamp.gte"] = r), n !== void 0 && (h["timestamp.gt"] = n), s !== void 0 && (h["timestamp.lte"] = s), o !== void 0 && (h["timestamp.lt"] = o), g !== void 0 && (h["session_end_date.gte"] = g), u !== void 0 && (h["session_end_date.gt"] = u), c !== void 0 && (h["session_end_date.lte"] = c), d !== void 0 && (h["session_end_date.lt"] = d), p !== void 0 && (h.sort = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getFuturesTrades: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getFuturesTrades", "ticker", a);
    let f = "/futures/vX/trades/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.session_end_date = i), t !== void 0 && (h.limit = t), r !== void 0 && (h["timestamp.gte"] = r), n !== void 0 && (h["timestamp.gt"] = n), s !== void 0 && (h["timestamp.lte"] = s), o !== void 0 && (h["timestamp.lt"] = o), g !== void 0 && (h["session_end_date.gte"] = g), u !== void 0 && (h["session_end_date.gt"] = u), c !== void 0 && (h["session_end_date.lte"] = c), d !== void 0 && (h["session_end_date.lt"] = d), p !== void 0 && (h.sort = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getGroupedCryptoAggregates: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getGroupedCryptoAggregates", "date", a);
    let t = "/v2/aggs/grouped/locale/global/market/crypto/{date}".replace("{date}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.adjusted = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getGroupedForexAggregates: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getGroupedForexAggregates", "date", a);
    let t = "/v2/aggs/grouped/locale/global/market/fx/{date}".replace("{date}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.adjusted = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getGroupedStocksAggregates: (_0, _1, _2, ..._3) => __async(null, [_0, _1, _2, ..._3], function* (a, e, i, t = {}) {
    T("getGroupedStocksAggregates", "date", a);
    let r = "/v2/aggs/grouped/locale/us/market/stocks/{date}".replace("{date}", encodeURIComponent(String(a))), n = new URL(r, q), s;
    l && (s = l.baseOptions);
    let o = __spreadValues(__spreadValues({ method: "GET" }, s), t), g = {}, u = {};
    yield F(u, "apiKey", l), e !== void 0 && (u.adjusted = e), i !== void 0 && (u.include_otc = i), G(n, u);
    let c = s && s.headers ? s.headers : {};
    return o.headers = __spreadValues(__spreadValues(__spreadValues({}, g), c), t.headers), { url: B(n), options: o };
  }), getIndicesAggregates: (_0, _1, _2, _3, _4, _5, _6, ..._7) => __async(null, [_0, _1, _2, _3, _4, _5, _6, ..._7], function* (a, e, i, t, r, n, s, o = {}) {
    T("getIndicesAggregates", "indicesTicker", a), T("getIndicesAggregates", "multiplier", e), T("getIndicesAggregates", "timespan", i), T("getIndicesAggregates", "from", t), T("getIndicesAggregates", "to", r);
    let g = "/v2/aggs/ticker/{indicesTicker}/range/{multiplier}/{timespan}/{from}/{to}".replace("{indicesTicker}", encodeURIComponent(String(a))).replace("{multiplier}", encodeURIComponent(String(e))).replace("{timespan}", encodeURIComponent(String(i))).replace("{from}", encodeURIComponent(String(t))).replace("{to}", encodeURIComponent(String(r))), u = new URL(g, q), c;
    l && (c = l.baseOptions);
    let d = __spreadValues(__spreadValues({ method: "GET" }, c), o), p = {}, m = {};
    yield F(m, "apiKey", l), n !== void 0 && (m.sort = n), s !== void 0 && (m.limit = s), G(u, m);
    let f = c && c.headers ? c.headers : {};
    return d.headers = __spreadValues(__spreadValues(__spreadValues({}, p), f), o.headers), { url: B(u), options: d };
  }), getIndicesEMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getIndicesEMA", "indicesTicker", a);
    let f = "/v1/indicators/ema/{indicesTicker}".replace("{indicesTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getIndicesMACD: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b = {}) {
    T("getIndicesMACD", "indicesTicker", a);
    let R = "/v1/indicators/macd/{indicesTicker}".replace("{indicesTicker}", encodeURIComponent(String(a))), x = new URL(R, q), y;
    l && (y = l.baseOptions);
    let h = __spreadValues(__spreadValues({ method: "GET" }, y), b), _ = {}, A = {};
    yield F(A, "apiKey", l), e !== void 0 && (A.timestamp = e), i !== void 0 && (A.timespan = i), t !== void 0 && (A.adjusted = t), r !== void 0 && (A.short_window = r), n !== void 0 && (A.long_window = n), s !== void 0 && (A.signal_window = s), o !== void 0 && (A.series_type = o), g !== void 0 && (A.expand_underlying = g), u !== void 0 && (A.order = u), c !== void 0 && (A.limit = c), d !== void 0 && (A["timestamp.gte"] = d), p !== void 0 && (A["timestamp.gt"] = p), m !== void 0 && (A["timestamp.lte"] = m), f !== void 0 && (A["timestamp.lt"] = f), G(x, A);
    let C = y && y.headers ? y.headers : {};
    return h.headers = __spreadValues(__spreadValues(__spreadValues({}, _), C), b.headers), { url: B(x), options: h };
  }), getIndicesOpenClose: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getIndicesOpenClose", "indicesTicker", a), T("getIndicesOpenClose", "date", e);
    let t = "/v1/open-close/{indicesTicker}/{date}".replace("{indicesTicker}", encodeURIComponent(String(a))).replace("{date}", encodeURIComponent(String(e))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getIndicesRSI: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getIndicesRSI", "indicesTicker", a);
    let f = "/v1/indicators/rsi/{indicesTicker}".replace("{indicesTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getIndicesSMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getIndicesSMA", "indicesTicker", a);
    let f = "/v1/indicators/sma/{indicesTicker}".replace("{indicesTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getIndicesSnapshot: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    let c = "/v3/snapshot/indices", d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), a !== void 0 && (b["ticker.any_of"] = a), e !== void 0 && (b.ticker = e), i !== void 0 && (b["ticker.gte"] = i), t !== void 0 && (b["ticker.gt"] = t), r !== void 0 && (b["ticker.lte"] = r), n !== void 0 && (b["ticker.lt"] = n), s !== void 0 && (b.order = s), o !== void 0 && (b.limit = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getLastCryptoTrade: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getLastCryptoTrade", "from", a), T("getLastCryptoTrade", "to", e);
    let t = "/v1/last/crypto/{from}/{to}".replace("{from}", encodeURIComponent(String(a))).replace("{to}", encodeURIComponent(String(e))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getLastCurrencyQuote: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getLastCurrencyQuote", "from", a), T("getLastCurrencyQuote", "to", e);
    let t = "/v1/last_quote/currencies/{from}/{to}".replace("{from}", encodeURIComponent(String(a))).replace("{to}", encodeURIComponent(String(e))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getLastOptionsTrade: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getLastOptionsTrade", "optionsTicker", a);
    let i = "/v2/last/trade/{optionsTicker}".replace("{optionsTicker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getLastStocksQuote: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getLastStocksQuote", "stocksTicker", a);
    let i = "/v2/last/nbbo/{stocksTicker}".replace("{stocksTicker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getLastStocksTrade: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getLastStocksTrade", "stocksTicker", a);
    let i = "/v2/last/trade/{stocksTicker}".replace("{stocksTicker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getMarketHolidays: (..._0) => __async(null, [..._0], function* (a = {}) {
    let e = "/v1/marketstatus/upcoming", i = new URL(e, q), t;
    l && (t = l.baseOptions);
    let r = __spreadValues(__spreadValues({ method: "GET" }, t), a), n = {}, s = {};
    yield F(s, "apiKey", l), G(i, s);
    let o = t && t.headers ? t.headers : {};
    return r.headers = __spreadValues(__spreadValues(__spreadValues({}, n), o), a.headers), { url: B(i), options: r };
  }), getMarketStatus: (..._0) => __async(null, [..._0], function* (a = {}) {
    let e = "/v1/marketstatus/now", i = new URL(e, q), t;
    l && (t = l.baseOptions);
    let r = __spreadValues(__spreadValues({ method: "GET" }, t), a), n = {}, s = {};
    yield F(s, "apiKey", l), G(i, s);
    let o = t && t.headers ? t.headers : {};
    return r.headers = __spreadValues(__spreadValues(__spreadValues({}, n), o), a.headers), { url: B(i), options: r };
  }), getOptionContract: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getOptionContract", "underlyingAsset", a), T("getOptionContract", "optionContract", e);
    let t = "/v3/snapshot/options/{underlyingAsset}/{optionContract}".replace("{underlyingAsset}", encodeURIComponent(String(a))).replace("{optionContract}", encodeURIComponent(String(e))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getOptionsAggregates: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    T("getOptionsAggregates", "optionsTicker", a), T("getOptionsAggregates", "multiplier", e), T("getOptionsAggregates", "timespan", i), T("getOptionsAggregates", "from", t), T("getOptionsAggregates", "to", r);
    let u = "/v2/aggs/ticker/{optionsTicker}/range/{multiplier}/{timespan}/{from}/{to}".replace("{optionsTicker}", encodeURIComponent(String(a))).replace("{multiplier}", encodeURIComponent(String(e))).replace("{timespan}", encodeURIComponent(String(i))).replace("{from}", encodeURIComponent(String(t))).replace("{to}", encodeURIComponent(String(r))), c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), n !== void 0 && (f.adjusted = n), s !== void 0 && (f.sort = s), o !== void 0 && (f.limit = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getOptionsChain: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b = {}) {
    T("getOptionsChain", "underlyingAsset", a);
    let R = "/v3/snapshot/options/{underlyingAsset}".replace("{underlyingAsset}", encodeURIComponent(String(a))), x = new URL(R, q), y;
    l && (y = l.baseOptions);
    let h = __spreadValues(__spreadValues({ method: "GET" }, y), b), _ = {}, A = {};
    yield F(A, "apiKey", l), e !== void 0 && (A.strike_price = e), i !== void 0 && (A.expiration_date = i), t !== void 0 && (A.contract_type = t), r !== void 0 && (A["strike_price.gte"] = r), n !== void 0 && (A["strike_price.gt"] = n), s !== void 0 && (A["strike_price.lte"] = s), o !== void 0 && (A["strike_price.lt"] = o), g !== void 0 && (A["expiration_date.gte"] = g), u !== void 0 && (A["expiration_date.gt"] = u), c !== void 0 && (A["expiration_date.lte"] = c), d !== void 0 && (A["expiration_date.lt"] = d), p !== void 0 && (A.order = p), m !== void 0 && (A.limit = m), f !== void 0 && (A.sort = f), G(x, A);
    let C = y && y.headers ? y.headers : {};
    return h.headers = __spreadValues(__spreadValues(__spreadValues({}, _), C), b.headers), { url: B(x), options: h };
  }), getOptionsContract: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getOptionsContract", "optionsTicker", a);
    let t = "/v3/reference/options/contracts/{options_ticker}".replace("{options_ticker}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.as_of = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getOptionsEMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getOptionsEMA", "optionsTicker", a);
    let f = "/v1/indicators/ema/{optionsTicker}".replace("{optionsTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getOptionsMACD: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b = {}) {
    T("getOptionsMACD", "optionsTicker", a);
    let R = "/v1/indicators/macd/{optionsTicker}".replace("{optionsTicker}", encodeURIComponent(String(a))), x = new URL(R, q), y;
    l && (y = l.baseOptions);
    let h = __spreadValues(__spreadValues({ method: "GET" }, y), b), _ = {}, A = {};
    yield F(A, "apiKey", l), e !== void 0 && (A.timestamp = e), i !== void 0 && (A.timespan = i), t !== void 0 && (A.adjusted = t), r !== void 0 && (A.short_window = r), n !== void 0 && (A.long_window = n), s !== void 0 && (A.signal_window = s), o !== void 0 && (A.series_type = o), g !== void 0 && (A.expand_underlying = g), u !== void 0 && (A.order = u), c !== void 0 && (A.limit = c), d !== void 0 && (A["timestamp.gte"] = d), p !== void 0 && (A["timestamp.gt"] = p), m !== void 0 && (A["timestamp.lte"] = m), f !== void 0 && (A["timestamp.lt"] = f), G(x, A);
    let C = y && y.headers ? y.headers : {};
    return h.headers = __spreadValues(__spreadValues(__spreadValues({}, _), C), b.headers), { url: B(x), options: h };
  }), getOptionsOpenClose: (_0, _1, _2, ..._3) => __async(null, [_0, _1, _2, ..._3], function* (a, e, i, t = {}) {
    T("getOptionsOpenClose", "optionsTicker", a), T("getOptionsOpenClose", "date", e);
    let r = "/v1/open-close/{optionsTicker}/{date}".replace("{optionsTicker}", encodeURIComponent(String(a))).replace("{date}", encodeURIComponent(String(e))), n = new URL(r, q), s;
    l && (s = l.baseOptions);
    let o = __spreadValues(__spreadValues({ method: "GET" }, s), t), g = {}, u = {};
    yield F(u, "apiKey", l), i !== void 0 && (u.adjusted = i), G(n, u);
    let c = s && s.headers ? s.headers : {};
    return o.headers = __spreadValues(__spreadValues(__spreadValues({}, g), c), t.headers), { url: B(n), options: o };
  }), getOptionsQuotes: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    T("getOptionsQuotes", "optionsTicker", a);
    let c = "/v3/quotes/{optionsTicker}".replace("{optionsTicker}", encodeURIComponent(String(a))), d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), e !== void 0 && (b.timestamp = e), i !== void 0 && (b["timestamp.gte"] = i), t !== void 0 && (b["timestamp.gt"] = t), r !== void 0 && (b["timestamp.lte"] = r), n !== void 0 && (b["timestamp.lt"] = n), s !== void 0 && (b.order = s), o !== void 0 && (b.limit = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getOptionsRSI: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getOptionsRSI", "optionsTicker", a);
    let f = "/v1/indicators/rsi/{optionsTicker}".replace("{optionsTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getOptionsSMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getOptionsSMA", "optionsTicker", a);
    let f = "/v1/indicators/sma/{optionsTicker}".replace("{optionsTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getOptionsTrades: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    T("getOptionsTrades", "optionsTicker", a);
    let c = "/v3/trades/{optionsTicker}".replace("{optionsTicker}", encodeURIComponent(String(a))), d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), e !== void 0 && (b.timestamp = e), i !== void 0 && (b["timestamp.gte"] = i), t !== void 0 && (b["timestamp.gt"] = t), r !== void 0 && (b["timestamp.lte"] = r), n !== void 0 && (b["timestamp.lt"] = n), s !== void 0 && (b.order = s), o !== void 0 && (b.limit = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getPreviousCryptoAggregates: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getPreviousCryptoAggregates", "cryptoTicker", a);
    let t = "/v2/aggs/ticker/{cryptoTicker}/prev".replace("{cryptoTicker}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.adjusted = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getPreviousForexAggregates: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getPreviousForexAggregates", "forexTicker", a);
    let t = "/v2/aggs/ticker/{forexTicker}/prev".replace("{forexTicker}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.adjusted = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getPreviousIndicesAggregates: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getPreviousIndicesAggregates", "indicesTicker", a);
    let i = "/v2/aggs/ticker/{indicesTicker}/prev".replace("{indicesTicker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getPreviousOptionsAggregates: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getPreviousOptionsAggregates", "optionsTicker", a);
    let t = "/v2/aggs/ticker/{optionsTicker}/prev".replace("{optionsTicker}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.adjusted = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getPreviousStocksAggregates: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getPreviousStocksAggregates", "stocksTicker", a);
    let t = "/v2/aggs/ticker/{stocksTicker}/prev".replace("{stocksTicker}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.adjusted = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getRelatedCompanies: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getRelatedCompanies", "ticker", a);
    let i = "/v1/related-companies/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getSnapshotSummary: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    let i = "/v1/summaries", t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), a !== void 0 && (o["ticker.any_of"] = a), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getSnapshots: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, ..._10) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, ..._10], function* (a, e, i, t, r, n, s, o, g, u, c = {}) {
    let d = "/v3/snapshot", p = new URL(d, q), m;
    l && (m = l.baseOptions);
    let f = __spreadValues(__spreadValues({ method: "GET" }, m), c), b = {}, R = {};
    yield F(R, "apiKey", l), a !== void 0 && (R.ticker = a), e !== void 0 && (R.type = e), i !== void 0 && (R["ticker.gte"] = i), t !== void 0 && (R["ticker.gt"] = t), r !== void 0 && (R["ticker.lte"] = r), n !== void 0 && (R["ticker.lt"] = n), s !== void 0 && (R["ticker.any_of"] = s), o !== void 0 && (R.order = o), g !== void 0 && (R.limit = g), u !== void 0 && (R.sort = u), G(p, R);
    let x = m && m.headers ? m.headers : {};
    return f.headers = __spreadValues(__spreadValues(__spreadValues({}, b), x), c.headers), { url: B(p), options: f };
  }), getStocksAggregates: (_0, _1, _2, _3, _4, _5, _6, _7, ..._8) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, ..._8], function* (a, e, i, t, r, n, s, o, g = {}) {
    T("getStocksAggregates", "stocksTicker", a), T("getStocksAggregates", "multiplier", e), T("getStocksAggregates", "timespan", i), T("getStocksAggregates", "from", t), T("getStocksAggregates", "to", r);
    let u = "/v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}".replace("{stocksTicker}", encodeURIComponent(String(a))).replace("{multiplier}", encodeURIComponent(String(e))).replace("{timespan}", encodeURIComponent(String(i))).replace("{from}", encodeURIComponent(String(t))).replace("{to}", encodeURIComponent(String(r))), c = new URL(u, q), d;
    l && (d = l.baseOptions);
    let p = __spreadValues(__spreadValues({ method: "GET" }, d), g), m = {}, f = {};
    yield F(f, "apiKey", l), n !== void 0 && (f.adjusted = n), s !== void 0 && (f.sort = s), o !== void 0 && (f.limit = o), G(c, f);
    let b = d && d.headers ? d.headers : {};
    return p.headers = __spreadValues(__spreadValues(__spreadValues({}, m), b), g.headers), { url: B(c), options: p };
  }), getStocksEMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getStocksEMA", "stockTicker", a);
    let f = "/v1/indicators/ema/{stockTicker}".replace("{stockTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getStocksMACD: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, ..._15], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b = {}) {
    T("getStocksMACD", "stockTicker", a);
    let R = "/v1/indicators/macd/{stockTicker}".replace("{stockTicker}", encodeURIComponent(String(a))), x = new URL(R, q), y;
    l && (y = l.baseOptions);
    let h = __spreadValues(__spreadValues({ method: "GET" }, y), b), _ = {}, A = {};
    yield F(A, "apiKey", l), e !== void 0 && (A.timestamp = e), i !== void 0 && (A.timespan = i), t !== void 0 && (A.adjusted = t), r !== void 0 && (A.short_window = r), n !== void 0 && (A.long_window = n), s !== void 0 && (A.signal_window = s), o !== void 0 && (A.series_type = o), g !== void 0 && (A.expand_underlying = g), u !== void 0 && (A.order = u), c !== void 0 && (A.limit = c), d !== void 0 && (A["timestamp.gte"] = d), p !== void 0 && (A["timestamp.gt"] = p), m !== void 0 && (A["timestamp.lte"] = m), f !== void 0 && (A["timestamp.lt"] = f), G(x, A);
    let C = y && y.headers ? y.headers : {};
    return h.headers = __spreadValues(__spreadValues(__spreadValues({}, _), C), b.headers), { url: B(x), options: h };
  }), getStocksOpenClose: (_0, _1, _2, ..._3) => __async(null, [_0, _1, _2, ..._3], function* (a, e, i, t = {}) {
    T("getStocksOpenClose", "stocksTicker", a), T("getStocksOpenClose", "date", e);
    let r = "/v1/open-close/{stocksTicker}/{date}".replace("{stocksTicker}", encodeURIComponent(String(a))).replace("{date}", encodeURIComponent(String(e))), n = new URL(r, q), s;
    l && (s = l.baseOptions);
    let o = __spreadValues(__spreadValues({ method: "GET" }, s), t), g = {}, u = {};
    yield F(u, "apiKey", l), i !== void 0 && (u.adjusted = i), G(n, u);
    let c = s && s.headers ? s.headers : {};
    return o.headers = __spreadValues(__spreadValues(__spreadValues({}, g), c), t.headers), { url: B(n), options: o };
  }), getStocksQuotes: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    T("getStocksQuotes", "stockTicker", a);
    let c = "/v3/quotes/{stockTicker}".replace("{stockTicker}", encodeURIComponent(String(a))), d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), e !== void 0 && (b.timestamp = e), i !== void 0 && (b["timestamp.gte"] = i), t !== void 0 && (b["timestamp.gt"] = t), r !== void 0 && (b["timestamp.lte"] = r), n !== void 0 && (b["timestamp.lt"] = n), s !== void 0 && (b.order = s), o !== void 0 && (b.limit = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getStocksRSI: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getStocksRSI", "stockTicker", a);
    let f = "/v1/indicators/rsi/{stockTicker}".replace("{stockTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getStocksSMA: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    T("getStocksSMA", "stockTicker", a);
    let f = "/v1/indicators/sma/{stockTicker}".replace("{stockTicker}", encodeURIComponent(String(a))), b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    yield F(h, "apiKey", l), e !== void 0 && (h.timestamp = e), i !== void 0 && (h.timespan = i), t !== void 0 && (h.adjusted = t), r !== void 0 && (h.window = r), n !== void 0 && (h.series_type = n), s !== void 0 && (h.expand_underlying = s), o !== void 0 && (h.order = o), g !== void 0 && (h.limit = g), u !== void 0 && (h["timestamp.gte"] = u), c !== void 0 && (h["timestamp.gt"] = c), d !== void 0 && (h["timestamp.lte"] = d), p !== void 0 && (h["timestamp.lt"] = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), getStocksSnapshotDirection: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getStocksSnapshotDirection", "direction", a);
    let t = "/v2/snapshot/locale/us/markets/stocks/{direction}".replace("{direction}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.include_otc = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getStocksSnapshotTicker: (_0, ..._1) => __async(null, [_0, ..._1], function* (a, e = {}) {
    T("getStocksSnapshotTicker", "stocksTicker", a);
    let i = "/v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}".replace("{stocksTicker}", encodeURIComponent(String(a))), t = new URL(i, q), r;
    l && (r = l.baseOptions);
    let n = __spreadValues(__spreadValues({ method: "GET" }, r), e), s = {}, o = {};
    yield F(o, "apiKey", l), G(t, o);
    let g = r && r.headers ? r.headers : {};
    return n.headers = __spreadValues(__spreadValues(__spreadValues({}, s), g), e.headers), { url: B(t), options: n };
  }), getStocksSnapshotTickers: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    let t = "/v2/snapshot/locale/us/markets/stocks/tickers", r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), a && (g.tickers = a), e !== void 0 && (g.include_otc = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getStocksTrades: (_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, ..._9], function* (a, e, i, t, r, n, s, o, g, u = {}) {
    T("getStocksTrades", "stockTicker", a);
    let c = "/v3/trades/{stockTicker}".replace("{stockTicker}", encodeURIComponent(String(a))), d = new URL(c, q), p;
    l && (p = l.baseOptions);
    let m = __spreadValues(__spreadValues({ method: "GET" }, p), u), f = {}, b = {};
    yield F(b, "apiKey", l), e !== void 0 && (b.timestamp = e), i !== void 0 && (b["timestamp.gte"] = i), t !== void 0 && (b["timestamp.gt"] = t), r !== void 0 && (b["timestamp.lte"] = r), n !== void 0 && (b["timestamp.lt"] = n), s !== void 0 && (b.order = s), o !== void 0 && (b.limit = o), g !== void 0 && (b.sort = g), G(d, b);
    let R = p && p.headers ? p.headers : {};
    return m.headers = __spreadValues(__spreadValues(__spreadValues({}, f), R), u.headers), { url: B(d), options: m };
  }), getStocksV1ShortInterest: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, ..._26) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, ..._26], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P = {}) {
    let z = "/stocks/v1/short-interest", w = new URL(z, q), j;
    l && (j = l.baseOptions);
    let N = __spreadValues(__spreadValues({ method: "GET" }, j), P), L = {}, O = {};
    yield F(O, "apiKey", l), a !== void 0 && (O.ticker = a), e !== void 0 && (O["ticker.any_of"] = e), i !== void 0 && (O["ticker.gt"] = i), t !== void 0 && (O["ticker.gte"] = t), r !== void 0 && (O["ticker.lt"] = r), n !== void 0 && (O["ticker.lte"] = n), s !== void 0 && (O.days_to_cover = s), o !== void 0 && (O["days_to_cover.any_of"] = o), g !== void 0 && (O["days_to_cover.gt"] = g), u !== void 0 && (O["days_to_cover.gte"] = u), c !== void 0 && (O["days_to_cover.lt"] = c), d !== void 0 && (O["days_to_cover.lte"] = d), p !== void 0 && (O.settlement_date = p), m !== void 0 && (O["settlement_date.any_of"] = m), f !== void 0 && (O["settlement_date.gt"] = f), b !== void 0 && (O["settlement_date.gte"] = b), R !== void 0 && (O["settlement_date.lt"] = R), x !== void 0 && (O["settlement_date.lte"] = x), y !== void 0 && (O.avg_daily_volume = y), h !== void 0 && (O["avg_daily_volume.any_of"] = h), _ !== void 0 && (O["avg_daily_volume.gt"] = _), A !== void 0 && (O["avg_daily_volume.gte"] = A), C !== void 0 && (O["avg_daily_volume.lt"] = C), V !== void 0 && (O["avg_daily_volume.lte"] = V), Q !== void 0 && (O.limit = Q), H !== void 0 && (O.sort = H), G(w, O);
    let E = j && j.headers ? j.headers : {};
    return N.headers = __spreadValues(__spreadValues(__spreadValues({}, L), E), P.headers), { url: B(w), options: N };
  }), getStocksV1ShortVolume: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, ..._26) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, ..._26], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P = {}) {
    let z = "/stocks/v1/short-volume", w = new URL(z, q), j;
    l && (j = l.baseOptions);
    let N = __spreadValues(__spreadValues({ method: "GET" }, j), P), L = {}, O = {};
    yield F(O, "apiKey", l), a !== void 0 && (O.ticker = a), e !== void 0 && (O["ticker.any_of"] = e), i !== void 0 && (O["ticker.gt"] = i), t !== void 0 && (O["ticker.gte"] = t), r !== void 0 && (O["ticker.lt"] = r), n !== void 0 && (O["ticker.lte"] = n), s !== void 0 && (O.date = s), o !== void 0 && (O["date.any_of"] = o), g !== void 0 && (O["date.gt"] = g), u !== void 0 && (O["date.gte"] = u), c !== void 0 && (O["date.lt"] = c), d !== void 0 && (O["date.lte"] = d), p !== void 0 && (O.short_volume_ratio = p), m !== void 0 && (O["short_volume_ratio.any_of"] = m), f !== void 0 && (O["short_volume_ratio.gt"] = f), b !== void 0 && (O["short_volume_ratio.gte"] = b), R !== void 0 && (O["short_volume_ratio.lt"] = R), x !== void 0 && (O["short_volume_ratio.lte"] = x), y !== void 0 && (O.total_volume = y), h !== void 0 && (O["total_volume.any_of"] = h), _ !== void 0 && (O["total_volume.gt"] = _), A !== void 0 && (O["total_volume.gte"] = A), C !== void 0 && (O["total_volume.lt"] = C), V !== void 0 && (O["total_volume.lte"] = V), Q !== void 0 && (O.limit = Q), H !== void 0 && (O.sort = H), G(w, O);
    let E = j && j.headers ? j.headers : {};
    return N.headers = __spreadValues(__spreadValues(__spreadValues({}, L), E), P.headers), { url: B(w), options: N };
  }), getTicker: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    T("getTicker", "ticker", a);
    let t = "/v3/reference/tickers/{ticker}".replace("{ticker}", encodeURIComponent(String(a))), r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), e !== void 0 && (g.date = e instanceof Date ? e.toISOString().substring(0, 10) : e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), getTmxV1CorporateEvents: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, _44, _45, _46, _47, _48, _49, ..._50) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, _35, _36, _37, _38, _39, _40, _41, _42, _43, _44, _45, _46, _47, _48, _49, ..._50], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S = {}) {
    let ce = "/tmx/v1/corporate-events", de = new URL(ce, q), le;
    l && (le = l.baseOptions);
    let pe = __spreadValues(__spreadValues({ method: "GET" }, le), S), me = {}, W = {};
    yield F(W, "apiKey", l), a !== void 0 && (W.date = a), e !== void 0 && (W["date.any_of"] = e), i !== void 0 && (W["date.gt"] = i), t !== void 0 && (W["date.gte"] = t), r !== void 0 && (W["date.lt"] = r), n !== void 0 && (W["date.lte"] = n), s !== void 0 && (W.type = s), o !== void 0 && (W["type.any_of"] = o), g !== void 0 && (W["type.gt"] = g), u !== void 0 && (W["type.gte"] = u), c !== void 0 && (W["type.lt"] = c), d !== void 0 && (W["type.lte"] = d), p !== void 0 && (W.status = p), m !== void 0 && (W["status.any_of"] = m), f !== void 0 && (W["status.gt"] = f), b !== void 0 && (W["status.gte"] = b), R !== void 0 && (W["status.lt"] = R), x !== void 0 && (W["status.lte"] = x), y !== void 0 && (W.ticker = y), h !== void 0 && (W["ticker.any_of"] = h), _ !== void 0 && (W["ticker.gt"] = _), A !== void 0 && (W["ticker.gte"] = A), C !== void 0 && (W["ticker.lt"] = C), V !== void 0 && (W["ticker.lte"] = V), Q !== void 0 && (W.isin = Q), H !== void 0 && (W["isin.any_of"] = H), P !== void 0 && (W["isin.gt"] = P), z !== void 0 && (W["isin.gte"] = z), w !== void 0 && (W["isin.lt"] = w), j !== void 0 && (W["isin.lte"] = j), N !== void 0 && (W.trading_venue = N), L !== void 0 && (W["trading_venue.any_of"] = L), O !== void 0 && (W["trading_venue.gt"] = O), E !== void 0 && (W["trading_venue.gte"] = E), Y !== void 0 && (W["trading_venue.lt"] = Y), K !== void 0 && (W["trading_venue.lte"] = K), X !== void 0 && (W.tmx_company_id = X), Z !== void 0 && (W["tmx_company_id.any_of"] = Z), J !== void 0 && (W["tmx_company_id.gt"] = J), ee !== void 0 && (W["tmx_company_id.gte"] = ee), te !== void 0 && (W["tmx_company_id.lt"] = te), $ !== void 0 && (W["tmx_company_id.lte"] = $), se !== void 0 && (W.tmx_record_id = se), ne !== void 0 && (W["tmx_record_id.any_of"] = ne), ie !== void 0 && (W["tmx_record_id.gt"] = ie), ae !== void 0 && (W["tmx_record_id.gte"] = ae), oe !== void 0 && (W["tmx_record_id.lt"] = oe), re !== void 0 && (W["tmx_record_id.lte"] = re), ge !== void 0 && (W.limit = ge), ue !== void 0 && (W.sort = ue), G(de, W);
    let be = le && le.headers ? le.headers : {};
    return pe.headers = __spreadValues(__spreadValues(__spreadValues({}, me), be), S.headers), { url: B(de), options: pe };
  }), listConditions: (_0, _1, _2, _3, _4, _5, _6, ..._7) => __async(null, [_0, _1, _2, _3, _4, _5, _6, ..._7], function* (a, e, i, t, r, n, s, o = {}) {
    let g = "/v3/reference/conditions", u = new URL(g, q), c;
    l && (c = l.baseOptions);
    let d = __spreadValues(__spreadValues({ method: "GET" }, c), o), p = {}, m = {};
    yield F(m, "apiKey", l), a !== void 0 && (m.asset_class = a), e !== void 0 && (m.data_type = e), i !== void 0 && (m.id = i), t !== void 0 && (m.sip = t), r !== void 0 && (m.order = r), n !== void 0 && (m.limit = n), s !== void 0 && (m.sort = s), G(u, m);
    let f = c && c.headers ? c.headers : {};
    return d.headers = __spreadValues(__spreadValues(__spreadValues({}, p), f), o.headers), { url: B(u), options: d };
  }), listDividends: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, ..._35) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, _22, _23, _24, _25, _26, _27, _28, _29, _30, _31, _32, _33, _34, ..._35], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K = {}) {
    let X = "/v3/reference/dividends", Z = new URL(X, q), J;
    l && (J = l.baseOptions);
    let ee = __spreadValues(__spreadValues({ method: "GET" }, J), K), te = {}, $ = {};
    yield F($, "apiKey", l), a !== void 0 && ($.ticker = a), e !== void 0 && ($.ex_dividend_date = e instanceof Date ? e.toISOString().substring(0, 10) : e), i !== void 0 && ($.record_date = i instanceof Date ? i.toISOString().substring(0, 10) : i), t !== void 0 && ($.declaration_date = t instanceof Date ? t.toISOString().substring(0, 10) : t), r !== void 0 && ($.pay_date = r instanceof Date ? r.toISOString().substring(0, 10) : r), n !== void 0 && ($.frequency = n), s !== void 0 && ($.cash_amount = s), o !== void 0 && ($.dividend_type = o), g !== void 0 && ($["ticker.gte"] = g), u !== void 0 && ($["ticker.gt"] = u), c !== void 0 && ($["ticker.lte"] = c), d !== void 0 && ($["ticker.lt"] = d), p !== void 0 && ($["ex_dividend_date.gte"] = p instanceof Date ? p.toISOString().substring(0, 10) : p), m !== void 0 && ($["ex_dividend_date.gt"] = m instanceof Date ? m.toISOString().substring(0, 10) : m), f !== void 0 && ($["ex_dividend_date.lte"] = f instanceof Date ? f.toISOString().substring(0, 10) : f), b !== void 0 && ($["ex_dividend_date.lt"] = b instanceof Date ? b.toISOString().substring(0, 10) : b), R !== void 0 && ($["record_date.gte"] = R instanceof Date ? R.toISOString().substring(0, 10) : R), x !== void 0 && ($["record_date.gt"] = x instanceof Date ? x.toISOString().substring(0, 10) : x), y !== void 0 && ($["record_date.lte"] = y instanceof Date ? y.toISOString().substring(0, 10) : y), h !== void 0 && ($["record_date.lt"] = h instanceof Date ? h.toISOString().substring(0, 10) : h), _ !== void 0 && ($["declaration_date.gte"] = _ instanceof Date ? _.toISOString().substring(0, 10) : _), A !== void 0 && ($["declaration_date.gt"] = A instanceof Date ? A.toISOString().substring(0, 10) : A), C !== void 0 && ($["declaration_date.lte"] = C instanceof Date ? C.toISOString().substring(0, 10) : C), V !== void 0 && ($["declaration_date.lt"] = V instanceof Date ? V.toISOString().substring(0, 10) : V), Q !== void 0 && ($["pay_date.gte"] = Q instanceof Date ? Q.toISOString().substring(0, 10) : Q), H !== void 0 && ($["pay_date.gt"] = H instanceof Date ? H.toISOString().substring(0, 10) : H), P !== void 0 && ($["pay_date.lte"] = P instanceof Date ? P.toISOString().substring(0, 10) : P), z !== void 0 && ($["pay_date.lt"] = z instanceof Date ? z.toISOString().substring(0, 10) : z), w !== void 0 && ($["cash_amount.gte"] = w), j !== void 0 && ($["cash_amount.gt"] = j), N !== void 0 && ($["cash_amount.lte"] = N), L !== void 0 && ($["cash_amount.lt"] = L), O !== void 0 && ($.order = O), E !== void 0 && ($.limit = E), Y !== void 0 && ($.sort = Y), G(Z, $);
    let se = J && J.headers ? J.headers : {};
    return ee.headers = __spreadValues(__spreadValues(__spreadValues({}, te), se), K.headers), { url: B(Z), options: ee };
  }), listExchanges: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    let t = "/v3/reference/exchanges", r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), a !== void 0 && (g.asset_class = a), e !== void 0 && (g.locale = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), listFinancials: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, ..._20) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, ..._20], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _ = {}) {
    let A = "/vX/reference/financials", C = new URL(A, q), V;
    l && (V = l.baseOptions);
    let Q = __spreadValues(__spreadValues({ method: "GET" }, V), _), H = {}, P = {};
    yield F(P, "apiKey", l), a !== void 0 && (P.ticker = a), e !== void 0 && (P.cik = e), i !== void 0 && (P.company_name = i), t !== void 0 && (P.sic = t), r !== void 0 && (P.filing_date = r instanceof Date ? r.toISOString().substring(0, 10) : r), n !== void 0 && (P.period_of_report_date = n instanceof Date ? n.toISOString().substring(0, 10) : n), s !== void 0 && (P.timeframe = s), o !== void 0 && (P.include_sources = o), g !== void 0 && (P["company_name.search"] = g), u !== void 0 && (P["filing_date.gte"] = u instanceof Date ? u.toISOString().substring(0, 10) : u), c !== void 0 && (P["filing_date.gt"] = c instanceof Date ? c.toISOString().substring(0, 10) : c), d !== void 0 && (P["filing_date.lte"] = d instanceof Date ? d.toISOString().substring(0, 10) : d), p !== void 0 && (P["filing_date.lt"] = p instanceof Date ? p.toISOString().substring(0, 10) : p), m !== void 0 && (P["period_of_report_date.gte"] = m instanceof Date ? m.toISOString().substring(0, 10) : m), f !== void 0 && (P["period_of_report_date.gt"] = f instanceof Date ? f.toISOString().substring(0, 10) : f), b !== void 0 && (P["period_of_report_date.lte"] = b instanceof Date ? b.toISOString().substring(0, 10) : b), R !== void 0 && (P["period_of_report_date.lt"] = R instanceof Date ? R.toISOString().substring(0, 10) : R), x !== void 0 && (P.order = x), y !== void 0 && (P.limit = y), h !== void 0 && (P.sort = h), G(C, P);
    let z = V && V.headers ? V.headers : {};
    return Q.headers = __spreadValues(__spreadValues(__spreadValues({}, H), z), _.headers), { url: B(C), options: Q };
  }), listIPOs: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, ..._12], function* (a, e, i, t, r, n, s, o, g, u, c, d, p = {}) {
    let m = "/vX/reference/ipos", f = new URL(m, q), b;
    l && (b = l.baseOptions);
    let R = __spreadValues(__spreadValues({ method: "GET" }, b), p), x = {}, y = {};
    yield F(y, "apiKey", l), a !== void 0 && (y.ticker = a), e !== void 0 && (y.us_code = e), i !== void 0 && (y.isin = i), t !== void 0 && (y.listing_date = t instanceof Date ? t.toISOString().substring(0, 10) : t), r !== void 0 && (y.ipo_status = r), n !== void 0 && (y["listing_date.gte"] = n instanceof Date ? n.toISOString().substring(0, 10) : n), s !== void 0 && (y["listing_date.gt"] = s instanceof Date ? s.toISOString().substring(0, 10) : s), o !== void 0 && (y["listing_date.lte"] = o instanceof Date ? o.toISOString().substring(0, 10) : o), g !== void 0 && (y["listing_date.lt"] = g instanceof Date ? g.toISOString().substring(0, 10) : g), u !== void 0 && (y.order = u), c !== void 0 && (y.limit = c), d !== void 0 && (y.sort = d), G(f, y);
    let h = b && b.headers ? b.headers : {};
    return R.headers = __spreadValues(__spreadValues(__spreadValues({}, x), h), p.headers), { url: B(f), options: R };
  }), listNews: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, ..._13], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m = {}) {
    let f = "/v2/reference/news", b = new URL(f, q), R;
    l && (R = l.baseOptions);
    let x = __spreadValues(__spreadValues({ method: "GET" }, R), m), y = {}, h = {};
    if (yield F(h, "apiKey", l), a !== void 0 && (h.ticker = a), e !== void 0) for (let [A, C] of Object.entries(e)) h[A] = C;
    if (i !== void 0 && (h["ticker.gte"] = i), t !== void 0 && (h["ticker.gt"] = t), r !== void 0 && (h["ticker.lte"] = r), n !== void 0 && (h["ticker.lt"] = n), s !== void 0) for (let [A, C] of Object.entries(s)) h[A] = C;
    if (o !== void 0) for (let [A, C] of Object.entries(o)) h[A] = C;
    if (g !== void 0) for (let [A, C] of Object.entries(g)) h[A] = C;
    if (u !== void 0) for (let [A, C] of Object.entries(u)) h[A] = C;
    c !== void 0 && (h.order = c), d !== void 0 && (h.limit = d), p !== void 0 && (h.sort = p), G(b, h);
    let _ = R && R.headers ? R.headers : {};
    return x.headers = __spreadValues(__spreadValues(__spreadValues({}, y), _), m.headers), { url: B(b), options: x };
  }), listOptionsContracts: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, ..._22) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, _16, _17, _18, _19, _20, _21, ..._22], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C = {}) {
    let V = "/v3/reference/options/contracts", Q = new URL(V, q), H;
    l && (H = l.baseOptions);
    let P = __spreadValues(__spreadValues({ method: "GET" }, H), C), z = {}, w = {};
    yield F(w, "apiKey", l), a !== void 0 && (w.underlying_ticker = a), e !== void 0 && (w.ticker = e), i !== void 0 && (w.contract_type = i), t !== void 0 && (w.expiration_date = t), r !== void 0 && (w.as_of = r), n !== void 0 && (w.strike_price = n), s !== void 0 && (w.expired = s), o !== void 0 && (w["underlying_ticker.gte"] = o), g !== void 0 && (w["underlying_ticker.gt"] = g), u !== void 0 && (w["underlying_ticker.lte"] = u), c !== void 0 && (w["underlying_ticker.lt"] = c), d !== void 0 && (w["expiration_date.gte"] = d), p !== void 0 && (w["expiration_date.gt"] = p), m !== void 0 && (w["expiration_date.lte"] = m), f !== void 0 && (w["expiration_date.lt"] = f), b !== void 0 && (w["strike_price.gte"] = b), R !== void 0 && (w["strike_price.gt"] = R), x !== void 0 && (w["strike_price.lte"] = x), y !== void 0 && (w["strike_price.lt"] = y), h !== void 0 && (w.order = h), _ !== void 0 && (w.limit = _), A !== void 0 && (w.sort = A), G(Q, w);
    let j = H && H.headers ? H.headers : {};
    return P.headers = __spreadValues(__spreadValues(__spreadValues({}, z), j), C.headers), { url: B(Q), options: P };
  }), listStockSplits: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, ..._14) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, ..._14], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f = {}) {
    let b = "/v3/reference/splits", R = new URL(b, q), x;
    l && (x = l.baseOptions);
    let y = __spreadValues(__spreadValues({ method: "GET" }, x), f), h = {}, _ = {};
    yield F(_, "apiKey", l), a !== void 0 && (_.ticker = a), e !== void 0 && (_.execution_date = e instanceof Date ? e.toISOString().substring(0, 10) : e), i !== void 0 && (_.reverse_split = i), t !== void 0 && (_["ticker.gte"] = t), r !== void 0 && (_["ticker.gt"] = r), n !== void 0 && (_["ticker.lte"] = n), s !== void 0 && (_["ticker.lt"] = s), o !== void 0 && (_["execution_date.gte"] = o instanceof Date ? o.toISOString().substring(0, 10) : o), g !== void 0 && (_["execution_date.gt"] = g instanceof Date ? g.toISOString().substring(0, 10) : g), u !== void 0 && (_["execution_date.lte"] = u instanceof Date ? u.toISOString().substring(0, 10) : u), c !== void 0 && (_["execution_date.lt"] = c instanceof Date ? c.toISOString().substring(0, 10) : c), d !== void 0 && (_.order = d), p !== void 0 && (_.limit = p), m !== void 0 && (_.sort = m), G(R, _);
    let A = x && x.headers ? x.headers : {};
    return y.headers = __spreadValues(__spreadValues(__spreadValues({}, h), A), f.headers), { url: B(R), options: y };
  }), listTickerTypes: (_0, _1, ..._2) => __async(null, [_0, _1, ..._2], function* (a, e, i = {}) {
    let t = "/v3/reference/tickers/types", r = new URL(t, q), n;
    l && (n = l.baseOptions);
    let s = __spreadValues(__spreadValues({ method: "GET" }, n), i), o = {}, g = {};
    yield F(g, "apiKey", l), a !== void 0 && (g.asset_class = a), e !== void 0 && (g.locale = e), G(r, g);
    let u = n && n.headers ? n.headers : {};
    return s.headers = __spreadValues(__spreadValues(__spreadValues({}, o), u), i.headers), { url: B(r), options: s };
  }), listTickers: (_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, ..._16) => __async(null, [_0, _1, _2, _3, _4, _5, _6, _7, _8, _9, _10, _11, _12, _13, _14, _15, ..._16], function* (a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R = {}) {
    let x = "/v3/reference/tickers", y = new URL(x, q), h;
    l && (h = l.baseOptions);
    let _ = __spreadValues(__spreadValues({ method: "GET" }, h), R), A = {}, C = {};
    yield F(C, "apiKey", l), a !== void 0 && (C.ticker = a), e !== void 0 && (C.type = e), i !== void 0 && (C.market = i), t !== void 0 && (C.exchange = t), r !== void 0 && (C.cusip = r), n !== void 0 && (C.cik = n), s !== void 0 && (C.date = s instanceof Date ? s.toISOString().substring(0, 10) : s), o !== void 0 && (C.search = o), g !== void 0 && (C.active = g), u !== void 0 && (C["ticker.gte"] = u), c !== void 0 && (C["ticker.gt"] = c), d !== void 0 && (C["ticker.lte"] = d), p !== void 0 && (C["ticker.lt"] = p), m !== void 0 && (C.order = m), f !== void 0 && (C.limit = f), b !== void 0 && (C.sort = b), G(y, C);
    let V = h && h.headers ? h.headers : {};
    return _.headers = __spreadValues(__spreadValues(__spreadValues({}, A), V), R.headers), { url: B(y), options: _ };
  }) };
};
var D = function(l) {
  let a = Tt(l);
  return { deprecatedGetCryptoSnapshotTickerBook(e, i) {
    return __async(this, null, function* () {
      let t = yield a.deprecatedGetCryptoSnapshotTickerBook(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.deprecatedGetCryptoSnapshotTickerBook"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, deprecatedGetHistoricCryptoTrades(e, i, t, r, n, s) {
    return __async(this, null, function* () {
      let o = yield a.deprecatedGetHistoricCryptoTrades(e, i, t, r, n, s), g = l?.serverIndex ?? 0, u = v["DefaultApi.deprecatedGetHistoricCryptoTrades"]?.[g]?.url;
      return (c, d) => U(o, axios_default, I, l)(c, u || d);
    });
  }, deprecatedGetHistoricForexQuotes(e, i, t, r, n, s) {
    return __async(this, null, function* () {
      let o = yield a.deprecatedGetHistoricForexQuotes(e, i, t, r, n, s), g = l?.serverIndex ?? 0, u = v["DefaultApi.deprecatedGetHistoricForexQuotes"]?.[g]?.url;
      return (c, d) => U(o, axios_default, I, l)(c, u || d);
    });
  }, deprecatedGetHistoricStocksQuotes(e, i, t, r, n, s, o) {
    return __async(this, null, function* () {
      let g = yield a.deprecatedGetHistoricStocksQuotes(e, i, t, r, n, s, o), u = l?.serverIndex ?? 0, c = v["DefaultApi.deprecatedGetHistoricStocksQuotes"]?.[u]?.url;
      return (d, p) => U(g, axios_default, I, l)(d, c || p);
    });
  }, deprecatedGetHistoricStocksTrades(e, i, t, r, n, s, o) {
    return __async(this, null, function* () {
      let g = yield a.deprecatedGetHistoricStocksTrades(e, i, t, r, n, s, o), u = l?.serverIndex ?? 0, c = v["DefaultApi.deprecatedGetHistoricStocksTrades"]?.[u]?.url;
      return (d, p) => U(g, axios_default, I, l)(d, c || p);
    });
  }, getBenzingaV1AnalystInsights(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae) {
    return __async(this, null, function* () {
      let oe = yield a.getBenzingaV1AnalystInsights(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae), re = l?.serverIndex ?? 0, ge = v["DefaultApi.getBenzingaV1AnalystInsights"]?.[re]?.url;
      return (ue, S) => U(oe, axios_default, I, l)(ue, ge || S);
    });
  }, getBenzingaV1Analysts(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z) {
    return __async(this, null, function* () {
      let w = yield a.getBenzingaV1Analysts(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z), j = l?.serverIndex ?? 0, N = v["DefaultApi.getBenzingaV1Analysts"]?.[j]?.url;
      return (L, O) => U(w, axios_default, I, l)(L, N || O);
    });
  }, getBenzingaV1ConsensusRatings(e, i, t, r, n, s, o, g) {
    return __async(this, null, function* () {
      let u = yield a.getBenzingaV1ConsensusRatings(e, i, t, r, n, s, o, g), c = l?.serverIndex ?? 0, d = v["DefaultApi.getBenzingaV1ConsensusRatings"]?.[c]?.url;
      return (p, m) => U(u, axios_default, I, l)(p, d || m);
    });
  }, getBenzingaV1Earnings(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be) {
    return __async(this, null, function* () {
      let he = yield a.getBenzingaV1Earnings(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be), fe = l?.serverIndex ?? 0, Re = v["DefaultApi.getBenzingaV1Earnings"]?.[fe]?.url;
      return (ye, k) => U(he, axios_default, I, l)(ye, Re || k);
    });
  }, getBenzingaV1Firms(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getBenzingaV1Firms(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getBenzingaV1Firms"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getBenzingaV1Guidance(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae) {
    return __async(this, null, function* () {
      let oe = yield a.getBenzingaV1Guidance(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae), re = l?.serverIndex ?? 0, ge = v["DefaultApi.getBenzingaV1Guidance"]?.[re]?.url;
      return (ue, S) => U(oe, axios_default, I, l)(ue, ge || S);
    });
  }, getBenzingaV1News(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N) {
    return __async(this, null, function* () {
      let L = yield a.getBenzingaV1News(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N), O = l?.serverIndex ?? 0, E = v["DefaultApi.getBenzingaV1News"]?.[O]?.url;
      return (Y, K) => U(L, axios_default, I, l)(Y, E || K);
    });
  }, getBenzingaV1Ratings(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be) {
    return __async(this, null, function* () {
      let he = yield a.getBenzingaV1Ratings(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be), fe = l?.serverIndex ?? 0, Re = v["DefaultApi.getBenzingaV1Ratings"]?.[fe]?.url;
      return (ye, k) => U(he, axios_default, I, l)(ye, Re || k);
    });
  }, getCryptoAggregates(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getCryptoAggregates(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getCryptoAggregates"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getCryptoEMA(e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return __async(this, null, function* () {
      let f = yield a.getCryptoEMA(e, i, t, r, n, s, o, g, u, c, d, p, m), b = l?.serverIndex ?? 0, R = v["DefaultApi.getCryptoEMA"]?.[b]?.url;
      return (x, y) => U(f, axios_default, I, l)(x, R || y);
    });
  }, getCryptoMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return __async(this, null, function* () {
      let R = yield a.getCryptoMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b), x = l?.serverIndex ?? 0, y = v["DefaultApi.getCryptoMACD"]?.[x]?.url;
      return (h, _) => U(R, axios_default, I, l)(h, y || _);
    });
  }, getCryptoOpenClose(e, i, t, r, n) {
    return __async(this, null, function* () {
      let s = yield a.getCryptoOpenClose(e, i, t, r, n), o = l?.serverIndex ?? 0, g = v["DefaultApi.getCryptoOpenClose"]?.[o]?.url;
      return (u, c) => U(s, axios_default, I, l)(u, g || c);
    });
  }, getCryptoRSI(e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return __async(this, null, function* () {
      let f = yield a.getCryptoRSI(e, i, t, r, n, s, o, g, u, c, d, p, m), b = l?.serverIndex ?? 0, R = v["DefaultApi.getCryptoRSI"]?.[b]?.url;
      return (x, y) => U(f, axios_default, I, l)(x, R || y);
    });
  }, getCryptoSMA(e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return __async(this, null, function* () {
      let f = yield a.getCryptoSMA(e, i, t, r, n, s, o, g, u, c, d, p, m), b = l?.serverIndex ?? 0, R = v["DefaultApi.getCryptoSMA"]?.[b]?.url;
      return (x, y) => U(f, axios_default, I, l)(x, R || y);
    });
  }, getCryptoSnapshotDirection(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getCryptoSnapshotDirection(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getCryptoSnapshotDirection"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getCryptoSnapshotTicker(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getCryptoSnapshotTicker(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getCryptoSnapshotTicker"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getCryptoSnapshotTickers(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getCryptoSnapshotTickers(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getCryptoSnapshotTickers"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getCryptoTrades(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getCryptoTrades(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getCryptoTrades"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getCurrencyConversion(e, i, t, r, n) {
    return __async(this, null, function* () {
      let s = yield a.getCurrencyConversion(e, i, t, r, n), o = l?.serverIndex ?? 0, g = v["DefaultApi.getCurrencyConversion"]?.[o]?.url;
      return (u, c) => U(s, axios_default, I, l)(u, g || c);
    });
  }, getEvents(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getEvents(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getEvents"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getFedV1Inflation(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getFedV1Inflation(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getFedV1Inflation"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getFedV1InflationExpectations(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getFedV1InflationExpectations(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getFedV1InflationExpectations"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getFedV1TreasuryYields(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getFedV1TreasuryYields(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getFedV1TreasuryYields"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getForexAggregates(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getForexAggregates(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getForexAggregates"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getForexEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getForexEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getForexEMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getForexMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return __async(this, null, function* () {
      let x = yield a.getForexMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R), y = l?.serverIndex ?? 0, h = v["DefaultApi.getForexMACD"]?.[y]?.url;
      return (_, A) => U(x, axios_default, I, l)(_, h || A);
    });
  }, getForexQuotes(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getForexQuotes(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getForexQuotes"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getForexRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getForexRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getForexRSI"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getForexSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getForexSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getForexSMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getForexSnapshotDirection(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getForexSnapshotDirection(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getForexSnapshotDirection"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getForexSnapshotTicker(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getForexSnapshotTicker(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getForexSnapshotTicker"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getForexSnapshotTickers(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getForexSnapshotTickers(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getForexSnapshotTickers"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getFuturesAggregates(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getFuturesAggregates(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getFuturesAggregates"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getFuturesContractDetails(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getFuturesContractDetails(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getFuturesContractDetails"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getFuturesContracts(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getFuturesContracts(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getFuturesContracts"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getFuturesDailySchedules(e, i, t, r, n) {
    return __async(this, null, function* () {
      let s = yield a.getFuturesDailySchedules(e, i, t, r, n), o = l?.serverIndex ?? 0, g = v["DefaultApi.getFuturesDailySchedules"]?.[o]?.url;
      return (u, c) => U(s, axios_default, I, l)(u, g || c);
    });
  }, getFuturesMarketStatuses(e, i, t, r, n) {
    return __async(this, null, function* () {
      let s = yield a.getFuturesMarketStatuses(e, i, t, r, n), o = l?.serverIndex ?? 0, g = v["DefaultApi.getFuturesMarketStatuses"]?.[o]?.url;
      return (u, c) => U(s, axios_default, I, l)(u, g || c);
    });
  }, getFuturesProductDetails(e, i, t, r) {
    return __async(this, null, function* () {
      let n = yield a.getFuturesProductDetails(e, i, t, r), s = l?.serverIndex ?? 0, o = v["DefaultApi.getFuturesProductDetails"]?.[s]?.url;
      return (g, u) => U(n, axios_default, I, l)(g, o || u);
    });
  }, getFuturesProductSchedules(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getFuturesProductSchedules(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getFuturesProductSchedules"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getFuturesProducts(e, i, t, r, n, s, o, g, u, c, d, p) {
    return __async(this, null, function* () {
      let m = yield a.getFuturesProducts(e, i, t, r, n, s, o, g, u, c, d, p), f = l?.serverIndex ?? 0, b = v["DefaultApi.getFuturesProducts"]?.[f]?.url;
      return (R, x) => U(m, axios_default, I, l)(R, b || x);
    });
  }, getFuturesQuotes(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getFuturesQuotes(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getFuturesQuotes"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getFuturesTrades(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getFuturesTrades(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getFuturesTrades"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getGroupedCryptoAggregates(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getGroupedCryptoAggregates(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getGroupedCryptoAggregates"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getGroupedForexAggregates(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getGroupedForexAggregates(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getGroupedForexAggregates"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getGroupedStocksAggregates(e, i, t, r) {
    return __async(this, null, function* () {
      let n = yield a.getGroupedStocksAggregates(e, i, t, r), s = l?.serverIndex ?? 0, o = v["DefaultApi.getGroupedStocksAggregates"]?.[s]?.url;
      return (g, u) => U(n, axios_default, I, l)(g, o || u);
    });
  }, getIndicesAggregates(e, i, t, r, n, s, o, g) {
    return __async(this, null, function* () {
      let u = yield a.getIndicesAggregates(e, i, t, r, n, s, o, g), c = l?.serverIndex ?? 0, d = v["DefaultApi.getIndicesAggregates"]?.[c]?.url;
      return (p, m) => U(u, axios_default, I, l)(p, d || m);
    });
  }, getIndicesEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getIndicesEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getIndicesEMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getIndicesMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return __async(this, null, function* () {
      let x = yield a.getIndicesMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R), y = l?.serverIndex ?? 0, h = v["DefaultApi.getIndicesMACD"]?.[y]?.url;
      return (_, A) => U(x, axios_default, I, l)(_, h || A);
    });
  }, getIndicesOpenClose(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getIndicesOpenClose(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getIndicesOpenClose"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getIndicesRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getIndicesRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getIndicesRSI"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getIndicesSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getIndicesSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getIndicesSMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getIndicesSnapshot(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getIndicesSnapshot(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getIndicesSnapshot"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getLastCryptoTrade(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getLastCryptoTrade(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getLastCryptoTrade"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getLastCurrencyQuote(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getLastCurrencyQuote(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getLastCurrencyQuote"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getLastOptionsTrade(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getLastOptionsTrade(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getLastOptionsTrade"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getLastStocksQuote(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getLastStocksQuote(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getLastStocksQuote"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getLastStocksTrade(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getLastStocksTrade(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getLastStocksTrade"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getMarketHolidays(e) {
    return __async(this, null, function* () {
      let i = yield a.getMarketHolidays(e), t = l?.serverIndex ?? 0, r = v["DefaultApi.getMarketHolidays"]?.[t]?.url;
      return (n, s) => U(i, axios_default, I, l)(n, r || s);
    });
  }, getMarketStatus(e) {
    return __async(this, null, function* () {
      let i = yield a.getMarketStatus(e), t = l?.serverIndex ?? 0, r = v["DefaultApi.getMarketStatus"]?.[t]?.url;
      return (n, s) => U(i, axios_default, I, l)(n, r || s);
    });
  }, getOptionContract(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getOptionContract(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getOptionContract"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getOptionsAggregates(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getOptionsAggregates(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getOptionsAggregates"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getOptionsChain(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return __async(this, null, function* () {
      let x = yield a.getOptionsChain(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R), y = l?.serverIndex ?? 0, h = v["DefaultApi.getOptionsChain"]?.[y]?.url;
      return (_, A) => U(x, axios_default, I, l)(_, h || A);
    });
  }, getOptionsContract(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getOptionsContract(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getOptionsContract"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getOptionsEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getOptionsEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getOptionsEMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getOptionsMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return __async(this, null, function* () {
      let x = yield a.getOptionsMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R), y = l?.serverIndex ?? 0, h = v["DefaultApi.getOptionsMACD"]?.[y]?.url;
      return (_, A) => U(x, axios_default, I, l)(_, h || A);
    });
  }, getOptionsOpenClose(e, i, t, r) {
    return __async(this, null, function* () {
      let n = yield a.getOptionsOpenClose(e, i, t, r), s = l?.serverIndex ?? 0, o = v["DefaultApi.getOptionsOpenClose"]?.[s]?.url;
      return (g, u) => U(n, axios_default, I, l)(g, o || u);
    });
  }, getOptionsQuotes(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getOptionsQuotes(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getOptionsQuotes"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getOptionsRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getOptionsRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getOptionsRSI"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getOptionsSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getOptionsSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getOptionsSMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getOptionsTrades(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getOptionsTrades(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getOptionsTrades"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getPreviousCryptoAggregates(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getPreviousCryptoAggregates(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getPreviousCryptoAggregates"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getPreviousForexAggregates(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getPreviousForexAggregates(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getPreviousForexAggregates"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getPreviousIndicesAggregates(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getPreviousIndicesAggregates(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getPreviousIndicesAggregates"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getPreviousOptionsAggregates(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getPreviousOptionsAggregates(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getPreviousOptionsAggregates"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getPreviousStocksAggregates(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getPreviousStocksAggregates(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getPreviousStocksAggregates"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getRelatedCompanies(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getRelatedCompanies(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getRelatedCompanies"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getSnapshotSummary(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getSnapshotSummary(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getSnapshotSummary"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getSnapshots(e, i, t, r, n, s, o, g, u, c, d) {
    return __async(this, null, function* () {
      let p = yield a.getSnapshots(e, i, t, r, n, s, o, g, u, c, d), m = l?.serverIndex ?? 0, f = v["DefaultApi.getSnapshots"]?.[m]?.url;
      return (b, R) => U(p, axios_default, I, l)(b, f || R);
    });
  }, getStocksAggregates(e, i, t, r, n, s, o, g, u) {
    return __async(this, null, function* () {
      let c = yield a.getStocksAggregates(e, i, t, r, n, s, o, g, u), d = l?.serverIndex ?? 0, p = v["DefaultApi.getStocksAggregates"]?.[d]?.url;
      return (m, f) => U(c, axios_default, I, l)(m, p || f);
    });
  }, getStocksEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getStocksEMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getStocksEMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getStocksMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return __async(this, null, function* () {
      let x = yield a.getStocksMACD(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R), y = l?.serverIndex ?? 0, h = v["DefaultApi.getStocksMACD"]?.[y]?.url;
      return (_, A) => U(x, axios_default, I, l)(_, h || A);
    });
  }, getStocksOpenClose(e, i, t, r) {
    return __async(this, null, function* () {
      let n = yield a.getStocksOpenClose(e, i, t, r), s = l?.serverIndex ?? 0, o = v["DefaultApi.getStocksOpenClose"]?.[s]?.url;
      return (g, u) => U(n, axios_default, I, l)(g, o || u);
    });
  }, getStocksQuotes(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getStocksQuotes(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getStocksQuotes"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getStocksRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getStocksRSI(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getStocksRSI"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getStocksSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.getStocksSMA(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.getStocksSMA"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, getStocksSnapshotDirection(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getStocksSnapshotDirection(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getStocksSnapshotDirection"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getStocksSnapshotTicker(e, i) {
    return __async(this, null, function* () {
      let t = yield a.getStocksSnapshotTicker(e, i), r = l?.serverIndex ?? 0, n = v["DefaultApi.getStocksSnapshotTicker"]?.[r]?.url;
      return (s, o) => U(t, axios_default, I, l)(s, n || o);
    });
  }, getStocksSnapshotTickers(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getStocksSnapshotTickers(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getStocksSnapshotTickers"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getStocksTrades(e, i, t, r, n, s, o, g, u, c) {
    return __async(this, null, function* () {
      let d = yield a.getStocksTrades(e, i, t, r, n, s, o, g, u, c), p = l?.serverIndex ?? 0, m = v["DefaultApi.getStocksTrades"]?.[p]?.url;
      return (f, b) => U(d, axios_default, I, l)(f, m || b);
    });
  }, getStocksV1ShortInterest(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z) {
    return __async(this, null, function* () {
      let w = yield a.getStocksV1ShortInterest(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z), j = l?.serverIndex ?? 0, N = v["DefaultApi.getStocksV1ShortInterest"]?.[j]?.url;
      return (L, O) => U(w, axios_default, I, l)(L, N || O);
    });
  }, getStocksV1ShortVolume(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z) {
    return __async(this, null, function* () {
      let w = yield a.getStocksV1ShortVolume(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z), j = l?.serverIndex ?? 0, N = v["DefaultApi.getStocksV1ShortVolume"]?.[j]?.url;
      return (L, O) => U(w, axios_default, I, l)(L, N || O);
    });
  }, getTicker(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.getTicker(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.getTicker"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, getTmxV1CorporateEvents(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce) {
    return __async(this, null, function* () {
      let de = yield a.getTmxV1CorporateEvents(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce), le = l?.serverIndex ?? 0, pe = v["DefaultApi.getTmxV1CorporateEvents"]?.[le]?.url;
      return (me, W) => U(de, axios_default, I, l)(me, pe || W);
    });
  }, listConditions(e, i, t, r, n, s, o, g) {
    return __async(this, null, function* () {
      let u = yield a.listConditions(e, i, t, r, n, s, o, g), c = l?.serverIndex ?? 0, d = v["DefaultApi.listConditions"]?.[c]?.url;
      return (p, m) => U(u, axios_default, I, l)(p, d || m);
    });
  }, listDividends(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X) {
    return __async(this, null, function* () {
      let Z = yield a.listDividends(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X), J = l?.serverIndex ?? 0, ee = v["DefaultApi.listDividends"]?.[J]?.url;
      return (te, $) => U(Z, axios_default, I, l)(te, ee || $);
    });
  }, listExchanges(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.listExchanges(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.listExchanges"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, listFinancials(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A) {
    return __async(this, null, function* () {
      let C = yield a.listFinancials(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A), V = l?.serverIndex ?? 0, Q = v["DefaultApi.listFinancials"]?.[V]?.url;
      return (H, P) => U(C, axios_default, I, l)(H, Q || P);
    });
  }, listIPOs(e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return __async(this, null, function* () {
      let f = yield a.listIPOs(e, i, t, r, n, s, o, g, u, c, d, p, m), b = l?.serverIndex ?? 0, R = v["DefaultApi.listIPOs"]?.[b]?.url;
      return (x, y) => U(f, axios_default, I, l)(x, R || y);
    });
  }, listNews(e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return __async(this, null, function* () {
      let b = yield a.listNews(e, i, t, r, n, s, o, g, u, c, d, p, m, f), R = l?.serverIndex ?? 0, x = v["DefaultApi.listNews"]?.[R]?.url;
      return (y, h) => U(b, axios_default, I, l)(y, x || h);
    });
  }, listOptionsContracts(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V) {
    return __async(this, null, function* () {
      let Q = yield a.listOptionsContracts(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V), H = l?.serverIndex ?? 0, P = v["DefaultApi.listOptionsContracts"]?.[H]?.url;
      return (z, w) => U(Q, axios_default, I, l)(z, P || w);
    });
  }, listStockSplits(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return __async(this, null, function* () {
      let R = yield a.listStockSplits(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b), x = l?.serverIndex ?? 0, y = v["DefaultApi.listStockSplits"]?.[x]?.url;
      return (h, _) => U(R, axios_default, I, l)(h, y || _);
    });
  }, listTickerTypes(e, i, t) {
    return __async(this, null, function* () {
      let r = yield a.listTickerTypes(e, i, t), n = l?.serverIndex ?? 0, s = v["DefaultApi.listTickerTypes"]?.[n]?.url;
      return (o, g) => U(r, axios_default, I, l)(o, s || g);
    });
  }, listTickers(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x) {
    return __async(this, null, function* () {
      let y = yield a.listTickers(e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x), h = l?.serverIndex ?? 0, _ = v["DefaultApi.listTickers"]?.[h]?.url;
      return (A, C) => U(y, axios_default, I, l)(A, _ || C);
    });
  } };
};
var vn = function(l, a, e) {
  let i = D(l);
  return { deprecatedGetCryptoSnapshotTickerBook(t, r) {
    return i.deprecatedGetCryptoSnapshotTickerBook(t, r).then((n) => n(e, a));
  }, deprecatedGetHistoricCryptoTrades(t, r, n, s, o, g) {
    return i.deprecatedGetHistoricCryptoTrades(t, r, n, s, o, g).then((u) => u(e, a));
  }, deprecatedGetHistoricForexQuotes(t, r, n, s, o, g) {
    return i.deprecatedGetHistoricForexQuotes(t, r, n, s, o, g).then((u) => u(e, a));
  }, deprecatedGetHistoricStocksQuotes(t, r, n, s, o, g, u) {
    return i.deprecatedGetHistoricStocksQuotes(t, r, n, s, o, g, u).then((c) => c(e, a));
  }, deprecatedGetHistoricStocksTrades(t, r, n, s, o, g, u) {
    return i.deprecatedGetHistoricStocksTrades(t, r, n, s, o, g, u).then((c) => c(e, a));
  }, getBenzingaV1AnalystInsights(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re) {
    return i.getBenzingaV1AnalystInsights(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re).then((ge) => ge(e, a));
  }, getBenzingaV1Analysts(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j) {
    return i.getBenzingaV1Analysts(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j).then((N) => N(e, a));
  }, getBenzingaV1ConsensusRatings(t, r, n, s, o, g, u, c) {
    return i.getBenzingaV1ConsensusRatings(t, r, n, s, o, g, u, c).then((d) => d(e, a));
  }, getBenzingaV1Earnings(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be, he, fe) {
    return i.getBenzingaV1Earnings(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be, he, fe).then((Re) => Re(e, a));
  }, getBenzingaV1Firms(t, r, n, s, o, g, u, c, d) {
    return i.getBenzingaV1Firms(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getBenzingaV1Guidance(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re) {
    return i.getBenzingaV1Guidance(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re).then((ge) => ge(e, a));
  }, getBenzingaV1News(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O) {
    return i.getBenzingaV1News(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O).then((E) => E(e, a));
  }, getBenzingaV1Ratings(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be, he, fe) {
    return i.getBenzingaV1Ratings(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W, be, he, fe).then((Re) => Re(e, a));
  }, getCryptoAggregates(t, r, n, s, o, g, u, c, d) {
    return i.getCryptoAggregates(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getCryptoEMA(t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return i.getCryptoEMA(t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(e, a));
  }, getCryptoMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x) {
    return i.getCryptoMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x).then((y) => y(e, a));
  }, getCryptoOpenClose(t, r, n, s, o) {
    return i.getCryptoOpenClose(t, r, n, s, o).then((g) => g(e, a));
  }, getCryptoRSI(t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return i.getCryptoRSI(t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(e, a));
  }, getCryptoSMA(t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return i.getCryptoSMA(t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(e, a));
  }, getCryptoSnapshotDirection(t, r) {
    return i.getCryptoSnapshotDirection(t, r).then((n) => n(e, a));
  }, getCryptoSnapshotTicker(t, r) {
    return i.getCryptoSnapshotTicker(t, r).then((n) => n(e, a));
  }, getCryptoSnapshotTickers(t, r) {
    return i.getCryptoSnapshotTickers(t, r).then((n) => n(e, a));
  }, getCryptoTrades(t, r, n, s, o, g, u, c, d, p) {
    return i.getCryptoTrades(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getCurrencyConversion(t, r, n, s, o) {
    return i.getCurrencyConversion(t, r, n, s, o).then((g) => g(e, a));
  }, getEvents(t, r, n) {
    return i.getEvents(t, r, n).then((s) => s(e, a));
  }, getFedV1Inflation(t, r, n, s, o, g, u, c, d) {
    return i.getFedV1Inflation(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getFedV1InflationExpectations(t, r, n, s, o, g, u, c, d) {
    return i.getFedV1InflationExpectations(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getFedV1TreasuryYields(t, r, n, s, o, g, u, c, d) {
    return i.getFedV1TreasuryYields(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getForexAggregates(t, r, n, s, o, g, u, c, d) {
    return i.getForexAggregates(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getForexEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getForexEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getForexMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y) {
    return i.getForexMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y).then((h) => h(e, a));
  }, getForexQuotes(t, r, n, s, o, g, u, c, d, p) {
    return i.getForexQuotes(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getForexRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getForexRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getForexSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getForexSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getForexSnapshotDirection(t, r) {
    return i.getForexSnapshotDirection(t, r).then((n) => n(e, a));
  }, getForexSnapshotTicker(t, r) {
    return i.getForexSnapshotTicker(t, r).then((n) => n(e, a));
  }, getForexSnapshotTickers(t, r) {
    return i.getForexSnapshotTickers(t, r).then((n) => n(e, a));
  }, getFuturesAggregates(t, r, n, s, o, g, u, c, d, p) {
    return i.getFuturesAggregates(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getFuturesContractDetails(t, r, n) {
    return i.getFuturesContractDetails(t, r, n).then((s) => s(e, a));
  }, getFuturesContracts(t, r, n, s, o, g, u, c, d) {
    return i.getFuturesContracts(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getFuturesDailySchedules(t, r, n, s, o) {
    return i.getFuturesDailySchedules(t, r, n, s, o).then((g) => g(e, a));
  }, getFuturesMarketStatuses(t, r, n, s, o) {
    return i.getFuturesMarketStatuses(t, r, n, s, o).then((g) => g(e, a));
  }, getFuturesProductDetails(t, r, n, s) {
    return i.getFuturesProductDetails(t, r, n, s).then((o) => o(e, a));
  }, getFuturesProductSchedules(t, r, n, s, o, g, u, c, d) {
    return i.getFuturesProductSchedules(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getFuturesProducts(t, r, n, s, o, g, u, c, d, p, m, f) {
    return i.getFuturesProducts(t, r, n, s, o, g, u, c, d, p, m, f).then((b) => b(e, a));
  }, getFuturesQuotes(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getFuturesQuotes(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getFuturesTrades(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getFuturesTrades(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getGroupedCryptoAggregates(t, r, n) {
    return i.getGroupedCryptoAggregates(t, r, n).then((s) => s(e, a));
  }, getGroupedForexAggregates(t, r, n) {
    return i.getGroupedForexAggregates(t, r, n).then((s) => s(e, a));
  }, getGroupedStocksAggregates(t, r, n, s) {
    return i.getGroupedStocksAggregates(t, r, n, s).then((o) => o(e, a));
  }, getIndicesAggregates(t, r, n, s, o, g, u, c) {
    return i.getIndicesAggregates(t, r, n, s, o, g, u, c).then((d) => d(e, a));
  }, getIndicesEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getIndicesEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getIndicesMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y) {
    return i.getIndicesMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y).then((h) => h(e, a));
  }, getIndicesOpenClose(t, r, n) {
    return i.getIndicesOpenClose(t, r, n).then((s) => s(e, a));
  }, getIndicesRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getIndicesRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getIndicesSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getIndicesSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getIndicesSnapshot(t, r, n, s, o, g, u, c, d, p) {
    return i.getIndicesSnapshot(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getLastCryptoTrade(t, r, n) {
    return i.getLastCryptoTrade(t, r, n).then((s) => s(e, a));
  }, getLastCurrencyQuote(t, r, n) {
    return i.getLastCurrencyQuote(t, r, n).then((s) => s(e, a));
  }, getLastOptionsTrade(t, r) {
    return i.getLastOptionsTrade(t, r).then((n) => n(e, a));
  }, getLastStocksQuote(t, r) {
    return i.getLastStocksQuote(t, r).then((n) => n(e, a));
  }, getLastStocksTrade(t, r) {
    return i.getLastStocksTrade(t, r).then((n) => n(e, a));
  }, getMarketHolidays(t) {
    return i.getMarketHolidays(t).then((r) => r(e, a));
  }, getMarketStatus(t) {
    return i.getMarketStatus(t).then((r) => r(e, a));
  }, getOptionContract(t, r, n) {
    return i.getOptionContract(t, r, n).then((s) => s(e, a));
  }, getOptionsAggregates(t, r, n, s, o, g, u, c, d) {
    return i.getOptionsAggregates(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getOptionsChain(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y) {
    return i.getOptionsChain(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y).then((h) => h(e, a));
  }, getOptionsContract(t, r, n) {
    return i.getOptionsContract(t, r, n).then((s) => s(e, a));
  }, getOptionsEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getOptionsEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getOptionsMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y) {
    return i.getOptionsMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y).then((h) => h(e, a));
  }, getOptionsOpenClose(t, r, n, s) {
    return i.getOptionsOpenClose(t, r, n, s).then((o) => o(e, a));
  }, getOptionsQuotes(t, r, n, s, o, g, u, c, d, p) {
    return i.getOptionsQuotes(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getOptionsRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getOptionsRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getOptionsSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getOptionsSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getOptionsTrades(t, r, n, s, o, g, u, c, d, p) {
    return i.getOptionsTrades(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getPreviousCryptoAggregates(t, r, n) {
    return i.getPreviousCryptoAggregates(t, r, n).then((s) => s(e, a));
  }, getPreviousForexAggregates(t, r, n) {
    return i.getPreviousForexAggregates(t, r, n).then((s) => s(e, a));
  }, getPreviousIndicesAggregates(t, r) {
    return i.getPreviousIndicesAggregates(t, r).then((n) => n(e, a));
  }, getPreviousOptionsAggregates(t, r, n) {
    return i.getPreviousOptionsAggregates(t, r, n).then((s) => s(e, a));
  }, getPreviousStocksAggregates(t, r, n) {
    return i.getPreviousStocksAggregates(t, r, n).then((s) => s(e, a));
  }, getRelatedCompanies(t, r) {
    return i.getRelatedCompanies(t, r).then((n) => n(e, a));
  }, getSnapshotSummary(t, r) {
    return i.getSnapshotSummary(t, r).then((n) => n(e, a));
  }, getSnapshots(t, r, n, s, o, g, u, c, d, p, m) {
    return i.getSnapshots(t, r, n, s, o, g, u, c, d, p, m).then((f) => f(e, a));
  }, getStocksAggregates(t, r, n, s, o, g, u, c, d) {
    return i.getStocksAggregates(t, r, n, s, o, g, u, c, d).then((p) => p(e, a));
  }, getStocksEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getStocksEMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getStocksMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y) {
    return i.getStocksMACD(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y).then((h) => h(e, a));
  }, getStocksOpenClose(t, r, n, s) {
    return i.getStocksOpenClose(t, r, n, s).then((o) => o(e, a));
  }, getStocksQuotes(t, r, n, s, o, g, u, c, d, p) {
    return i.getStocksQuotes(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getStocksRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getStocksRSI(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getStocksSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.getStocksSMA(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, getStocksSnapshotDirection(t, r, n) {
    return i.getStocksSnapshotDirection(t, r, n).then((s) => s(e, a));
  }, getStocksSnapshotTicker(t, r) {
    return i.getStocksSnapshotTicker(t, r).then((n) => n(e, a));
  }, getStocksSnapshotTickers(t, r, n) {
    return i.getStocksSnapshotTickers(t, r, n).then((s) => s(e, a));
  }, getStocksTrades(t, r, n, s, o, g, u, c, d, p) {
    return i.getStocksTrades(t, r, n, s, o, g, u, c, d, p).then((m) => m(e, a));
  }, getStocksV1ShortInterest(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j) {
    return i.getStocksV1ShortInterest(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j).then((N) => N(e, a));
  }, getStocksV1ShortVolume(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j) {
    return i.getStocksV1ShortVolume(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j).then((N) => N(e, a));
  }, getTicker(t, r, n) {
    return i.getTicker(t, r, n).then((s) => s(e, a));
  }, getTmxV1CorporateEvents(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le) {
    return i.getTmxV1CorporateEvents(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le).then((pe) => pe(e, a));
  }, listConditions(t, r, n, s, o, g, u, c) {
    return i.listConditions(t, r, n, s, o, g, u, c).then((d) => d(e, a));
  }, listDividends(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J) {
    return i.listDividends(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J).then((ee) => ee(e, a));
  }, listExchanges(t, r, n) {
    return i.listExchanges(t, r, n).then((s) => s(e, a));
  }, listFinancials(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V) {
    return i.listFinancials(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V).then((Q) => Q(e, a));
  }, listIPOs(t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return i.listIPOs(t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(e, a));
  }, listNews(t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return i.listNews(t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(e, a));
  }, listOptionsContracts(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H) {
    return i.listOptionsContracts(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H).then((P) => P(e, a));
  }, listStockSplits(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x) {
    return i.listStockSplits(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x).then((y) => y(e, a));
  }, listTickerTypes(t, r, n) {
    return i.listTickerTypes(t, r, n).then((s) => s(e, a));
  }, listTickers(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h) {
    return i.listTickers(t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h).then((_) => _(e, a));
  } };
};
var Ce = class extends Ae {
  deprecatedGetCryptoSnapshotTickerBook(a, e) {
    return D(this.configuration).deprecatedGetCryptoSnapshotTickerBook(a, e).then((i) => i(this.axios, this.basePath));
  }
  deprecatedGetHistoricCryptoTrades(a, e, i, t, r, n) {
    return D(this.configuration).deprecatedGetHistoricCryptoTrades(a, e, i, t, r, n).then((s) => s(this.axios, this.basePath));
  }
  deprecatedGetHistoricForexQuotes(a, e, i, t, r, n) {
    return D(this.configuration).deprecatedGetHistoricForexQuotes(a, e, i, t, r, n).then((s) => s(this.axios, this.basePath));
  }
  deprecatedGetHistoricStocksQuotes(a, e, i, t, r, n, s) {
    return D(this.configuration).deprecatedGetHistoricStocksQuotes(a, e, i, t, r, n, s).then((o) => o(this.axios, this.basePath));
  }
  deprecatedGetHistoricStocksTrades(a, e, i, t, r, n, s) {
    return D(this.configuration).deprecatedGetHistoricStocksTrades(a, e, i, t, r, n, s).then((o) => o(this.axios, this.basePath));
  }
  getBenzingaV1AnalystInsights(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie) {
    return D(this.configuration).getBenzingaV1AnalystInsights(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie).then((ae) => ae(this.axios, this.basePath));
  }
  getBenzingaV1Analysts(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P) {
    return D(this.configuration).getBenzingaV1Analysts(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P).then((z) => z(this.axios, this.basePath));
  }
  getBenzingaV1ConsensusRatings(a, e, i, t, r, n, s, o) {
    return D(this.configuration).getBenzingaV1ConsensusRatings(a, e, i, t, r, n, s, o).then((g) => g(this.axios, this.basePath));
  }
  getBenzingaV1Earnings(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W) {
    return D(this.configuration).getBenzingaV1Earnings(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W).then((be) => be(this.axios, this.basePath));
  }
  getBenzingaV1Firms(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getBenzingaV1Firms(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getBenzingaV1Guidance(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie) {
    return D(this.configuration).getBenzingaV1Guidance(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie).then((ae) => ae(this.axios, this.basePath));
  }
  getBenzingaV1News(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j) {
    return D(this.configuration).getBenzingaV1News(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j).then((N) => N(this.axios, this.basePath));
  }
  getBenzingaV1Ratings(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W) {
    return D(this.configuration).getBenzingaV1Ratings(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S, ce, de, le, pe, me, W).then((be) => be(this.axios, this.basePath));
  }
  getCryptoAggregates(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getCryptoAggregates(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getCryptoEMA(a, e, i, t, r, n, s, o, g, u, c, d, p) {
    return D(this.configuration).getCryptoEMA(a, e, i, t, r, n, s, o, g, u, c, d, p).then((m) => m(this.axios, this.basePath));
  }
  getCryptoMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return D(this.configuration).getCryptoMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f).then((b) => b(this.axios, this.basePath));
  }
  getCryptoOpenClose(a, e, i, t, r) {
    return D(this.configuration).getCryptoOpenClose(a, e, i, t, r).then((n) => n(this.axios, this.basePath));
  }
  getCryptoRSI(a, e, i, t, r, n, s, o, g, u, c, d, p) {
    return D(this.configuration).getCryptoRSI(a, e, i, t, r, n, s, o, g, u, c, d, p).then((m) => m(this.axios, this.basePath));
  }
  getCryptoSMA(a, e, i, t, r, n, s, o, g, u, c, d, p) {
    return D(this.configuration).getCryptoSMA(a, e, i, t, r, n, s, o, g, u, c, d, p).then((m) => m(this.axios, this.basePath));
  }
  getCryptoSnapshotDirection(a, e) {
    return D(this.configuration).getCryptoSnapshotDirection(a, e).then((i) => i(this.axios, this.basePath));
  }
  getCryptoSnapshotTicker(a, e) {
    return D(this.configuration).getCryptoSnapshotTicker(a, e).then((i) => i(this.axios, this.basePath));
  }
  getCryptoSnapshotTickers(a, e) {
    return D(this.configuration).getCryptoSnapshotTickers(a, e).then((i) => i(this.axios, this.basePath));
  }
  getCryptoTrades(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getCryptoTrades(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getCurrencyConversion(a, e, i, t, r) {
    return D(this.configuration).getCurrencyConversion(a, e, i, t, r).then((n) => n(this.axios, this.basePath));
  }
  getEvents(a, e, i) {
    return D(this.configuration).getEvents(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getFedV1Inflation(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getFedV1Inflation(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getFedV1InflationExpectations(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getFedV1InflationExpectations(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getFedV1TreasuryYields(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getFedV1TreasuryYields(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getForexAggregates(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getForexAggregates(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getForexEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getForexEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getForexMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return D(this.configuration).getForexMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(this.axios, this.basePath));
  }
  getForexQuotes(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getForexQuotes(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getForexRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getForexRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getForexSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getForexSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getForexSnapshotDirection(a, e) {
    return D(this.configuration).getForexSnapshotDirection(a, e).then((i) => i(this.axios, this.basePath));
  }
  getForexSnapshotTicker(a, e) {
    return D(this.configuration).getForexSnapshotTicker(a, e).then((i) => i(this.axios, this.basePath));
  }
  getForexSnapshotTickers(a, e) {
    return D(this.configuration).getForexSnapshotTickers(a, e).then((i) => i(this.axios, this.basePath));
  }
  getFuturesAggregates(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getFuturesAggregates(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getFuturesContractDetails(a, e, i) {
    return D(this.configuration).getFuturesContractDetails(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getFuturesContracts(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getFuturesContracts(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getFuturesDailySchedules(a, e, i, t, r) {
    return D(this.configuration).getFuturesDailySchedules(a, e, i, t, r).then((n) => n(this.axios, this.basePath));
  }
  getFuturesMarketStatuses(a, e, i, t, r) {
    return D(this.configuration).getFuturesMarketStatuses(a, e, i, t, r).then((n) => n(this.axios, this.basePath));
  }
  getFuturesProductDetails(a, e, i, t) {
    return D(this.configuration).getFuturesProductDetails(a, e, i, t).then((r) => r(this.axios, this.basePath));
  }
  getFuturesProductSchedules(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getFuturesProductSchedules(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getFuturesProducts(a, e, i, t, r, n, s, o, g, u, c, d) {
    return D(this.configuration).getFuturesProducts(a, e, i, t, r, n, s, o, g, u, c, d).then((p) => p(this.axios, this.basePath));
  }
  getFuturesQuotes(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getFuturesQuotes(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getFuturesTrades(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getFuturesTrades(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getGroupedCryptoAggregates(a, e, i) {
    return D(this.configuration).getGroupedCryptoAggregates(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getGroupedForexAggregates(a, e, i) {
    return D(this.configuration).getGroupedForexAggregates(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getGroupedStocksAggregates(a, e, i, t) {
    return D(this.configuration).getGroupedStocksAggregates(a, e, i, t).then((r) => r(this.axios, this.basePath));
  }
  getIndicesAggregates(a, e, i, t, r, n, s, o) {
    return D(this.configuration).getIndicesAggregates(a, e, i, t, r, n, s, o).then((g) => g(this.axios, this.basePath));
  }
  getIndicesEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getIndicesEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getIndicesMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return D(this.configuration).getIndicesMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(this.axios, this.basePath));
  }
  getIndicesOpenClose(a, e, i) {
    return D(this.configuration).getIndicesOpenClose(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getIndicesRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getIndicesRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getIndicesSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getIndicesSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getIndicesSnapshot(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getIndicesSnapshot(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getLastCryptoTrade(a, e, i) {
    return D(this.configuration).getLastCryptoTrade(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getLastCurrencyQuote(a, e, i) {
    return D(this.configuration).getLastCurrencyQuote(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getLastOptionsTrade(a, e) {
    return D(this.configuration).getLastOptionsTrade(a, e).then((i) => i(this.axios, this.basePath));
  }
  getLastStocksQuote(a, e) {
    return D(this.configuration).getLastStocksQuote(a, e).then((i) => i(this.axios, this.basePath));
  }
  getLastStocksTrade(a, e) {
    return D(this.configuration).getLastStocksTrade(a, e).then((i) => i(this.axios, this.basePath));
  }
  getMarketHolidays(a) {
    return D(this.configuration).getMarketHolidays(a).then((e) => e(this.axios, this.basePath));
  }
  getMarketStatus(a) {
    return D(this.configuration).getMarketStatus(a).then((e) => e(this.axios, this.basePath));
  }
  getOptionContract(a, e, i) {
    return D(this.configuration).getOptionContract(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getOptionsAggregates(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getOptionsAggregates(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getOptionsChain(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return D(this.configuration).getOptionsChain(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(this.axios, this.basePath));
  }
  getOptionsContract(a, e, i) {
    return D(this.configuration).getOptionsContract(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getOptionsEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getOptionsEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getOptionsMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return D(this.configuration).getOptionsMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(this.axios, this.basePath));
  }
  getOptionsOpenClose(a, e, i, t) {
    return D(this.configuration).getOptionsOpenClose(a, e, i, t).then((r) => r(this.axios, this.basePath));
  }
  getOptionsQuotes(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getOptionsQuotes(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getOptionsRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getOptionsRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getOptionsSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getOptionsSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getOptionsTrades(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getOptionsTrades(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getPreviousCryptoAggregates(a, e, i) {
    return D(this.configuration).getPreviousCryptoAggregates(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getPreviousForexAggregates(a, e, i) {
    return D(this.configuration).getPreviousForexAggregates(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getPreviousIndicesAggregates(a, e) {
    return D(this.configuration).getPreviousIndicesAggregates(a, e).then((i) => i(this.axios, this.basePath));
  }
  getPreviousOptionsAggregates(a, e, i) {
    return D(this.configuration).getPreviousOptionsAggregates(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getPreviousStocksAggregates(a, e, i) {
    return D(this.configuration).getPreviousStocksAggregates(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getRelatedCompanies(a, e) {
    return D(this.configuration).getRelatedCompanies(a, e).then((i) => i(this.axios, this.basePath));
  }
  getSnapshotSummary(a, e) {
    return D(this.configuration).getSnapshotSummary(a, e).then((i) => i(this.axios, this.basePath));
  }
  getSnapshots(a, e, i, t, r, n, s, o, g, u, c) {
    return D(this.configuration).getSnapshots(a, e, i, t, r, n, s, o, g, u, c).then((d) => d(this.axios, this.basePath));
  }
  getStocksAggregates(a, e, i, t, r, n, s, o, g) {
    return D(this.configuration).getStocksAggregates(a, e, i, t, r, n, s, o, g).then((u) => u(this.axios, this.basePath));
  }
  getStocksEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getStocksEMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getStocksMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b) {
    return D(this.configuration).getStocksMACD(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b).then((R) => R(this.axios, this.basePath));
  }
  getStocksOpenClose(a, e, i, t) {
    return D(this.configuration).getStocksOpenClose(a, e, i, t).then((r) => r(this.axios, this.basePath));
  }
  getStocksQuotes(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getStocksQuotes(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getStocksRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getStocksRSI(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getStocksSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).getStocksSMA(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  getStocksSnapshotDirection(a, e, i) {
    return D(this.configuration).getStocksSnapshotDirection(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getStocksSnapshotTicker(a, e) {
    return D(this.configuration).getStocksSnapshotTicker(a, e).then((i) => i(this.axios, this.basePath));
  }
  getStocksSnapshotTickers(a, e, i) {
    return D(this.configuration).getStocksSnapshotTickers(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getStocksTrades(a, e, i, t, r, n, s, o, g, u) {
    return D(this.configuration).getStocksTrades(a, e, i, t, r, n, s, o, g, u).then((c) => c(this.axios, this.basePath));
  }
  getStocksV1ShortInterest(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P) {
    return D(this.configuration).getStocksV1ShortInterest(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P).then((z) => z(this.axios, this.basePath));
  }
  getStocksV1ShortVolume(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P) {
    return D(this.configuration).getStocksV1ShortVolume(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P).then((z) => z(this.axios, this.basePath));
  }
  getTicker(a, e, i) {
    return D(this.configuration).getTicker(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  getTmxV1CorporateEvents(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S) {
    return D(this.configuration).getTmxV1CorporateEvents(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K, X, Z, J, ee, te, $, se, ne, ie, ae, oe, re, ge, ue, S).then((ce) => ce(this.axios, this.basePath));
  }
  listConditions(a, e, i, t, r, n, s, o) {
    return D(this.configuration).listConditions(a, e, i, t, r, n, s, o).then((g) => g(this.axios, this.basePath));
  }
  listDividends(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K) {
    return D(this.configuration).listDividends(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C, V, Q, H, P, z, w, j, N, L, O, E, Y, K).then((X) => X(this.axios, this.basePath));
  }
  listExchanges(a, e, i) {
    return D(this.configuration).listExchanges(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  listFinancials(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _) {
    return D(this.configuration).listFinancials(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _).then((A) => A(this.axios, this.basePath));
  }
  listIPOs(a, e, i, t, r, n, s, o, g, u, c, d, p) {
    return D(this.configuration).listIPOs(a, e, i, t, r, n, s, o, g, u, c, d, p).then((m) => m(this.axios, this.basePath));
  }
  listNews(a, e, i, t, r, n, s, o, g, u, c, d, p, m) {
    return D(this.configuration).listNews(a, e, i, t, r, n, s, o, g, u, c, d, p, m).then((f) => f(this.axios, this.basePath));
  }
  listOptionsContracts(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C) {
    return D(this.configuration).listOptionsContracts(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R, x, y, h, _, A, C).then((V) => V(this.axios, this.basePath));
  }
  listStockSplits(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f) {
    return D(this.configuration).listStockSplits(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f).then((b) => b(this.axios, this.basePath));
  }
  listTickerTypes(a, e, i) {
    return D(this.configuration).listTickerTypes(a, e, i).then((t) => t(this.axios, this.basePath));
  }
  listTickers(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R) {
    return D(this.configuration).listTickers(a, e, i, t, r, n, s, o, g, u, c, d, p, m, f, b, R).then((x) => x(this.axios, this.basePath));
  }
};
var Ft = ((o) => (o.Second = "second", o.Minute = "minute", o.Hour = "hour", o.Day = "day", o.Week = "week", o.Month = "month", o.Quarter = "quarter", o.Year = "year", o))(Ft || {});
var Gt = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Gt || {});
var Bt = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Bt || {});
var Ut = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Ut || {});
var Dt = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Dt || {});
var Mt = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Mt || {});
var Qt = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Qt || {});
var Ht = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Ht || {});
var Et = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Et || {});
var zt = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(zt || {});
var jt = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(jt || {});
var Kt = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Kt || {});
var $t = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))($t || {});
var Nt = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Nt || {});
var Lt = ((e) => (e.Gainers = "gainers", e.Losers = "losers", e))(Lt || {});
var Wt = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Wt || {});
var Yt = ((a) => (a.Timestamp = "timestamp", a))(Yt || {});
var Xt = ((r) => (r[r.NUMBER_0 = 0] = "NUMBER_0", r[r.NUMBER_1 = 1] = "NUMBER_1", r[r.NUMBER_2 = 2] = "NUMBER_2", r[r.NUMBER_3 = 3] = "NUMBER_3", r[r.NUMBER_4 = 4] = "NUMBER_4", r))(Xt || {});
var Jt = ((o) => (o.Second = "second", o.Minute = "minute", o.Hour = "hour", o.Day = "day", o.Week = "week", o.Month = "month", o.Quarter = "quarter", o.Year = "year", o))(Jt || {});
var Zt = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Zt || {});
var es = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(es || {});
var ts = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(ts || {});
var ss = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(ss || {});
var rs = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(rs || {});
var ns = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(ns || {});
var is = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(is || {});
var as = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(as || {});
var os = ((a) => (a.Timestamp = "timestamp", a))(os || {});
var gs = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(gs || {});
var us = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(us || {});
var cs = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(cs || {});
var ls = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(ls || {});
var ds = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(ds || {});
var ps = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(ps || {});
var ms = ((e) => (e.Gainers = "gainers", e.Losers = "losers", e))(ms || {});
var fs = ((e) => (e.WindowStartAsc = "window_start.asc", e.WindowStartDesc = "window_start.desc", e))(fs || {});
var bs = ((i) => (i.All = "all", i.True = "true", i.False = "false", i))(bs || {});
var hs = ((i) => (i.All = "all", i.Single = "single", i.Combo = "combo", i))(hs || {});
var Rs = ((n) => (n.ProductCodeAsc = "product_code.asc", n.ProductCodeDesc = "product_code.desc", n.FirstTradeDateAsc = "first_trade_date.asc", n.FirstTradeDateDesc = "first_trade_date.desc", n.LastTradeDateAsc = "last_trade_date.asc", n.LastTradeDateDesc = "last_trade_date.desc", n))(Rs || {});
var xs = ((e) => (e.TradingVenueAsc = "trading_venue.asc", e.TradingVenueDesc = "trading_venue.desc", e))(xs || {});
var ys = ((e) => (e.ProductCodeAsc = "product_code.asc", e.ProductCodeDesc = "product_code.desc", e))(ys || {});
var As = ((e) => (e.Single = "single", e.Combo = "combo", e))(As || {});
var _s = ((e) => (e.SessionEndDateAsc = "session_end_date.asc", e.SessionEndDateDesc = "session_end_date.desc", e))(_s || {});
var Cs = ((E) => (E.Asia = "asia", E.Base = "base", E.Biofuels = "biofuels", E.Coal = "coal", E.CrossRates = "cross_rates", E.CrudeOil = "crude_oil", E.CustomIndex = "custom_index", E.Dairy = "dairy", E.DjUbsCi = "dj_ubs_ci", E.Electricity = "electricity", E.Emissions = "emissions", E.Europe = "europe", E.Fertilizer = "fertilizer", E.Forestry = "forestry", E.GrainsAndOilseeds = "grains_and_oilseeds", E.IntlIndex = "intl_index", E.LiqNatGasLng = "liq_nat_gas_lng", E.Livestock = "livestock", E.LongTermGov = "long_term_gov", E.LongTermNonGov = "long_term_non_gov", E.Majors = "majors", E.Minors = "minors", E.NatGas = "nat_gas", E.NatGasLiqPetro = "nat_gas_liq_petro", E.Precious = "precious", E.RefinedProducts = "refined_products", E.SAndPGsci = "s_and_p_gsci", E.SelSectorIndex = "sel_sector_index", E.ShortTermGov = "short_term_gov", E.ShortTermNonGov = "short_term_non_gov", E.Softs = "softs", E.Us = "us", E.UsIndex = "us_index", E.WetBulk = "wet_bulk", E))(Cs || {});
var Os = ((x) => (x.Asian = "asian", x.Canadian = "canadian", x.Cat = "cat", x.CoolingDegreeDays = "cooling_degree_days", x.Ercot = "ercot", x.European = "european", x.Gulf = "gulf", x.HeatingDegreeDays = "heating_degree_days", x.IsoNe = "iso_ne", x.LargeCapIndex = "large_cap_index", x.MidCapIndex = "mid_cap_index", x.Miso = "miso", x.NorthAmerican = "north_american", x.Nyiso = "nyiso", x.Pjm = "pjm", x.SmallCapIndex = "small_cap_index", x.West = "west", x.WesternPower = "western_power", x))(Os || {});
var Ss = ((i) => (i.AltInvestment = "alt_investment", i.Commodity = "commodity", i.Financials = "financials", i))(Ss || {});
var Ps = ((u) => (u.Agricultural = "agricultural", u.CommodityIndex = "commodity_index", u.Energy = "energy", u.Equity = "equity", u.ForeignExchange = "foreign_exchange", u.Freight = "freight", u.Housing = "housing", u.InterestRate = "interest_rate", u.Metals = "metals", u.Weather = "weather", u))(Ps || {});
var ks = ((i) => (i.All = "all", i.Single = "single", i.Combo = "combo", i))(ks || {});
var Vs = ((m) => (m.NameAsc = "name.asc", m.NameDesc = "name.desc", m.TradingVenueAsc = "trading_venue.asc", m.TradingVenueDesc = "trading_venue.desc", m.SectorAsc = "sector.asc", m.SectorDesc = "sector.desc", m.SubSectorAsc = "sub_sector.asc", m.SubSectorDesc = "sub_sector.desc", m.AssetClassAsc = "asset_class.asc", m.AssetClassDesc = "asset_class.desc", m.AssetSubClassAsc = "asset_sub_class.asc", m.AssetSubClassDesc = "asset_sub_class.desc", m.TypeAsc = "type.asc", m.TypeDesc = "type.desc", m))(Vs || {});
var ws = ((e) => (e.TimestampAsc = "timestamp.asc", e.TimestampDesc = "timestamp.desc", e))(ws || {});
var Is = ((e) => (e.TimestampAsc = "timestamp.asc", e.TimestampDesc = "timestamp.desc", e))(Is || {});
var vs = ((o) => (o.Second = "second", o.Minute = "minute", o.Hour = "hour", o.Day = "day", o.Week = "week", o.Month = "month", o.Quarter = "quarter", o.Year = "year", o))(vs || {});
var qs = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(qs || {});
var Ts = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Ts || {});
var Fs = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Fs || {});
var Gs = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Gs || {});
var Bs = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Bs || {});
var Us = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Us || {});
var Ds = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Ds || {});
var Ms = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Ms || {});
var Qs = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Qs || {});
var Hs = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Hs || {});
var Es = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Es || {});
var zs = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(zs || {});
var js = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(js || {});
var Ks = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Ks || {});
var $s = ((a) => (a.Ticker = "ticker", a))($s || {});
var Ns = ((o) => (o.Second = "second", o.Minute = "minute", o.Hour = "hour", o.Day = "day", o.Week = "week", o.Month = "month", o.Quarter = "quarter", o.Year = "year", o))(Ns || {});
var Ls = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Ls || {});
var Ws = ((e) => (e.Call = "call", e.Put = "put", e))(Ws || {});
var Ys = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Ys || {});
var Xs = ((i) => (i.Ticker = "ticker", i.ExpirationDate = "expiration_date", i.StrikePrice = "strike_price", i))(Xs || {});
var Js = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Js || {});
var Zs = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Zs || {});
var er = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(er || {});
var tr = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(tr || {});
var sr = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(sr || {});
var rr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(rr || {});
var nr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(nr || {});
var ir = ((a) => (a.Timestamp = "timestamp", a))(ir || {});
var ar = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(ar || {});
var or = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(or || {});
var gr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(gr || {});
var ur = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(ur || {});
var cr = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(cr || {});
var lr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(lr || {});
var dr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(dr || {});
var pr = ((a) => (a.Timestamp = "timestamp", a))(pr || {});
var mr = ((r) => (r.Stocks = "stocks", r.Options = "options", r.Crypto = "crypto", r.Fx = "fx", r.Indices = "indices", r))(mr || {});
var fr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(fr || {});
var br = ((a) => (a.Ticker = "ticker", a))(br || {});
var hr = ((o) => (o.Second = "second", o.Minute = "minute", o.Hour = "hour", o.Day = "day", o.Week = "week", o.Month = "month", o.Quarter = "quarter", o.Year = "year", o))(hr || {});
var Rr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Rr || {});
var xr = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(xr || {});
var yr = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(yr || {});
var Ar = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Ar || {});
var _r = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(_r || {});
var Cr = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Cr || {});
var Or = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Or || {});
var Sr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Sr || {});
var Pr = ((a) => (a.Timestamp = "timestamp", a))(Pr || {});
var kr = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(kr || {});
var Vr = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(Vr || {});
var wr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(wr || {});
var Ir = ((s) => (s.Minute = "minute", s.Hour = "hour", s.Day = "day", s.Week = "week", s.Month = "month", s.Quarter = "quarter", s.Year = "year", s))(Ir || {});
var vr = ((t) => (t.Open = "open", t.High = "high", t.Low = "low", t.Close = "close", t))(vr || {});
var qr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(qr || {});
var Tr = ((e) => (e.Gainers = "gainers", e.Losers = "losers", e))(Tr || {});
var Fr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Fr || {});
var Gr = ((a) => (a.Timestamp = "timestamp", a))(Gr || {});
var Br = ((t) => (t.Stocks = "stocks", t.Options = "options", t.Crypto = "crypto", t.Fx = "fx", t))(Br || {});
var Ur = ((i) => (i.Trade = "trade", i.Bbo = "bbo", i.Nbbo = "nbbo", i))(Ur || {});
var Dr = ((i) => (i.Cta = "CTA", i.Utp = "UTP", i.Opra = "OPRA", i))(Dr || {});
var Mr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Mr || {});
var Qr = ((n) => (n.AssetClass = "asset_class", n.Id = "id", n.Type = "type", n.Name = "name", n.DataTypes = "data_types", n.Legacy = "legacy", n))(Qr || {});
var Hr = ((s) => (s[s.NUMBER_0 = 0] = "NUMBER_0", s[s.NUMBER_1 = 1] = "NUMBER_1", s[s.NUMBER_2 = 2] = "NUMBER_2", s[s.NUMBER_4 = 4] = "NUMBER_4", s[s.NUMBER_12 = 12] = "NUMBER_12", s[s.NUMBER_24 = 24] = "NUMBER_24", s[s.NUMBER_52 = 52] = "NUMBER_52", s))(Hr || {});
var Er = ((t) => (t.Cd = "CD", t.Sc = "SC", t.Lt = "LT", t.St = "ST", t))(Er || {});
var zr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(zr || {});
var jr = ((n) => (n.ExDividendDate = "ex_dividend_date", n.PayDate = "pay_date", n.DeclarationDate = "declaration_date", n.RecordDate = "record_date", n.CashAmount = "cash_amount", n.Ticker = "ticker", n))(jr || {});
var Kr = ((r) => (r.Stocks = "stocks", r.Options = "options", r.Crypto = "crypto", r.Fx = "fx", r.Futures = "futures", r))(Kr || {});
var $r = ((e) => (e.Us = "us", e.Global = "global", e))($r || {});
var Nr = ((i) => (i.Annual = "annual", i.Quarterly = "quarterly", i.Ttm = "ttm", i))(Nr || {});
var Lr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Lr || {});
var Wr = ((e) => (e.FilingDate = "filing_date", e.PeriodOfReportDate = "period_of_report_date", e))(Wr || {});
var Yr = ((s) => (s.DirectListingProcess = "direct_listing_process", s.History = "history", s.New = "new", s.Pending = "pending", s.Postponed = "postponed", s.Rumor = "rumor", s.Withdrawn = "withdrawn", s))(Yr || {});
var Xr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Xr || {});
var Jr = ((h) => (h.ListingDate = "listing_date", h.Ticker = "ticker", h.LastUpdated = "last_updated", h.SecurityType = "security_type", h.IssuerName = "issuer_name", h.CurrencyCode = "currency_code", h.Isin = "isin", h.UsCode = "us_code", h.FinalIssuePrice = "final_issue_price", h.MinSharesOffered = "min_shares_offered", h.MaxSharesOffered = "max_shares_offered", h.LowestOfferPrice = "lowest_offer_price", h.HighestOfferPrice = "highest_offer_price", h.TotalOfferSize = "total_offer_size", h.SharesOutstanding = "shares_outstanding", h.PrimaryExchange = "primary_exchange", h.LotSize = "lot_size", h.SecurityDescription = "security_description", h.IpoStatus = "ipo_status", h.AnnouncedDate = "announced_date", h))(Jr || {});
var Zr = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(Zr || {});
var en = ((a) => (a.PublishedUtc = "published_utc", a))(en || {});
var tn = ((e) => (e.Call = "call", e.Put = "put", e))(tn || {});
var sn = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(sn || {});
var rn = ((t) => (t.Ticker = "ticker", t.UnderlyingTicker = "underlying_ticker", t.ExpirationDate = "expiration_date", t.StrikePrice = "strike_price", t))(rn || {});
var nn = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(nn || {});
var an = ((e) => (e.ExecutionDate = "execution_date", e.Ticker = "ticker", e))(an || {});
var on = ((r) => (r.Stocks = "stocks", r.Options = "options", r.Crypto = "crypto", r.Fx = "fx", r.Indices = "indices", r))(on || {});
var gn = ((e) => (e.Us = "us", e.Global = "global", e))(gn || {});
var un = ((C) => (C.Cs = "CS", C.Adrc = "ADRC", C.Adrp = "ADRP", C.Adrr = "ADRR", C.Unit = "UNIT", C.Right = "RIGHT", C.Pfd = "PFD", C.Fund = "FUND", C.Sp = "SP", C.Warrant = "WARRANT", C.Index = "INDEX", C.Etf = "ETF", C.Etn = "ETN", C.Os = "OS", C.Gdr = "GDR", C.Other = "OTHER", C.Nyrs = "NYRS", C.Agen = "AGEN", C.Eqlk = "EQLK", C.Bond = "BOND", C.Adrw = "ADRW", C.Basket = "BASKET", C.Lt = "LT", C))(un || {});
var cn = ((r) => (r.Stocks = "stocks", r.Crypto = "crypto", r.Fx = "fx", r.Otc = "otc", r.Indices = "indices", r))(cn || {});
var ln = ((e) => (e.Asc = "asc", e.Desc = "desc", e))(ln || {});
var dn = ((f) => (f.Ticker = "ticker", f.Name = "name", f.Market = "market", f.Locale = "locale", f.PrimaryExchange = "primary_exchange", f.Type = "type", f.CurrencySymbol = "currency_symbol", f.CurrencyName = "currency_name", f.BaseCurrencySymbol = "base_currency_symbol", f.BaseCurrencyName = "base_currency_name", f.Cik = "cik", f.CompositeFigi = "composite_figi", f.ShareClassFigi = "share_class_figi", f.LastUpdatedUtc = "last_updated_utc", f.DelistedUtc = "delisted_utc", f))(dn || {});
var Oe = class {
  constructor(a = {}) {
    this.apiKey = a.apiKey, this.username = a.username, this.password = a.password, this.accessToken = a.accessToken, this.basePath = a.basePath, this.serverIndex = a.serverIndex, this.baseOptions = __spreadProps(__spreadValues({}, a.baseOptions), { headers: __spreadValues({}, a.baseOptions?.headers) }), this.formDataCtor = a.formDataCtor;
  }
  isJsonMime(a) {
    let e = new RegExp("^(application/json|[^;/ 	]+/[^;/ 	]+[+]json)[ 	]*(;.*)?$", "i");
    return a !== null && (e.test(a) || a.toLowerCase() === "application/json-patch+json");
  }
};
var xe = (l, a) => {
  if (!a) throw new Error("api key not provided.");
  let e = new import_websocket.default.w3cwebsocket(l);
  return e.onopen = () => {
    e.send(JSON.stringify({ action: "auth", params: a }));
  }, e;
};
var ke = (l, a = "wss://socket.polygon.io") => xe(`${a}/crypto`, l);
var Ve = (l, a = "wss://socket.polygon.io") => xe(`${a}/forex`, l);
var we = (l, a = "wss://socket.polygon.io") => xe(`${a}/indices`, l);
var Ie = (l, a = "wss://socket.polygon.io") => xe(`${a}/options`, l);
var ve = (l, a = "wss://socket.polygon.io") => xe(`${a}/stocks`, l);
var qe = (l, a = "wss://socket.polygon.io", e) => xe(`${a}/futures${e ? `/${e}` : ""}`, l);
var mn = (l, a, e) => ({ crypto: () => ke(l, a), forex: () => Ve(l, a), indices: () => we(l, a), options: () => Ie(l, a), stocks: () => ve(l, a), futures: () => qe(l, a, e) });
var Te = mn;
var bn = (l, a, e) => {
  let i = new Oe({ apiKey: l }), t = "https://api.polygon.io", r = axios_default.create();
  return r.interceptors.response.use((n) => __async(null, null, function* () {
    if (e?.pagination && n?.data?.next_url) {
      let s = yield r.get(`${n.data.next_url}&apiKey=${l}`), { results: o, count: g } = s;
      return __spreadValues(__spreadProps(__spreadValues({}, n.data), { results: [...o, ...n.data?.results] }), n.data?.count && { count: n.data.count + g });
    }
    return n?.data;
  })), new Ce(i, a || t, r);
};
var hn = (l, a, e, i) => ({ rest: bn(l, a, i), websockets: Te(l, e) });
var mi = hn;
export {
  Oe as Configuration,
  Ce as DefaultApi,
  Tt as DefaultApiAxiosParamCreator,
  vn as DefaultApiFactory,
  D as DefaultApiFp,
  Ge as FinancialsPeriodEnum,
  Be as GetBenzingaV1AnalystInsights200ResponseStatusEnum,
  Ue as GetBenzingaV1AnalystInsights400ResponseStatusEnum,
  De as GetBenzingaV1Analysts200ResponseStatusEnum,
  Me as GetBenzingaV1ConsensusRatings200ResponseStatusEnum,
  Qe as GetBenzingaV1Earnings200ResponseStatusEnum,
  He as GetBenzingaV1Firms200ResponseStatusEnum,
  Ee as GetBenzingaV1Guidance200ResponseStatusEnum,
  ze as GetBenzingaV1News200ResponseStatusEnum,
  je as GetBenzingaV1Ratings200ResponseStatusEnum,
  Gt as GetCryptoAggregatesSortEnum,
  Ft as GetCryptoAggregatesTimespanEnum,
  Dt as GetCryptoEMAOrderEnum,
  Ut as GetCryptoEMASeriesTypeEnum,
  Bt as GetCryptoEMATimespanEnum,
  Ht as GetCryptoMACDOrderEnum,
  Qt as GetCryptoMACDSeriesTypeEnum,
  Mt as GetCryptoMACDTimespanEnum,
  jt as GetCryptoRSIOrderEnum,
  zt as GetCryptoRSISeriesTypeEnum,
  Et as GetCryptoRSITimespanEnum,
  Nt as GetCryptoSMAOrderEnum,
  $t as GetCryptoSMASeriesTypeEnum,
  Kt as GetCryptoSMATimespanEnum,
  Lt as GetCryptoSnapshotDirectionDirectionEnum,
  Wt as GetCryptoTradesOrderEnum,
  Yt as GetCryptoTradesSortEnum,
  Xt as GetCurrencyConversionPrecisionEnum,
  Ke as GetFedV1Inflation200ResponseStatusEnum,
  $e as GetFedV1InflationExpectations200ResponseStatusEnum,
  Ne as GetFedV1TreasuryYields200ResponseStatusEnum,
  Zt as GetForexAggregatesSortEnum,
  Jt as GetForexAggregatesTimespanEnum,
  ss as GetForexEMAOrderEnum,
  ts as GetForexEMASeriesTypeEnum,
  es as GetForexEMATimespanEnum,
  is as GetForexMACDOrderEnum,
  ns as GetForexMACDSeriesTypeEnum,
  rs as GetForexMACDTimespanEnum,
  as as GetForexQuotesOrderEnum,
  os as GetForexQuotesSortEnum,
  cs as GetForexRSIOrderEnum,
  us as GetForexRSISeriesTypeEnum,
  gs as GetForexRSITimespanEnum,
  ps as GetForexSMAOrderEnum,
  ds as GetForexSMASeriesTypeEnum,
  ls as GetForexSMATimespanEnum,
  ms as GetForexSnapshotDirectionDirectionEnum,
  fs as GetFuturesAggregatesSortEnum,
  bs as GetFuturesContractsActiveEnum,
  Rs as GetFuturesContractsSortEnum,
  hs as GetFuturesContractsTypeEnum,
  xs as GetFuturesDailySchedulesSortEnum,
  Le as GetFuturesMarketStatuses200ResponseResultsInnerMarketStatusEnum,
  ys as GetFuturesMarketStatusesSortEnum,
  As as GetFuturesProductDetailsTypeEnum,
  We as GetFuturesProductSchedules200ResponseResultsInnerScheduleInnerEventEnum,
  _s as GetFuturesProductSchedulesSortEnum,
  Ye as GetFuturesProducts200ResponseResultsInnerClearingChannelEnum,
  Ss as GetFuturesProductsAssetClassEnum,
  Ps as GetFuturesProductsAssetSubClassEnum,
  Cs as GetFuturesProductsSectorEnum,
  Vs as GetFuturesProductsSortEnum,
  Os as GetFuturesProductsSubSectorEnum,
  ks as GetFuturesProductsTypeEnum,
  ws as GetFuturesQuotesSortEnum,
  Is as GetFuturesTradesSortEnum,
  qs as GetIndicesAggregatesSortEnum,
  vs as GetIndicesAggregatesTimespanEnum,
  Gs as GetIndicesEMAOrderEnum,
  Fs as GetIndicesEMASeriesTypeEnum,
  Ts as GetIndicesEMATimespanEnum,
  Ds as GetIndicesMACDOrderEnum,
  Us as GetIndicesMACDSeriesTypeEnum,
  Bs as GetIndicesMACDTimespanEnum,
  Hs as GetIndicesRSIOrderEnum,
  Qs as GetIndicesRSISeriesTypeEnum,
  Ms as GetIndicesRSITimespanEnum,
  js as GetIndicesSMAOrderEnum,
  zs as GetIndicesSMASeriesTypeEnum,
  Es as GetIndicesSMATimespanEnum,
  Xe as GetIndicesSnapshot200ResponseResultsInnerTimeframeEnum,
  Je as GetIndicesSnapshot200ResponseResultsInnerTypeEnum,
  Ks as GetIndicesSnapshotOrderEnum,
  $s as GetIndicesSnapshotSortEnum,
  Ls as GetOptionsAggregatesSortEnum,
  Ns as GetOptionsAggregatesTimespanEnum,
  Ze as GetOptionsChain200ResponseResultsInnerDetailsContractTypeEnum,
  et as GetOptionsChain200ResponseResultsInnerDetailsExerciseStyleEnum,
  tt as GetOptionsChain200ResponseResultsInnerLastQuoteTimeframeEnum,
  st as GetOptionsChain200ResponseResultsInnerLastTradeTimeframeEnum,
  Ws as GetOptionsChainContractTypeEnum,
  Ys as GetOptionsChainOrderEnum,
  Xs as GetOptionsChainSortEnum,
  er as GetOptionsEMAOrderEnum,
  Zs as GetOptionsEMASeriesTypeEnum,
  Js as GetOptionsEMATimespanEnum,
  rr as GetOptionsMACDOrderEnum,
  sr as GetOptionsMACDSeriesTypeEnum,
  tr as GetOptionsMACDTimespanEnum,
  nr as GetOptionsQuotesOrderEnum,
  ir as GetOptionsQuotesSortEnum,
  gr as GetOptionsRSIOrderEnum,
  or as GetOptionsRSISeriesTypeEnum,
  ar as GetOptionsRSITimespanEnum,
  lr as GetOptionsSMAOrderEnum,
  cr as GetOptionsSMASeriesTypeEnum,
  ur as GetOptionsSMATimespanEnum,
  dr as GetOptionsTradesOrderEnum,
  pr as GetOptionsTradesSortEnum,
  nt as GetSnapshotSummary200ResponseResultsInnerOptionsContractTypeEnum,
  it as GetSnapshotSummary200ResponseResultsInnerOptionsExerciseStyleEnum,
  rt as GetSnapshotSummary200ResponseResultsInnerTypeEnum,
  gt as GetSnapshots200ResponseResultsInnerDetailsContractTypeEnum,
  ut as GetSnapshots200ResponseResultsInnerDetailsExerciseStyleEnum,
  ct as GetSnapshots200ResponseResultsInnerLastQuoteTimeframeEnum,
  lt as GetSnapshots200ResponseResultsInnerLastTradeTimeframeEnum,
  at as GetSnapshots200ResponseResultsInnerTimeframeEnum,
  ot as GetSnapshots200ResponseResultsInnerTypeEnum,
  dt as GetSnapshots200ResponseResultsInnerUnderlyingAssetTimeframeEnum,
  fr as GetSnapshotsOrderEnum,
  br as GetSnapshotsSortEnum,
  mr as GetSnapshotsTypeEnum,
  Rr as GetStocksAggregatesSortEnum,
  hr as GetStocksAggregatesTimespanEnum,
  Ar as GetStocksEMAOrderEnum,
  yr as GetStocksEMASeriesTypeEnum,
  xr as GetStocksEMATimespanEnum,
  Or as GetStocksMACDOrderEnum,
  Cr as GetStocksMACDSeriesTypeEnum,
  _r as GetStocksMACDTimespanEnum,
  Sr as GetStocksQuotesOrderEnum,
  Pr as GetStocksQuotesSortEnum,
  wr as GetStocksRSIOrderEnum,
  Vr as GetStocksRSISeriesTypeEnum,
  kr as GetStocksRSITimespanEnum,
  qr as GetStocksSMAOrderEnum,
  vr as GetStocksSMASeriesTypeEnum,
  Ir as GetStocksSMATimespanEnum,
  Tr as GetStocksSnapshotDirectionDirectionEnum,
  Fr as GetStocksTradesOrderEnum,
  Gr as GetStocksTradesSortEnum,
  pt as GetStocksV1ShortInterest200ResponseStatusEnum,
  mt as GetStocksV1ShortVolume200ResponseStatusEnum,
  ft as GetTicker200ResponseResultsLocaleEnum,
  bt as GetTicker200ResponseResultsMarketEnum,
  ht as GetTmxV1CorporateEvents200ResponseStatusEnum,
  Rt as ListConditions200ResponseResultsInnerAssetClassEnum,
  xt as ListConditions200ResponseResultsInnerDataTypesEnum,
  yt as ListConditions200ResponseResultsInnerTypeEnum,
  Br as ListConditionsAssetClassEnum,
  Ur as ListConditionsDataTypeEnum,
  Mr as ListConditionsOrderEnum,
  Dr as ListConditionsSipEnum,
  Qr as ListConditionsSortEnum,
  At as ListDividends200ResponseResultsInnerDividendTypeEnum,
  Er as ListDividendsDividendTypeEnum,
  Hr as ListDividendsFrequencyEnum,
  zr as ListDividendsOrderEnum,
  jr as ListDividendsSortEnum,
  _t as ListExchanges200ResponseResultsInnerAssetClassEnum,
  Ct as ListExchanges200ResponseResultsInnerLocaleEnum,
  Ot as ListExchanges200ResponseResultsInnerTypeEnum,
  Kr as ListExchangesAssetClassEnum,
  $r as ListExchangesLocaleEnum,
  Lr as ListFinancialsOrderEnum,
  Wr as ListFinancialsSortEnum,
  Nr as ListFinancialsTimeframeEnum,
  St as ListIPOs200ResponseResultsInnerIpoStatusEnum,
  Yr as ListIPOsIpoStatusEnum,
  Xr as ListIPOsOrderEnum,
  Jr as ListIPOsSortEnum,
  Pt as ListNews200ResponseResultsInnerInsightsInnerSentimentEnum,
  Zr as ListNewsOrderEnum,
  en as ListNewsSortEnum,
  kt as ListOptionsContracts200ResponseResultsInnerExerciseStyleEnum,
  tn as ListOptionsContractsContractTypeEnum,
  sn as ListOptionsContractsOrderEnum,
  rn as ListOptionsContractsSortEnum,
  nn as ListStockSplitsOrderEnum,
  an as ListStockSplitsSortEnum,
  Vt as ListTickerTypes200ResponseResultsInnerAssetClassEnum,
  wt as ListTickerTypes200ResponseResultsInnerLocaleEnum,
  on as ListTickerTypesAssetClassEnum,
  gn as ListTickerTypesLocaleEnum,
  It as ListTickers200ResponseResultsInnerLocaleEnum,
  vt as ListTickers200ResponseResultsInnerMarketEnum,
  cn as ListTickersMarketEnum,
  ln as ListTickersOrderEnum,
  dn as ListTickersSortEnum,
  un as ListTickersTypeEnum,
  qt as MapKeyTypeEnum,
  mi as default,
  ke as getCryptoWebsocket,
  Ve as getForexWebsocket,
  qe as getFuturesWebsocket,
  we as getIndicesWebsocket,
  Ie as getOptionsWebsocket,
  ve as getStocksWebsocket,
  hn as polygonClient,
  bn as restClient,
  mn as websocketClient
};
//# sourceMappingURL=@polygon__io_client-js.js.map
