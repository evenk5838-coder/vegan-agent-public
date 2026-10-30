import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/food-reminder.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { reminderUpdate } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
const now = Date.parse('2026-10-27T12:00:00+08:00');
const food = { id: 'tofu-1', name: '豆腐', storage: 'frozen', started_at: '2026-10-25T12:00:00+08:00',
  expires_at: '2026-10-30T12:00:00+08:00', deadline_source: 'user', status: 'active',
  storage_history: [{ storage: 'refrigerated' }], remind_at: null };

test('reminder edit retains the selected food, original dates, storage and history', () => {
  const updated = reminderUpdate(food, '2026-10-28T12:00:00+08:00', now);
  assert.equal(updated.id, 'tofu-1');
  assert.equal(updated.started_at, food.started_at);
  assert.equal(updated.expires_at, food.expires_at);
  assert.equal(updated.deadline_source, 'user');
  assert.equal(updated.storage, 'frozen');
  assert.deepEqual(updated.storage_history, food.storage_history);
  assert.equal(updated.remind_at, '2026-10-28T04:00:00.000Z');
  assert.equal(food.remind_at, null);
});

test('reminders are allowed for foods without an established expiry, without inventing one', () => {
  const result = reminderUpdate({ ...food, expires_at: null, deadline_source: 'unknown' }, '2026-10-28T12:00:00+08:00', now);
  assert.equal(result.expires_at, null);
  assert.equal(result.deadline_source, 'unknown');
  assert.equal(result.remind_at, '2026-10-28T04:00:00.000Z');
});
test('a name-only food can set a reminder without inventing any storage or source date', () => {
  const partial = { ...food, storage: 'unknown', condition: 'unknown', date_type: 'unknown', started_at: null, expires_at: null, deadline_source: 'unknown' };
  const result = reminderUpdate(partial, '2026-10-28T12:00:00+08:00', now);
  assert.equal(result.storage, 'unknown');
  assert.equal(result.started_at, null);
  assert.equal(result.expires_at, null);
  assert.equal(result.date_type, 'unknown');
});

test('clearing a reminder explicitly sends null without deleting the food', () => {
  const result = reminderUpdate({ ...food, remind_at: '2026-10-28T04:00:00Z' }, '', now);
  assert.equal(result.remind_at, null);
  assert.equal(result.status, 'active');
});

test('invalid, elapsed or post-expiry reminder dates cannot be submitted', () => {
  assert.throws(() => reminderUpdate(food, 'invalid', now), /有效/);
  assert.throws(() => reminderUpdate(food, '2026-10-26T12:00:00+08:00', now), /之后/);
  assert.throws(() => reminderUpdate(food, '2026-11-01T12:00:00+08:00', now), /不能晚于/);
});
