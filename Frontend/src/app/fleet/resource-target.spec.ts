import { describe, expect, it } from 'vitest';

import {
  commandBodyOf,
  commandContextOf,
  laneKey,
  resourceTarget,
  sameResourceTarget,
  withCommand,
  withEntity,
} from './resource-target';

describe('resource targets', () => {
  it('freezes complete routing provenance and carries it through a command envelope', () => {
    const target = resourceTarget('alpaca', 'clerk-1', {
      accountId: 'account-1',
      entityId: 'bot-1',
      capability: 'bot_action',
      idempotencyKey: 'command-1',
      bindingGeneration: 7,
      routingEpoch: 12,
    });

    expect(Object.isFrozen(target)).toBe(true);
    expect(commandContextOf(target)).toEqual({
      capability: 'bot_action',
      idempotency_key: 'command-1',
      expected_effective_binding_generation: 7,
      target: { account_id: 'account-1', entity_id: 'bot-1' },
    });
    expect(commandBodyOf(target, { reason: 'operator' })).toMatchObject({
      command_context: { capability: 'bot_action' },
    });
    expect(laneKey('alpaca', 'clerk-1', 12, 7, 'account-1')).toContain('12::7::account-1');
    expect(withEntity(target, 'bot-2')).toMatchObject({
      entityId: 'bot-2',
      routingEpoch: 12,
      idempotencyKey: 'command-1',
    });
    expect(withCommand(target, 'deploy', 'command-2')).toMatchObject({
      broker: 'alpaca',
      clerkId: 'clerk-1',
      accountId: 'account-1',
      entityId: 'bot-1',
      capability: 'deploy',
      idempotencyKey: 'command-2',
      bindingGeneration: 7,
      routingEpoch: 12,
    });
  });

  it('rejects incomplete outbound command and account URL targets', () => {
    const lane = resourceTarget('alpaca', 'clerk-1');

    expect(() => commandContextOf(lane)).toThrow(/frozen capability/i);
    expect(() => resourceTarget('alpaca', 'clerk-1', { accountId: '' })).toThrow(/account ID/i);
  });

  it('treats an identical address re-derived as a new object as the same target', () => {
    const frozen = { accountId: 'PA1', bindingGeneration: 3, routingEpoch: 4 };
    const rendered = resourceTarget('alpaca', 'clerk-1', frozen);

    expect(sameResourceTarget(rendered, resourceTarget('alpaca', 'clerk-1', frozen))).toBe(true);
    expect(sameResourceTarget(null, undefined)).toBe(false);
    expect(sameResourceTarget(null, null)).toBe(true);
    for (const changed of [
      resourceTarget('alpaca', 'clerk-2', frozen),
      resourceTarget('alpaca', 'clerk-1', { ...frozen, accountId: 'PA2' }),
      resourceTarget('alpaca', 'clerk-1', { ...frozen, bindingGeneration: 4 }),
      resourceTarget('alpaca', 'clerk-1', { ...frozen, routingEpoch: 5 }),
      withEntity(rendered, 'bot-1'),
      withCommand(rendered, 'deploy', 'key-1'),
    ]) {
      expect(sameResourceTarget(rendered, changed)).toBe(false);
    }
  });
});
