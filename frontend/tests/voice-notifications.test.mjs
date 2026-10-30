import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

async function load(path) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
  return import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
}
const voice = await load('../lib/voice-input.ts');
const notifications = await load('../lib/notifications.ts');
const limits = { configured: true, mime_types: ['audio/wav', 'audio/webm', 'audio/mp4', 'audio/mpeg', 'audio/ogg'], max_bytes: 100, max_seconds: 300, min_seconds: 0.2 };
const now = Date.parse('2026-10-26T12:00:00Z');

test('a WebM upload declares its actual duration, not a placeholder maximum', () => {
  const form = voice.transcriptionForm(new Blob(['container'], { type: 'audio/webm;codecs=opus' }), 'voice.webm', 8.125, limits);
  assert.equal(form.get('duration_seconds'), '8.125');
  assert.equal(form.get('file').type, 'audio/webm');
  assert.equal(form.get('file').name, 'voice.webm');
});

test('missing or invalid duration, oversized files and unsupported containers never produce an upload', () => {
  const file = new Blob(['audio'], { type: 'audio/mp4' });
  for (const duration of [NaN, Infinity, 0, -1, 0.1, 301]) {
    assert.throws(() => voice.transcriptionForm(file, 'voice.m4a', duration, limits));
  }
  assert.throws(() => voice.transcriptionForm(new Blob([]), 'voice.wav', 2, limits), /为空/);
  assert.throws(() => voice.transcriptionForm(new Blob(['x'.repeat(101)], { type: 'audio/wav' }), 'voice.wav', 2, limits), /过大/);
  assert.throws(() => voice.transcriptionForm(new Blob(['x'], { type: 'video/mp4' }), 'video.mp4', 2, limits), /格式/);
});

test('common file MIME aliases normalize while final container verification remains on the server', () => {
  assert.equal(voice.audioMime('audio/x-wav', 'voice.wav'), 'audio/wav');
  assert.equal(voice.audioMime('audio/x-m4a', 'voice.m4a'), 'audio/mp4');
  assert.equal(voice.audioMime('', 'voice.MP3'), 'audio/mpeg');
  assert.equal(voice.audioMime('', 'voice.unknown'), '');
});

test('resumable edited text is account scoped, keeps intentional empty edits, and expires', () => {
  assert.notDeepEqual(voice.voiceKeys('alice'), voice.voiceKeys('bob'));
  const draft = { jobId: 'job-a', text: '', simulated: false, expiresAt: '2026-10-26T13:00:00Z' };
  assert.deepEqual(voice.parseVoiceDraft(JSON.stringify(draft), now), draft);
  assert.equal(voice.parseVoiceDraft(JSON.stringify(draft), now + 3600000), null);
  assert.equal(voice.parseVoiceDraft('{broken', now), null);
  assert.equal(voice.parseVoiceDraft(JSON.stringify({ ...draft, text: 'x'.repeat(4001) }), now), null);
});

test('local edits never extend server expiry or the maximum 24 hour lifetime', () => {
  assert.equal(voice.draftExpiry('2026-10-26T13:00:00Z', now), '2026-10-26T13:00:00.000Z');
  assert.equal(voice.draftExpiry('2027-01-01T00:00:00Z', now), '2026-10-27T12:00:00.000Z');
  assert.equal(voice.draftExpiry(null, now), '2026-10-27T12:00:00.000Z');
});

test('unknown or expired task states do not become successful transcription', () => {
  assert.equal(voice.transcriptionState({ status: 'succeeded', expires_at: '2026-10-26T11:00:00Z' }, now), 'expired');
  assert.equal(voice.transcriptionState({ status: 'future-status' }, now), 'unknown');
  assert.equal(voice.transcriptionState({ status: 'running' }, now), 'running');
  assert.equal(voice.transcriptionLabel('future-status'), '任务状态待核对');
  assert.equal(voice.VOICE_POLL_LIMIT, 60);
});

test('push service acceptance is distinct from device display even with misleading result fields', () => {
  const task = { id: 'n1', due: '2026-10-26T12:00:00Z', status: 'accepted', result: { accepted_by_service: 1, device_display: 'delivered' } };
  const state = notifications.notificationTaskState(task);
  assert.equal(state.accepted, true);
  assert.equal(state.deviceDisplay, '待真机核对');
  assert.match(state.label, /设备显示待核对/);
  assert.equal(notifications.notificationTaskState({ ...task, status: 'succeeded' }).accepted, false);
  assert.equal(notifications.notificationTaskState({ ...task, status: 'mystery' }).label, '任务状态待核对');
});

test('four levels keep task, subscription, service acceptance and phone display separate', () => {
  const task = { id: 'n1', status: 'accepted', due: '2026-10-26T12:00:00Z' };
  const levels = notifications.notificationLevels([task], false);
  assert.deepEqual(levels.map(row => row.title), ['提醒任务', '设备订阅', '服务接受', '真机显示']);
  assert.match(levels[1].detail, /尚未确认/);
  assert.match(levels[2].detail, /1 项已获推送服务接受/);
  assert.match(levels[3].detail, /没有设备送达回执/);
  assert.match(notifications.notificationLevels([task], true)[1].detail, /已保存到当前账号/);
});

test('loading or failed task reads are not presented as an empty reminder list', () => {
  assert.match(notifications.notificationLevels(null, false)[0].detail, /尚未取回/);
  assert.match(notifications.notificationLevels(null, false)[2].detail, /尚未取回/);
  assert.match(notifications.notificationLevels([], false)[0].detail, /还没有提醒任务/);
  assert.equal(notifications.notificationTaskState({ id: 'n', status: 'failed', due: '', result: { error: { message: '服务未接受' } } }).error, '服务未接受');
});

test('notification keys decode URL-safe base64 with padding and reject malformed public keys', () => {
  const bytes = Uint8Array.from([4, ...Array.from({ length: 64 }, (_, index) => index + 128)]);
  const encoded = Buffer.from(bytes).toString('base64url');
  const decoded = notifications.pushApplicationKey(encoded);
  assert.deepEqual(decoded, bytes);
  assert.equal(notifications.samePushKey(decoded.buffer, bytes), true);
  assert.equal(notifications.samePushKey(new Uint8Array(65).buffer, bytes), false);
  assert.equal(notifications.samePushKey(null, bytes), false);
  for (const invalid of ['', 'bad*key', 'YQ', Buffer.from(new Uint8Array(65)).toString('base64url')]) {
    assert.throws(() => notifications.pushApplicationKey(invalid), /配置格式/);
  }
});
