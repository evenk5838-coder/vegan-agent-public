import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/api.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { api, ApiError, safeExternalUrl } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('uses the same-origin API and preserves the supplied idempotency body', async () => {
  const body = JSON.stringify({ content: '豆腐', idempotency_key: 'same-operation' });
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/v1/chat/sessions/s1/messages');
    assert.equal(init.credentials, 'same-origin');
    assert.equal(init.body, body);
    assert.equal(init.headers.get('Content-Type'), 'application/json');
    return Response.json({ turn_id: 't1', status: 'queued' }, { status: 201 });
  };
  assert.deepEqual(await api('/chat/sessions/s1/messages', { method: 'POST', body }), { turn_id: 't1', status: 'queued' });
});

test('preserves backend conflict code and status', async () => {
  globalThis.fetch = async () => Response.json({ error: { code: 'IDEMPOTENCY_CONFLICT', message: '内容已变化' } }, { status: 409 });
  await assert.rejects(api('/foods/drafts/d1/confirm'), error => error instanceof ApiError && error.code === 'IDEMPOTENCY_CONFLICT' && error.status === 409 && error.message === '内容已变化');
});

test('lost response never automatically repeats a mutating request', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError('network'); };
  await assert.rejects(api('/foods/drafts/d1/confirm', { method: 'POST', body: '{}' }), error => error instanceof ApiError && error.status === 0 && error.code === 'CONNECTION_LOST');
  assert.equal(calls, 1);
});

test('does not assume an unreadable successful response means saved', async () => {
  globalThis.fetch = async () => new Response('invalid-json', { status: 200 });
  await assert.rejects(api('/foods/drafts/d1/confirm'), error => error.code === 'INVALID_RESPONSE');
});

test('lets the browser set the multipart boundary for photos', async () => {
  const body = new FormData(); body.set('save_original', 'false');
  globalThis.fetch = async (_url, init) => {
    assert.equal(init.headers.has('Content-Type'), false);
    assert.equal(init.body, body);
    return Response.json({ job_id: 'photo-1' });
  };
  await api('/ingredients/photo', { method: 'POST', body });
});

test('cancellation remains cancellation when a conversation is switched', async () => {
  const controller = new AbortController(); controller.abort();
  const aborted = new DOMException('Aborted', 'AbortError');
  globalThis.fetch = async () => { throw aborted; };
  await assert.rejects(api('/chat/sessions/s1/messages', { signal: controller.signal }), error => error === aborted);
});

test('recipe source links only allow http and https URLs', () => {
  assert.equal(safeExternalUrl('javascript:alert(1)'), undefined);
  assert.equal(safeExternalUrl('data:text/html,test'), undefined);
  assert.equal(safeExternalUrl('/relative'), undefined);
  assert.equal(safeExternalUrl('https://example.com/recipe'), 'https://example.com/recipe');
});
