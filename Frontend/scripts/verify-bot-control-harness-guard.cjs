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
  'startHostRunner',
  'setInstanceDesiredState',
  'flattenAndPause',
  'issueInstanceCommand',
  'reconcileInstance',
];
const mutationMethodSet = new Set(mutationMethods);
const mutationHelperNames = [
  'allowRenewControlPlaneLeaseCall',
  'allowStartHostRunnerCall',
  'allowSetDesiredStateCall',
  'rejectSetDesiredStateCall',
  'allowFlattenAndPauseCall',
  'allowIssueInstanceCommandCall',
  'allowReconcileInstanceCall',
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

function containsGuardedMutationExpression(node, aliases, source) {
  let found = false;
  function visit(child) {
    if (found) return;
    if (ts.isIdentifier(child) && aliases.has(child.text)) {
      found = true;
      return;
    }
    const name = memberName(child);
    if (name && mutationMethodSet.has(name)) {
      found = true;
      return;
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return found;
}

function addHarnessObjectBindingAliases(bindingName, liveRunsObjectAliases, mutationAliases) {
  if (!ts.isObjectBindingPattern(bindingName)) return;
  for (const element of bindingName.elements) {
    const propertyName = bindingPropertyName(element);
    if (propertyName !== 'liveRuns') continue;
    if (ts.isIdentifier(element.name)) {
      liveRunsObjectAliases.add(element.name.text);
    } else {
      addBindingAliases(element.name, mutationAliases);
    }
  }
}

function addBindingAliases(bindingName, aliases) {
  if (ts.isIdentifier(bindingName)) {
    aliases.add(bindingName.text);
    return;
  }
  if (!ts.isObjectBindingPattern(bindingName)) return;
  for (const element of bindingName.elements) {
    const propertyName = bindingPropertyName(element);
    if (!propertyName || !mutationMethodSet.has(propertyName)) continue;
    addBindingAliases(element.name, aliases);
  }
}

function expressionNamesHarnessObject(node, harnessObjectAliases, source) {
  if (ts.isIdentifier(node) && harnessObjectAliases.has(node.text)) return true;
  const text = node.getText(source);
  return harnessObjectTokens.some((token) => new RegExp(`\\b${token}\\s*\\(`).test(text));
}

function expressionNamesLiveRunsObject(node, liveRunsObjectAliases, harnessObjectAliases, source) {
  if (ts.isIdentifier(node) && liveRunsObjectAliases.has(node.text)) return true;
  if (/\bmakeFailClosedLiveRuns\s*\(/.test(node.getText(source))) return true;
  if (memberName(node) === 'liveRuns') {
    const receiver = ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)
      ? node.expression
      : null;
    return receiver ? expressionNamesHarnessObject(receiver, harnessObjectAliases, source) : false;
  }
  return false;
}

function gatherMutationAliases(source) {
  const aliases = new Set();
  const liveRunsObjectAliases = new Set(['liveRuns']);
  const harnessObjectAliases = new Set();
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.initializer) {
      if (ts.isIdentifier(node.name) && expressionNamesHarnessObject(node.initializer, harnessObjectAliases, source)) {
        harnessObjectAliases.add(node.name.text);
      }
      if (ts.isIdentifier(node.name) && expressionNamesLiveRunsObject(
        node.initializer,
        liveRunsObjectAliases,
        harnessObjectAliases,
        source,
      )) {
        liveRunsObjectAliases.add(node.name.text);
      }
      if (ts.isObjectBindingPattern(node.name) && expressionNamesHarnessObject(
        node.initializer,
        harnessObjectAliases,
        source,
      )) {
        addHarnessObjectBindingAliases(node.name, liveRunsObjectAliases, aliases);
      }
      if (containsGuardedMutationExpression(node.initializer, aliases, source)) {
        addBindingAliases(node.name, aliases);
      } else if (ts.isObjectBindingPattern(node.name) && expressionNamesLiveRunsObject(
        node.initializer,
        liveRunsObjectAliases,
        harnessObjectAliases,
        source,
      )) {
        addBindingAliases(node.name, aliases);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  return aliases;
}

function mutationMockOffenders(content, specPath) {
  const source = ts.createSourceFile(specPath, content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const aliases = gatherMutationAliases(source);
  const lines = content.split(/\r?\n/);
  const matches = [];

  function visit(node) {
    if (ts.isCallExpression(node)) {
      const name = memberName(node.expression);
      const receiver = ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression)
        ? node.expression.expression
        : null;
      if (name && receiver && mutationMockNames.has(name) && containsGuardedMutationExpression(receiver, aliases, source)) {
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
  const bypasses = [
    'liveRuns.setInstanceDesiredState.mockResolvedValue(response);',
    'liveRuns.setInstanceDesiredState.mockResolvedValueOnce(response);',
    'vi.mocked(liveRuns.setInstanceDesiredState).mockResolvedValue(response);',
    'liveRuns.setInstanceDesiredState\n  .mockResolvedValue(response);',
    'liveRuns.setInstanceDesiredState.mockReset().mockResolvedValue(response);',
    "liveRuns['setInstanceDesiredState'].mockResolvedValue(response);",
    'const setDesiredState = liveRuns.setInstanceDesiredState;\nsetDesiredState.mockResolvedValue(response);',
    'const { setInstanceDesiredState } = liveRuns;\nsetInstanceDesiredState.mockResolvedValue(response);',
    'const { liveRuns: runs } = await setupBotControlPage();\nconst { setInstanceDesiredState } = runs;\nsetInstanceDesiredState.mockResolvedValue(response);',
    'const harness = await setupBotControlPage();\nconst runs = harness.liveRuns;\nconst { setInstanceDesiredState } = runs;\nsetInstanceDesiredState.mockResolvedValue(response);',
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
    mutationMockOffenders("liveRuns.issueInstanceCommand('sid-x', { verb: 'MARK_POISONED' });", '<self-test>').length,
    0,
    'Bot Control mutation guard should not flag ordinary mutation assertions/calls.',
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
