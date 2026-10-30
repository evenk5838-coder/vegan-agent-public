import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const { outputText } = ts.transpileModule(readFileSync(new URL('../lib/voice-session.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { VoiceSessionGate, pollVoiceJob } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
const options = (patch = {}) => ({ signal: new AbortController().signal, sleep: async () => {}, maxReads: 3, ...patch });

test('one hold claims one upload even when the response is uncertain', () => {
  const gate = new VoiceSessionGate();
  const ticket = gate.begin();
  assert.equal(gate.claimUpload(ticket), true);
  assert.equal(gate.claimUpload(ticket), false);
  assert.equal(gate.isCurrent(ticket), true);
});

test('cancel and a new hold invalidate stale recorder, upload and delivery callbacks', () => {
  const gate = new VoiceSessionGate();
  const first = gate.begin();
  gate.cancel();
  assert.equal(gate.isCurrent(first), false);
  assert.equal(gate.claimUpload(first), false);
  assert.equal(gate.claimDelivery(first), false);
  const second = gate.begin();
  assert.notEqual(first, second);
  assert.equal(gate.claimUpload(second), true);
  assert.equal(gate.claimDelivery(first), false);
});

test('delivery is once per session and explicit recovery needs no new upload', () => {
  const gate = new VoiceSessionGate();
  const recovery = gate.begin();
  assert.equal(gate.claimDelivery(recovery), true);
  assert.equal(gate.claimDelivery(recovery), false);
  assert.equal(gate.claimUpload(recovery), false);
  const next = gate.begin();
  assert.equal(gate.claimDelivery(recovery), false);
  assert.equal(gate.claimDelivery(next), true);
});

test('GET polling progresses to ready without any upload or chat operation', async () => {
  const jobs = [{ status: 'pending' }, { status: 'running' }, { status: 'succeeded', text: ' 豆腐已冷藏。 ' }];
  const states = [];
  let reads = 0, sleeps = 0;
  const result = await pollVoiceJob(async signal => {
    assert.ok(signal instanceof AbortSignal);
    return jobs[reads++];
  }, options({ sleep: async () => { sleeps++; }, onState: job => states.push(job.status) }));
  assert.equal(result.state, 'ready');
  assert.equal(result.text, '豆腐已冷藏。');
  assert.deepEqual(states, ['pending', 'running', 'succeeded']);
  assert.equal(reads, 3);
  assert.equal(sleeps, 2);
});

test('temporary GET failures can recover the same job instead of failing transcription', async () => {
  let reads = 0;
  const result = await pollVoiceJob(async () => {
    if (++reads === 1) throw new Error('temporary connection error');
    return { status: 'succeeded', text: '可以取回。' };
  }, options());
  assert.equal(result.state, 'ready');
  assert.equal(reads, 2);
});

test('exhausted GET failures stay unconfirmed and cannot become provider failure', async () => {
  let reads = 0;
  const result = await pollVoiceJob(async () => { reads++; throw new Error('connection interrupted'); }, options());
  assert.equal(reads, 3);
  assert.equal(result.state, 'unconfirmed');
  assert.equal(result.cause, 'read_failed');
  assert.match(result.reason, /重新读取/);
});

test('only an explicit failed server job returns failed', async () => {
  const result = await pollVoiceJob(async () => ({ status: 'failed', error: { message: '服务暂时不可用。' } }), options());
  assert.equal(result.state, 'failed');
  assert.equal(result.reason, '服务暂时不可用。');
  const cancelled = await pollVoiceJob(async () => ({ status: 'cancelled' }), options());
  assert.equal(cancelled.state, 'unconfirmed');
  assert.equal(cancelled.cause, 'cancelled');
});

test('successful but empty transcripts are not delivered as messages', async () => {
  for (const text of ['', ' \n ', null, undefined]) {
    const result = await pollVoiceJob(async () => ({ status: 'succeeded', text }), options());
    assert.equal(result.state, 'empty');
    assert.equal('text' in result, false);
  }
});

test('expired and missing jobs are handled separately from provider failure', async () => {
  const expired = await pollVoiceJob(async () => ({ status: 'succeeded', text: '旧文字', expires_at: '2026-10-29T00:00:00Z' }),
    options({ now: () => Date.parse('2026-10-30T00:00:00Z') }));
  assert.equal(expired.state, 'expired');
  const missing = await pollVoiceJob(async () => { throw Object.assign(new Error('missing'), { status: 404 }); }, options());
  assert.equal(missing.state, 'expired');
});

test('unknown server states request a reread instead of claiming success', async () => {
  const result = await pollVoiceJob(async () => ({ status: 'future-state', text: '不可发送' }), options());
  assert.equal(result.state, 'unconfirmed');
  assert.equal(result.cause, 'unknown_state');
});

test('pending jobs stop at the read cap without cancelling the server job', async () => {
  let reads = 0;
  const result = await pollVoiceJob(async () => { reads++; return { status: 'running' }; }, options({ maxReads: 2 }));
  assert.equal(reads, 2);
  assert.equal(result.state, 'unconfirmed');
  assert.equal(result.cause, 'taking_longer');
});

test('elapsed polling budget stops before another GET and does not reset on reads', async () => {
  let time = 0, reads = 0;
  const result = await pollVoiceJob(async () => { reads++; return { status: 'running' }; },
    options({ maxReads: 20, maxWaitMs: 100, intervalMs: 60, now: () => time, sleep: async ms => { time += ms; } }));
  assert.equal(result.state, 'unconfirmed');
  assert.equal(result.cause, 'taking_longer');
  assert.equal(time, 100);
  assert.equal(reads, 2);
});

test('already aborted reads perform no GET', async () => {
  const controller = new AbortController();
  controller.abort();
  const result = await pollVoiceJob(async () => { throw new Error('must not read'); }, options({ signal: controller.signal }));
  assert.equal(result.state, 'aborted');
});

test('a late successful GET after cancellation cannot deliver a transcript', async () => {
  const controller = new AbortController();
  let finish;
  const result = pollVoiceJob(() => new Promise(resolve => { finish = resolve; }), options({ signal: controller.signal }));
  controller.abort();
  finish({ status: 'succeeded', text: '迟到结果' });
  assert.equal((await result).state, 'aborted');
});

test('abort during the default polling sleep wakes immediately without another GET', async () => {
  const controller = new AbortController();
  let reads = 0;
  const result = pollVoiceJob(async () => { reads++; return { status: 'pending' }; },
    { signal: controller.signal, intervalMs: 10000, onState: () => { setTimeout(() => controller.abort(), 0); } });
  assert.equal((await result).state, 'aborted');
  assert.equal(reads, 1);
});

test('refresh recovery only reads a saved job and each read can be repeated safely', async () => {
  let reads = 0;
  const read = async () => { reads++; return { id: 'saved-job', status: 'succeeded', text: '待用户确认的原转写' }; };
  assert.equal((await pollVoiceJob(read, options())).state, 'ready');
  assert.equal((await pollVoiceJob(read, options())).state, 'ready');
  assert.equal(reads, 2);
});
