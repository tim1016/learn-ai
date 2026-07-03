const assert = require('node:assert/strict');

const proxyConfig = require('../proxy.conf.js');
const {
  DATA_PLANE_CONTROL_SECRET_HEADER,
  DATA_PLANE_CONTROL_INTENT_HEADER,
  DATA_PLANE_CONTROL_INTENT_VALUE,
  CONTROL_PREFIXES,
  attachDataPlaneSecret,
  configureDataPlaneProxy,
  isControlMutation,
  shouldAttachDataPlaneSecret,
} = proxyConfig.__test;

function request({
  method = 'POST',
  url = '/api/broker/connect',
  intent = DATA_PLANE_CONTROL_INTENT_VALUE,
  origin = 'http://localhost:4200',
  referer = 'http://localhost:4200/broker',
  secFetchSite = 'same-origin',
  callerSecret = null,
} = {}) {
  const headers = {};
  if (intent !== null) headers[DATA_PLANE_CONTROL_INTENT_HEADER.toLowerCase()] = intent;
  if (origin !== null) headers.origin = origin;
  if (referer !== null) headers.referer = referer;
  if (secFetchSite !== null) headers['sec-fetch-site'] = secFetchSite;
  if (callerSecret !== null) headers[DATA_PLANE_CONTROL_SECRET_HEADER.toLowerCase()] = callerSecret;
  return { method, url, headers };
}

function proxyReqRecorder(initialHeaders = {}) {
  const values = new Map(
    Object.entries(initialHeaders).map(([name, value]) => [name.toLowerCase(), value]),
  );
  const headers = {
    get(name) {
      return values.get(name.toLowerCase());
    },
    has(name) {
      return values.has(name.toLowerCase());
    },
  };
  return {
    headers,
    setHeader(name, value) {
      values.set(name.toLowerCase(), value);
    },
    removeHeader(name) {
      values.delete(name.toLowerCase());
    },
  };
}

function proxyEmitterRecorder() {
  const handlers = new Map();
  return {
    on(event, handler) {
      handlers.set(event, handler);
    },
    handler(event) {
      return handlers.get(event);
    },
  };
}

for (const prefix of CONTROL_PREFIXES) {
  const req = request({ url: `${prefix}/__probe` });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.get(DATA_PLANE_CONTROL_SECRET_HEADER), 'local-dev-control-secret');
  assert.equal(isControlMutation(req), true);
}

{
  const req = request({ url: '/api/live-instances/runs/run-abc/start' });
  const proxy = proxyEmitterRecorder();
  const proxyReq = proxyReqRecorder();
  configureDataPlaneProxy(proxy);

  const proxyReqHandler = proxy.handler('proxyReq');
  assert.equal(typeof proxyReqHandler, 'function');
  proxyReqHandler(proxyReq, req);
  assert.equal(proxyReq.headers.get(DATA_PLANE_CONTROL_SECRET_HEADER), 'local-dev-control-secret');
}

{
  const req = request({ intent: null });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(shouldAttachDataPlaneSecret(req), false);
}

{
  const req = request({ origin: 'https://evil.example', secFetchSite: 'cross-site' });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(shouldAttachDataPlaneSecret(req), false);
}

{
  const req = request({ origin: null, referer: null, secFetchSite: null });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(shouldAttachDataPlaneSecret(req), false);
}

{
  const req = request({ origin: null, referer: null, secFetchSite: 'same-origin' });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(shouldAttachDataPlaneSecret(req), false);
}

{
  const req = request({
    callerSecret: 'attacker-supplied-secret',
    intent: null,
  });
  const proxyReq = proxyReqRecorder({
    [DATA_PLANE_CONTROL_SECRET_HEADER.toLowerCase()]: 'attacker-supplied-secret',
  });
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), true);
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(req.headers[DATA_PLANE_CONTROL_SECRET_HEADER.toLowerCase()], 'attacker-supplied-secret');
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER.toLowerCase()), false);
  assert.equal(shouldAttachDataPlaneSecret(req), false);
}

{
  const req = request({ method: 'GET', url: '/api/broker/health' });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(shouldAttachDataPlaneSecret(req), false);
}

{
  const req = request({ method: 'POST', url: '/api/research/strategy-runs' });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(shouldAttachDataPlaneSecret(req), false);
}

{
  const req = request({ method: 'POST', url: '/api/brokerage/connect' });
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
  assert.equal(proxyReq.headers.has(DATA_PLANE_CONTROL_SECRET_HEADER), false);
  assert.equal(isControlMutation(req), false);
}

console.log('proxy control guard ok');
