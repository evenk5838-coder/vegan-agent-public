import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/chat-submission.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { prepareChatAttempt } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
const input = { owner: 'alice', session: 'chat-a', content: '这包能吃吗', attachments: [{ kind: 'ingredient_photo', job_id: 'photo-a' }] };

test('an ambiguous send can replay the same text and uploaded task with the same key', () => {
  const attempt = prepareChatAttempt(null, input, () => 'key-a');
  assert.equal(prepareChatAttempt(attempt, { ...input, attachments: [...input.attachments] }, () => assert.fail('must reuse key')), attempt);
});

test('changing the image, text, conversation or account starts a distinct submission', () => {
  const attempt = prepareChatAttempt(null, input, () => 'key-a');
  for (const patch of [{ owner: 'bob' }, { session: 'chat-b' }, { content: '换个问题' }, { attachments: [{ kind: 'ingredient_photo', job_id: 'photo-b' }] }, { attachments: [] }]) {
    assert.equal(prepareChatAttempt(attempt, { ...input, ...patch }, () => 'key-b').key, 'key-b');
  }
});

test('an image-only question has stable visible content and sends no upload metadata', () => {
  const attempt = prepareChatAttempt(null, { ...input, content: ' ', attachments: [{ ...input.attachments[0], save_original: true }] }, () => 'key-a');
  assert.match(attempt.content, /配料表/);
  assert.deepEqual(attempt.attachments, [{ kind: 'ingredient_photo', job_id: 'photo-a' }]);
});

test('empty text without an image, oversized text and missing task IDs cannot submit', () => {
  assert.throws(() => prepareChatAttempt(null, { ...input, content: '', attachments: [] }, () => 'key'), /填写/);
  assert.throws(() => prepareChatAttempt(null, { ...input, content: '字'.repeat(4001) }, () => 'key'), /4000/);
  assert.throws(() => prepareChatAttempt(null, { ...input, attachments: [{ kind: 'ingredient_photo', job_id: '' }] }, () => 'key'), /图片任务/);
});
