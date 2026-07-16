const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const specRoot = path.join(
  __dirname,
  '..',
  'src',
  'app',
  'components',
  'broker',
  'bot-control',
);
const harnessPath = path.join(specRoot, 'bot-control-page.testing.ts');

const configureAllowMarker = 'bot-control-allow-configure-live-runs';
const mutationAllowMarker = 'bot-control-allow-live-runs-mutation-mock';
const pageHarnessTokens = [
  'makeFailClosedLiveRuns',
  'setupBotControlPage',
  'setupBotControlSidebarHost',
];
const harnessObjectTokens = [
  'setupBotControlPage',
  'setupBotControlSidebarHost',
];
const mutationMethods = [
  'renewControlPlaneLease',
  'runRollCall',
  'startHostRunner',
  'endDayNow',
  'emergencyFlattenAccount',
  'setBotLifecycleRoster',
  'retireAndReplace',
  'setInstanceDesiredState',
  'flattenAndPause',
  'issueInstanceCommand',
  'reconcileInstance',
  'recordCrashRecoveryOverride',
  'deleteBot',
];
const mutationMethodSet = new Set(mutationMethods);
const mutationHelperNames = [
  'allowRenewControlPlaneLeaseCall',
  'allowRunRollCallCall',
  'allowStartHostRunnerCall',
  'allowEndDayNowCall',
  'allowEmergencyFlattenAccountCall',
  'allowBotLifecycleMutationCall',
  'allowSetDesiredStateCall',
  'rejectSetDesiredStateCall',
  'allowFlattenAndPauseCall',
  'allowIssueInstanceCommandCall',
  'allowReconcileInstanceCall',
  'allowCrashRecoveryOverrideCall',
  'allowDeleteBotCall',
  'rejectReconcileInstanceCall',
];
const mutationMockNames = new Set([
  'mockImplementation',
  'mockImplementationOnce',
  'mockRejectedValue',
  'mockRejectedValueOnce',
  'mockResolvedValue',
  'mockResolvedValueOnce',
  'mockReturnValue',
  'mockReturnValueOnce',
]);
const mutationHelperPattern = new RegExp(`\\b(?:${mutationHelperNames.join('|')})\\b`);
const offenders = [];

function specFilesUnder(directory) {
  const entries = fs.readdirSync(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const childPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...specFilesUnder(childPath));
    } else if (entry.isFile() && entry.name.endsWith('.spec.ts')) {
      files.push(childPath);
    }
  }
  return files.sort();
}

function markerAttached(lines, index, marker) {
  return lines.slice(Math.max(0, index - 2), index + 1).join('\n').includes(marker);
}

function usesBotControlPageHarness(content) {
  return pageHarnessTokens.some((token) => new RegExp(`\\b${token}\\b`).test(content));
}

function memberName(node) {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isElementAccessExpression(node) && ts.isStringLiteralLike(node.argumentExpression)) {
    return node.argumentExpression.text;
  }
  return null;
}

function bindingPropertyName(element) {
  const propertyName = element.propertyName ?? element.name;
  if (ts.isIdentifier(propertyName) || ts.isStringLiteralLike(propertyName)) {
    return propertyName.text;
  }
  return null;
}

function createScope(parent = null) {
  return {
    parent,
    symbols: new Map(),
  };
}

function declareName(scope, name, kind) {
  scope.symbols.set(name, kind);
}

function assignName(scope, name, kind) {
  for (let current = scope; current !== null; current = current.parent) {
    if (current.symbols.has(name)) {
      current.symbols.set(name, kind);
      return;
    }
  }
  scope.symbols.set(name, kind);
}

function lookupName(scope, name) {
  for (let current = scope; current !== null; current = current.parent) {
    if (current.symbols.has(name)) return current.symbols.get(name);
  }
  return null;
}

