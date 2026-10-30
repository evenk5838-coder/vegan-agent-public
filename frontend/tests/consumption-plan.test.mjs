import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const { outputText } = ts.transpileModule(readFileSync(new URL('../lib/consumption-plan.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { consumptionPlanView: view } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
const start = '2026-10-29T00:00:00+08:00', target = '2026-10-30T00:00:00+08:00';
const now = Date.parse(start) + 12 * 3600000;
const plan = { version: 'consumption-plan-2026-10-29.1', scope: 'eating_plan', safety_guarantee: false, status: 'ready', start_at: start, target_at: target,
  source: { title: '消费提示', url: 'https://www.hami.gov.cn/example' }, evidence_quote: '密封冷藏并在24小时内吃完' };

test('source plan uses original timestamps, not cached counters or generation time', () => {
  assert.equal(view(plan, now).label, '约剩 12 小时');
  assert.equal(view(plan, now).percent, 50);
  const reread = { ...plan, generated_at: new Date(now).toISOString(), remaining_days: 10, elapsed_percent: 0 };
  assert.deepEqual(view(reread, now), view(plan, now));
  assert.equal(view(reread, now + 3600000).label, '约剩 11 小时');
});
test('due time does not imply spoilage and progress clamps to the actual interval', () => {
  const due = view(plan, Date.parse(target) + 1000);
  assert.equal(due.label, '已到建议食用时间'); assert.equal(due.percent, 100);
  assert.equal(view(plan, Date.parse(start) - 1000).percent, 0);
  assert.match(view(plan, Date.parse(target) - 1000).label, /不到 1 小时/);
});
test('unavailable or outdated plans cannot show stale dates, source quotes or countdowns', () => {
  for (const status of ['outdated', 'needs_context', 'unavailable']) {
    const value = view({ ...plan, status }, now);
    assert.equal(value.target, null); assert.equal(value.percent, null); assert.equal(value.source, null); assert.equal(value.quote, null);
  }
});
test('invalid dates and protocol mismatches never fabricate time', () => {
  for (const patch of [{ start_at: null }, { target_at: 'bad' }, { target_at: start }, { start_at: target }]) {
    assert.equal(view({ ...plan, ...patch }, now).target, null);
  }
  for (const patch of [{ version: 'new-unknown' }, { scope: 'other' }, { safety_guarantee: true }, { status: 'unknown' }]) assert.equal(view({ ...plan, ...patch }, now), null);
  assert.equal(view(null, now), null);
});
test('only genuine HTTPS reference domains become links', () => {
  for (const url of ['javascript:alert(1)', 'https://gov.cn.evil.test', 'https://evil.gov.cn@evil.test', 'http://foodsafety.gov']) {
    assert.equal(view({ ...plan, source: { url } }, now).source, null);
  }
  assert.equal(view(plan, now).source.url, plan.source.url);
});

test('conservative goals keep their boundary and reminders, and never survive a context change', () => {
  const value = { ...plan, basis: 'conservative_schedule', message: '来源没有给出保存天数', key_reminders: ['非保质期', '冷藏中断请先核对', null] };
  const result = view(value, now);
  assert.equal(result.conservative, true);
  assert.deepEqual(result.reminders, ['非保质期', '冷藏中断请先核对']);
  assert.equal(result.target, target);
  assert.equal(result.message, '来源没有给出保存天数');
  for (const status of ['outdated', 'unavailable', 'needs_context']) {
    const stale = view({ ...value, status }, now);
    assert.equal(stale.conservative, false); assert.equal(stale.message, null); assert.deepEqual(stale.reminders, []);
  }
  assert.equal(view({ ...plan, basis: 'unknown' }, now), null);
});
