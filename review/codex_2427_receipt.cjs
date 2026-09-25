/* Offline characterization of the actual action method, without Angular boot,
 * HTTP, credentials, or changing source. Pass an installed TypeScript module
 * path; it is used read-only to extract and transpile the production methods. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require(process.argv[2]);
const root = path.resolve(__dirname, '..');
const filename = path.join(root, 'Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts');
const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true);
const declaration = source.statements.find(node => ts.isClassDeclaration(node) && node.name.text === 'BotPanelShellComponent');
assert.ok(declaration);
const methodNames = ['onActionRequested', 'successReceipt'];
const methods = methodNames.map(name => {
  const method = declaration.members.find(member => member.name?.getText(source) === name);
  assert.ok(method, `Missing actual method ${name}`);
  return method.getText(source);
});
const compiled = ts.transpileModule(`class ReviewShell {${methods.join('\n')}}; globalThis.ReviewShell = ReviewShell;`, {
  compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}
}).outputText;
const context = vm.createContext({
  laneFenceVerdict: () => ({ok: true}),
  fencedTarget: target => Object.freeze({...target}),
  actionOutcomeToast: (outcome, message) => ({outcome, message}),
});
vm.runInContext(compiled, context);
function signal(initial) { let value = initial; const read = () => value; read.set = next => {value = next;}; return read; }
async function characterize(move) {
  let finish;
  const result = new Promise(resolve => {finish = resolve;});
  let visible = {clerkId: 'lane-a', accountId: 'account-a', sid: 'bot-a'};
  const sent = [];
  const toasts = [];
  const shell = new context.ReviewShell();
  Object.assign(shell, {
    actionPending: signal(false), actionReceipt: signal(null), openFence: () => ({}),
    fleetDirectory: {lane: () => ({})}, broker: () => 'alpaca', clerkId: () => visible.clerkId,
    target: () => visible, sid: () => visible.sid, commandTarget: target => target,
    panelSvc: {runBotAction: async (target, sid) => {sent.push({target, sid}); return result;}},
    messageService: {add: toast => toasts.push(toast)}, liveStore: {refresh: async () => {}},
  });
  const pending = shell.onActionRequested({action: {action_id: 'stop', label: 'Stop'}, reason: 'synthetic'});
  if (move) visible = {clerkId: 'lane-b', accountId: 'account-b', sid: 'bot-b'};
  finish({action_id: 'stop', receipt_id: 'receipt-a', recorded_at_ms: 1, message: 'Bot stopped.'});
  await pending;
  assert.equal(sent.length, 1);
  assert.equal(sent[0].target.accountId, 'account-a');
  assert.equal(sent[0].sid, 'bot-a');
  assert.equal(shell.actionReceipt().receiptId, 'receipt-a');
  assert.equal(shell.actionReceipt().message, 'Bot stopped.');
  assert.equal(toasts.length, 1);
  assert.equal(shell.actionReceipt().accountId, undefined);
  return {visibleAccount: visible.accountId, submittedAccount: sent[0].target.accountId, displayedReceipt: shell.actionReceipt().receiptId};
}
(async () => {
  const unchanged = await characterize(false);
  const moved = await characterize(true);
  assert.equal(unchanged.visibleAccount, unchanged.submittedAccount);
  assert.notEqual(moved.visibleAccount, moved.submittedAccount);
  process.stdout.write(JSON.stringify({unchanged, moved}) + '\n');
})().catch(error => {process.stderr.write(String(error) + '\n'); process.exitCode = 1;});