function declareBindingNames(scope, bindingName, kind = 'other') {
  if (ts.isIdentifier(bindingName)) {
    declareName(scope, bindingName.text, kind);
    return;
  }
  if (!ts.isObjectBindingPattern(bindingName)) return;
  for (const element of bindingName.elements) {
    declareBindingNames(scope, element.name, kind);
  }
}

function unwrapExpression(node) {
  let current = node;
  while (ts.isAwaitExpression(current) || ts.isParenthesizedExpression(current)) {
    current = current.expression;
  }
  return current;
}

function isCallToToken(node, tokens) {
  const current = unwrapExpression(node);
  if (!ts.isCallExpression(current)) return false;
  const expression = unwrapExpression(current.expression);
  return ts.isIdentifier(expression) && tokens.includes(expression.text);
}

function containsGuardedMutationExpression(node, scope) {
  let found = false;
  function visit(child) {
    if (found) return;
    if (ts.isFunctionLike(child)) return;
    if (ts.isIdentifier(child) && lookupName(scope, child.text) === 'mutation') {
      found = true;
      return;
    }
    const name = memberName(child);
    const receiver = ts.isPropertyAccessExpression(child) || ts.isElementAccessExpression(child)
      ? child.expression
      : null;
    if (name && receiver && mutationMethodSet.has(name) && expressionNamesLiveRunsObject(receiver, scope)) {
      found = true;
      return;
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return found;
}

function addHarnessObjectBindingAliases(bindingName, scope) {
  if (!ts.isObjectBindingPattern(bindingName)) return;
  for (const element of bindingName.elements) {
    const propertyName = bindingPropertyName(element);
    if (propertyName !== 'liveRuns') continue;
    if (ts.isIdentifier(element.name)) {
      declareName(scope, element.name.text, 'liveRuns');
    } else {
      addBindingAliases(element.name, scope);
    }
  }
}

function addBindingAliases(bindingName, scope) {
  if (ts.isIdentifier(bindingName)) {
    declareName(scope, bindingName.text, 'mutation');
    return;
  }
  if (!ts.isObjectBindingPattern(bindingName)) return;
  for (const element of bindingName.elements) {
    const propertyName = bindingPropertyName(element);
    if (!propertyName || !mutationMethodSet.has(propertyName)) continue;
    addBindingAliases(element.name, scope);
  }
}

function expressionNamesHarnessObject(node, scope) {
  const current = unwrapExpression(node);
  if (ts.isIdentifier(current) && lookupName(scope, current.text) === 'harness') return true;
  return isCallToToken(current, harnessObjectTokens);
}

function expressionNamesLiveRunsObject(node, scope) {
  const current = unwrapExpression(node);
  if (ts.isIdentifier(current) && lookupName(scope, current.text) === 'liveRuns') return true;
  if (isCallToToken(current, ['makeFailClosedLiveRuns'])) return true;
  if (memberName(current) === 'liveRuns') {
    const receiver = ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)
      ? current.expression
      : null;
    return receiver ? expressionNamesHarnessObject(receiver, scope) : false;
  }
  return false;
}

function mutationMockOffenders(content, specPath) {
  const source = ts.createSourceFile(specPath, content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const lines = content.split(/\r?\n/);
  const matches = [];
  let scope = createScope();

  function withChildScope(callback) {
    const parent = scope;
    scope = createScope(parent);
    try {
      callback();
    } finally {
      scope = parent;
    }
  }

  function applyVariableAlias(node) {
    if (!node.initializer) {
      declareBindingNames(scope, node.name);
      return;
    }

    visit(node.initializer);
    declareBindingNames(scope, node.name);
    if (ts.isIdentifier(node.name)) {
      if (expressionNamesHarnessObject(node.initializer, scope)) {
        declareName(scope, node.name.text, 'harness');
      } else if (expressionNamesLiveRunsObject(node.initializer, scope)) {
        declareName(scope, node.name.text, 'liveRuns');
      } else if (containsGuardedMutationExpression(node.initializer, scope)) {
        declareName(scope, node.name.text, 'mutation');
      }
      return;
    }

    if (expressionNamesHarnessObject(node.initializer, scope)) {
      addHarnessObjectBindingAliases(node.name, scope);
    } else if (
      expressionNamesLiveRunsObject(node.initializer, scope)
      || containsGuardedMutationExpression(node.initializer, scope)
    ) {
      addBindingAliases(node.name, scope);
    }
  }

  function applyAssignmentAlias(node) {
    visit(node.right);
    if (!ts.isIdentifier(node.left)) return;
    if (expressionNamesHarnessObject(node.right, scope)) {
      assignName(scope, node.left.text, 'harness');
    } else if (expressionNamesLiveRunsObject(node.right, scope)) {
      assignName(scope, node.left.text, 'liveRuns');
    } else if (containsGuardedMutationExpression(node.right, scope)) {
      assignName(scope, node.left.text, 'mutation');
    } else {
      assignName(scope, node.left.text, 'other');
    }
  }

  function visit(node) {
    if (ts.isSourceFile(node)) {
      withChildScope(() => {
        for (const statement of node.statements) visit(statement);
      });
      return;
    }
    if (ts.isBlock(node)) {
      withChildScope(() => {
        for (const statement of node.statements) visit(statement);
      });
      return;
    }
    if (ts.isFunctionLike(node)) {
      withChildScope(() => {
        for (const parameter of node.parameters) declareBindingNames(scope, parameter.name);
        if (node.body) visit(node.body);
      });
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      applyVariableAlias(node);
      return;
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      applyAssignmentAlias(node);
      return;
    }
    if (ts.isCallExpression(node)) {
      const name = memberName(node.expression);
      const receiver = ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression)
        ? node.expression.expression
        : null;
      if (name && receiver && mutationMockNames.has(name) && containsGuardedMutationExpression(receiver, scope)) {
        const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line;
        matches.push({ line, text: lines[line]?.trim() ?? '' });
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(source);
  return matches;
}

function assertMutationMockDetectorCatchesBypasses() {
  const withLiveRuns = (statement) => `const { liveRuns } = await setupBotControlPage();\n${statement}`;
  const bypasses = [
    withLiveRuns('liveRuns.setInstanceDesiredState.mockResolvedValue(response);'),
    withLiveRuns('liveRuns.setInstanceDesiredState.mockResolvedValueOnce(response);'),
    withLiveRuns('vi.mocked(liveRuns.setInstanceDesiredState).mockResolvedValue(response);'),
    withLiveRuns('liveRuns.setInstanceDesiredState\n  .mockResolvedValue(response);'),
    withLiveRuns('liveRuns.setInstanceDesiredState.mockReset().mockResolvedValue(response);'),
    withLiveRuns("liveRuns['setInstanceDesiredState'].mockResolvedValue(response);"),
    withLiveRuns('const setDesiredState = liveRuns.setInstanceDesiredState;\nsetDesiredState.mockResolvedValue(response);'),
    withLiveRuns('const { setInstanceDesiredState } = liveRuns;\nsetInstanceDesiredState.mockResolvedValue(response);'),
    'const { liveRuns: runs } = await setupBotControlPage();\nconst { setInstanceDesiredState } = runs;\nsetInstanceDesiredState.mockResolvedValue(response);',
    'const harness = await setupBotControlPage();\nconst runs = harness.liveRuns;\nconst { setInstanceDesiredState } = runs;\nsetInstanceDesiredState.mockResolvedValue(response);',
    'let runs;\nconst harness = await setupBotControlPage();\nruns = harness.liveRuns;\nconst { setInstanceDesiredState } = runs;\nsetInstanceDesiredState.mockResolvedValue(response);',
    withLiveRuns('let setDesiredState;\nsetDesiredState = liveRuns.setInstanceDesiredState;\nsetDesiredState.mockResolvedValue(response);'),
    'const { liveRuns: { setInstanceDesiredState } } = await setupBotControlPage();\nsetInstanceDesiredState.mockResolvedValue(response);',
    'const { liveRuns: { setInstanceDesiredState: setDesiredState } } = await setupBotControlPage();\nsetDesiredState.mockResolvedValue(response);',
    'const harness = await setupBotControlPage();\nconst { liveRuns: { setInstanceDesiredState } } = harness;\nsetInstanceDesiredState.mockResolvedValue(response);',
  ];
  for (const bypass of bypasses) {
    assert.notEqual(
      mutationMockOffenders(bypass, '<self-test>').length,
      0,
      `Bot Control mutation guard failed to catch bypass probe:\n${bypass}`,
    );
  }
  assert.equal(
    mutationMockOffenders(withLiveRuns("liveRuns.issueInstanceCommand('sid-x', { verb: 'MARK_POISONED' });"), '<self-test>').length,
    0,
    'Bot Control mutation guard should not flag ordinary mutation assertions/calls.',
  );
  assert.equal(
    mutationMockOffenders(
      [
        'const { liveRuns: { setInstanceDesiredState } } = await setupBotControlPage();',
        "it('uses a local helper', () => {",
        '  const setInstanceDesiredState = vi.fn();',
        '  setInstanceDesiredState.mockResolvedValue(response);',
        '});',
      ].join('\n'),
      '<self-test>',
    ).length,
    0,
    'Bot Control mutation guard should not flag a shadowed local mutation-name helper.',
  );
}

assertMutationMockDetectorCatchesBypasses();

function assertMutationDenyListMatchesHarnessDefaults() {
  const harnessContent = fs.readFileSync(harnessPath, 'utf8');
  const harnessDefaults = [...harnessContent.matchAll(
    /liveRuns\.([A-Za-z0-9_]+)\.mockRejectedValue\(unexpectedMutation\('\1'\)\)/g,
  )].map((match) => match[1]).sort();
  assert.notEqual(harnessDefaults.length, 0, 'No fail-closed Bot Control harness mutation defaults were found.');
  assert.deepEqual(
    [...mutationMethodSet].sort(),
    harnessDefaults,
    'Bot Control mutation guard deny-list must match the harness fail-closed mutation defaults.',
  );
}

assertMutationDenyListMatchesHarnessDefaults();

for (const specPath of specFilesUnder(specRoot)) {
  const content = fs.readFileSync(specPath, 'utf8');
  const lines = content.split(/\r?\n/);
  const isPageHarnessConsumer = usesBotControlPageHarness(content);
  const mutationOffenders = isPageHarnessConsumer ? mutationMockOffenders(content, specPath) : [];

  for (const [index, line] of lines.entries()) {
    if (/\bconfigureLiveRuns\b/.test(line) && !markerAttached(lines, index, configureAllowMarker)) {
      offenders.push(`${specPath}:${index + 1}: ${line.trim()}`);
    }
    if (!isPageHarnessConsumer) continue;
    if (mutationHelperPattern.test(line) && !markerAttached(lines, index, mutationAllowMarker)) {
      offenders.push(`${specPath}:${index + 1}: ${line.trim()}`);
    }
  }
  for (const { line, text } of mutationOffenders) {
    if (markerAttached(lines, line, mutationAllowMarker)) continue;
    offenders.push(`${specPath}:${line + 1}: ${text}`);
  }
}

assert.equal(
  offenders.length,
  0,
  [
    'Bot Control specs must use typed read/mutation harness options for ordinary LiveRunsService setup.',
    `Add ${configureAllowMarker} immediately above an intentional bespoke configureLiveRuns escape hatch.`,
    `Add ${mutationAllowMarker} immediately above an intentional bespoke mutation mock override.`,
    ...offenders,
  ].join('\n'),
);
