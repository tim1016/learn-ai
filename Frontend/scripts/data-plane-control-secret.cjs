const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { parseEnv } = require('node:util');

const DATA_PLANE_CONTROL_SECRET_KEY = 'DATA_PLANE_CONTROL_SECRET';
const RETIRED_DATA_PLANE_CONTROL_SECRET = 'local-dev-control-secret';
const DEFAULT_ENV_FILE = path.resolve(__dirname, '../../.env');
const ASSIGNMENT_PATTERN = /^\s*(?:export\s+)?DATA_PLANE_CONTROL_SECRET\s*=[^\r\n]*$/gm;

function validatedSecret(value) {
  const secret = value?.trim();
  if (!secret || secret === RETIRED_DATA_PLANE_CONTROL_SECRET) {
    throw new Error(
      'DATA_PLANE_CONTROL_SECRET must be configured with a distinct value before the frontend proxy starts.',
    );
  }
  return secret;
}

function resolveDataPlaneControlSecret(environment = process.env, envFile = DEFAULT_ENV_FILE) {
  if (Object.hasOwn(environment, DATA_PLANE_CONTROL_SECRET_KEY)) {
    return validatedSecret(environment[DATA_PLANE_CONTROL_SECRET_KEY]);
  }

  let fileEnvironment = {};
  try {
    fileEnvironment = parseEnv(fs.readFileSync(envFile, 'utf8'));
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  return validatedSecret(fileEnvironment[DATA_PLANE_CONTROL_SECRET_KEY]);
}

function ensureControlSecretFile(envFile) {
  const environmentPath = path.resolve(envFile);
  const original = fs.readFileSync(environmentPath, 'utf8');
  const assignments = [...original.matchAll(ASSIGNMENT_PATTERN)];
  if (assignments.length > 1) {
    throw new Error(`${DATA_PLANE_CONTROL_SECRET_KEY} must appear exactly once in ${environmentPath}.`);
  }

  const parsed = parseEnv(original);
  const current = parsed[DATA_PLANE_CONTROL_SECRET_KEY]?.trim();
  if (current && current !== RETIRED_DATA_PLANE_CONTROL_SECRET) return 'preserved';

  const secret = crypto.randomBytes(32).toString('base64url');
  const assignment = `${DATA_PLANE_CONTROL_SECRET_KEY}=${secret}`;
  const updated = assignments.length === 1
    ? original.replace(ASSIGNMENT_PATTERN, assignment)
    : `${original}${original.endsWith('\n') || !original ? '' : '\n'}${assignment}\n`;
  const mode = fs.statSync(environmentPath).mode & 0o777;
  const candidate = path.join(
    path.dirname(environmentPath),
    `.${path.basename(environmentPath)}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`,
  );

  let descriptor;
  try {
    descriptor = fs.openSync(candidate, 'wx', mode);
    fs.fchmodSync(descriptor, mode);
    fs.writeFileSync(descriptor, updated, 'utf8');
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(candidate, environmentPath);
    const directory = fs.openSync(path.dirname(environmentPath), 'r');
    try {
      fs.fsyncSync(directory);
    } finally {
      fs.closeSync(directory);
    }
  } catch (error) {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    try {
      fs.unlinkSync(candidate);
    } catch (cleanupError) {
      if (cleanupError?.code !== 'ENOENT') throw cleanupError;
    }
    throw error;
  }

  return current === RETIRED_DATA_PLANE_CONTROL_SECRET ? 'rotated' : 'generated';
}

if (require.main === module) {
  const envFile = process.argv[2];
  if (!envFile) throw new Error('Usage: node data-plane-control-secret.cjs <env-file>');
  const outcome = ensureControlSecretFile(envFile);
  const label = outcome === 'preserved' ? 'already configured' : `${outcome} securely`;
  process.stdout.write(`==> DATA_PLANE_CONTROL_SECRET ${label} in ${envFile}\n`);
}

module.exports = {
  DATA_PLANE_CONTROL_SECRET_KEY,
  RETIRED_DATA_PLANE_CONTROL_SECRET,
  ensureControlSecretFile,
  resolveDataPlaneControlSecret,
};
