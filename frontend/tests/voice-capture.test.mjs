import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const { outputText } = ts.transpileModule(readFileSync(new URL('../lib/voice-capture.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { isPhoneMicrophone, preferredMicrophone, suspiciousTranscript, VoiceSignalMeter } = await import(
  'data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));

const device = (deviceId, label, kind = 'audioinput') => ({ deviceId, label, kind });
const alternating = (amplitude, length = 2048, offset = 0) => Float32Array.from(
  { length }, (_, index) => offset + (index % 2 ? amplitude : -amplitude));

test('phone and continuity labels are excluded from automatic microphone selection', () => {
  for (const label of ['iPhone Microphone', '念念的 iPad', 'Continuity Microphone', '连续互通麦克风', '接續互通麥克風', '手机麦克风'])
    assert.equal(isPhoneMicrophone(label), true, label);
  for (const label of ['MacBook Pro Microphone', 'Built-in Microphone', 'USB Headset', '内置麦克风'])
    assert.equal(isPhoneMicrophone(label), false, label);
});

test('a physical built-in device wins over aliases, a phone, and a local headset', () => {
  const builtIn = device('mac-mic', 'MacBook Pro Microphone');
  const inputs = [device('default', 'Default - iPhone Microphone'), device('phone', 'iPhone Microphone'),
    device('headset', 'USB Headset'), builtIn, device('communications', 'Built-in Microphone')];
  assert.equal(preferredMicrophone(inputs), builtIn);
});

test('explicit visible physical device selection is respected, including a phone chosen by the user', () => {
  const phone = device('phone', 'iPhone Microphone');
  const headset = device('headset', 'USB Headset');
  const inputs = [device('mac', '内置麦克风'), phone, headset];
  assert.equal(preferredMicrophone(inputs, 'headset'), headset);
  assert.equal(preferredMicrophone(inputs, 'phone'), phone);
});

test('aliases, anonymous inputs and audio outputs cannot become an automatic microphone', () => {
  const inputs = [device('default', '默认 - MacBook Pro Microphone'), device('communications', 'Communications'),
    device('anonymous', ''), device('', 'Built-in Microphone'), device('speakers', 'MacBook Speakers', 'audiooutput')];
  assert.equal(preferredMicrophone(inputs), null);
  assert.equal(preferredMicrophone(inputs, 'default'), null);
  assert.equal(preferredMicrophone(inputs, 'anonymous'), null);
});

test('phone-only devices return null automatically while a named USB fallback remains usable', () => {
  assert.equal(preferredMicrophone([device('p', 'iPhone Microphone')]), null);
  const usb = device('usb', 'USB Audio Device');
  assert.equal(preferredMicrophone([device('p', 'iPhone Microphone'), usb]), usb);
  assert.equal(preferredMicrophone([device('p', 'iPhone Microphone')], 'removed-device'), null);
});

test('built-in labels in supported UI languages precede the local fallback', () => {
  for (const label of ['Built-in Microphone', 'Internal Microphone', '内建麦克风', '內建麥克風']) {
    const builtIn = device('local', label);
    assert.equal(preferredMicrophone([device('headset', 'USB Headset'), builtIn]), builtIn);
  }
});

test('the exact unexpected outro is suspicious despite punctuation, spaces and invisible characters', () => {
  for (const text of ['请不吝点赞 订阅 转发 打赏支持明镜与点点栏目',
    '請不吝點讚、訂閱、轉發、打賞，支持明鏡與點點欄目。',
    '今天买了豆腐。请不吝点赞、订阅、转发、打赏，支持明镜与点点栏目。',
    '请不吝点赞\u200b订阅转发打赏支持明镜与点点栏目'])
    assert.equal(suspiciousTranscript(text), true, text);
});

test('ordinary short replies and questions about the quoted phrase remain available', () => {
  for (const text of ['', '谢谢', '谢谢观看', '请给这道菜点赞', '订阅提醒是什么意思？',
    '点点栏目介绍过哪些素食菜谱？', '“请不吝点赞订阅转发打赏支持明镜与点点栏目”是什么意思？'])
    assert.equal(suspiciousTranscript(text), false, text);
});

test('empty, invalid and silent audio blocks cannot satisfy the signal gate', () => {
  const meter = new VoiceSignalMeter();
  meter.add(new Float32Array(), 48000);
  meter.add(new Float32Array([NaN, 1]), 48000);
  meter.add(new Float32Array([Infinity, -1]), 48000);
  for (const rate of [NaN, Infinity, 0, -1]) meter.add(alternating(0.1), rate);
  meter.add(new Float32Array(48000), 48000);
  assert.deepEqual(meter.summary(), { audibleMs: 0, peak: 0, hasSignal: false });
});

test('constant DC offset is removed before evaluating loudness', () => {
  const meter = new VoiceSignalMeter();
  meter.add(new Float32Array(48000).fill(0.5), 48000);
  assert.deepEqual(meter.summary(), { audibleMs: 0, peak: 0, hasSignal: false });
});

test('duration accumulates samples, not callback counts, and needs at least 120ms', () => {
  const meter = new VoiceSignalMeter();
  meter.add(alternating(0.01), 48000);
  meter.add(alternating(0.01), 48000);
  assert.equal(meter.summary().hasSignal, false);
  assert.ok(Math.abs(meter.summary().audibleMs - 2048 / 48000 * 2000) < 0.0001);
  meter.add(alternating(0.01), 48000);
  assert.equal(meter.summary().hasSignal, true);
  assert.ok(Math.abs(meter.summary().audibleMs - 128) < 0.0001);
});

test('quiet AC stays rejected and a short isolated spike is insufficient', () => {
  const quiet = new VoiceSignalMeter();
  quiet.add(alternating(0.001, 48000, 0.1), 48000);
  assert.equal(quiet.summary().audibleMs, 0);
  assert.equal(quiet.summary().hasSignal, false);
  const spike = new VoiceSignalMeter();
  spike.add(new Float32Array([0.5, -0.5]), 48000);
  assert.equal(spike.summary().hasSignal, false);
});

test('both audible duration and the independent peak threshold are required', () => {
  const meter = new VoiceSignalMeter();
  meter.add(alternating(0.002, 48000), 48000);
  assert.ok(meter.summary().audibleMs >= 120);
  assert.equal(meter.summary().hasSignal, false);
  meter.add(alternating(0.006, 2048, 0.2), 48000);
  assert.equal(meter.summary().hasSignal, true);
});

test('summaries are independent snapshots and irregular blocks preserve physical duration', () => {
  const meter = new VoiceSignalMeter();
  meter.add(alternating(0.01, 1600), 16000);
  const before = meter.summary();
  meter.add(alternating(0.01, 400), 16000);
  assert.equal(before.hasSignal, false);
  assert.ok(Math.abs(before.audibleMs - 100) < 0.0001);
  assert.equal(meter.summary().audibleMs, 125);
  assert.equal(meter.summary().hasSignal, true);
});
