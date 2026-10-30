import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

async function load(path) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
  return import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
}
const { validatePhoto, PHOTO_MAX_BYTES, photoJobLabel, photoIsTerminal, readPhotoState } = await load('../lib/chat-photo.ts');
const { apiBlob, ApiError } = await load('../lib/api.ts');
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('photo upload accepts only supported nonempty images up to the backend limit', () => {
  assert.equal(validatePhoto({ type: 'image/jpeg', size: PHOTO_MAX_BYTES }), null);
  assert.equal(validatePhoto({ type: 'image/png', size: 100 }), null);
  assert.equal(validatePhoto({ type: 'image/webp', size: 100 }), null);
  assert.match(validatePhoto({ type: 'image/heic', size: 100 }), /JPG/);
  assert.match(validatePhoto({ type: 'image/jpeg', size: 0 }), /为空/);
  assert.match(validatePhoto({ type: 'image/jpeg', size: PHOTO_MAX_BYTES + 1 }), /10MB/);
});

test('pending jobs and unknown states do not claim successful recognition', () => {
  for (const status of ['pending', 'queued', 'running', 'unrecognized']) {
    assert.equal(photoIsTerminal({ status }), false);
    assert.doesNotMatch(photoJobLabel({ status }), /识别已完成/);
  }
  assert.match(photoJobLabel({ status: 'unrecognized' }), /无法确认/);
  assert.match(photoJobLabel({ status: 'succeeded' }), /识别已完成/);
});

test('failed recognition shows the backend failure and stops polling', () => {
  const job = { status: 'failed', result: { error: { message: '这张图片不清晰' } } };
  assert.equal(photoIsTerminal(job), true);
  assert.equal(photoJobLabel(job), '这张图片不清晰');
  assert.equal(photoIsTerminal({ status: 'cancelled' }), true);
});

test('past saving consent alone never grants a photo view action', () => {
  assert.deepEqual(readPhotoState({}), { present: false, saved: false, viewable: false, assetId: null, expiresAt: null });
  assert.equal(readPhotoState({ photo: { saved_original: true, asset_id: 'a1' } }).viewable, false);
  assert.equal(readPhotoState({ photo: { saved_original: true, viewable: true, asset_id: null } }).viewable, false);
  const cleaned = readPhotoState({ photo: { saved_original: true, viewable: false, asset_id: null } });
  assert.equal(cleaned.saved, true);
  assert.equal(cleaned.viewable, false);
  assert.equal(readPhotoState({ photo: { saved_original: true, viewable: true, asset_id: 'a1' } }).viewable, true);
});

test('saved image request uses authenticated same-origin binary access and no cache', async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/v1/images/a1');
    assert.equal(init.credentials, 'same-origin');
    assert.equal(init.cache, 'no-store');
    return new Response(new Blob(['photo'], { type: 'image/png' }));
  };
  const image = await apiBlob('/images/a1');
  assert.equal(image.type, 'image/png');
  assert.equal(await image.text(), 'photo');
});

test('stale saved-image card receives a detectable 404, without retrying or fabricating a blob', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({ error: { code: 'NOT_FOUND', message: '原图已清理。' } }, { status: 404 }); };
  await assert.rejects(apiBlob('/images/deleted'), error => error instanceof ApiError && error.status === 404 && error.code === 'NOT_FOUND');
  assert.equal(calls, 1);
});

test('binary request preserves cancellation rather than converting it to a user failure', async () => {
  const controller = new AbortController();
  controller.abort();
  const cancelled = new DOMException('Aborted', 'AbortError');
  globalThis.fetch = async () => { throw cancelled; };
  await assert.rejects(apiBlob('/images/a1', { signal: controller.signal }), error => error === cancelled);
});

test('binary connection loss never automatically retries', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError('network'); };
  await assert.rejects(apiBlob('/images/a1'), error => error instanceof ApiError && error.code === 'CONNECTION_LOST');
  assert.equal(calls, 1);
});
