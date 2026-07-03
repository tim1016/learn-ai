const assert = require('node:assert/strict');

const proxyConfig = require('../proxy.conf.js');
const {
  DATA_PLANE_CONTROL_SECRET_HEADER,
  DATA_PLANE_CONTROL_INTENT_HEADER,
  DATA_PLANE_CONTROL_INTENT_VALUE,
  attachDataPlaneSecret,
  shouldAttachDataPlaneSecret,
} = proxyConfig.__test;

function request({
  method = 'POST',
  url = '/api/broker/connect',
  intent = DATA_PLANE_CONTROL_INTENT_VALUE,
  origin = 'http://localhost:4200',
  referer = 'http://localhost:4200/broker',
  secFetchSite = 'same-origin',
} = {}) {
  const headers = {};
  if (intent !== null) headers[DATA_PLANE_CONTROL_INTENT_HEADER.toLowerCase()] = intent;
  if (origin !== null) headers.origin = origin;
  if (referer !== null) headers.referer = referer;
  if (secFetchSite !== null) headers['sec-fetch-site'] = secFetchSite;
  return { method, url, headers };
}

function proxyReqRecorder() {
  const headers = new Map();
  return {
    headers,
    setHeader(name, value) {
      headers.set(name, value);
    },
    removeHeader(name) {
      headers.delete(name);
    },
  };
}

{
  const req = request();
  const proxyReq = proxyReqRecorder();
  attachDataPlaneSecret(proxyReq, req);
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

console.log('proxy control guard ok');
