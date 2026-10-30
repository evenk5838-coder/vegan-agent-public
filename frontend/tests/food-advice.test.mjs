import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const source = readFileSync(new URL('../lib/food-advice.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { foodAdviceView, foodReferenceView, foodUsageView, prepareFoodConfirmation, foodConfirmationBody, autoAssessmentMessage } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));

test('missing information and a complete input with no published rule have different guidance', () => {
  assert.match(foodAdviceView({ reason: 'missing_info', missing: ['food_class'] }).title, /补充/);
  const noRule = foodAdviceView({ reason: 'no_published_rule', missing: [] });
  assert.match(noRule.title, /暂无适用/);
  assert.match(noRule.text, /不需要反复补填/);
  assert.equal(noRule.date, null);
});

test('only published rule or package evidence can present a system reference date', () => {
  const date = '2026-10-28T08:00:00+08:00';
  for (const basis of [{ type: 'none' }, { type: 'user' }, { type: 'rule' }]) {
    assert.equal(foodAdviceView({ action: 'date', date, basis, rules_status: 'draft' }).date, null);
  }
  assert.equal(foodAdviceView({ action: 'date', date, basis: { type: 'package' } }).date, date);
  const rule = foodAdviceView({ action: 'date', date, basis: { type: 'rule', rule_source: 'https://example.org/rule' }, rules_status: 'published' });
  assert.equal(rule.date, date);
  assert.equal(rule.source, 'https://example.org/rule');
});

test('an expired package date stays visible as expired even when action is unknown', () => {
  const advice = foodAdviceView({ action: 'unknown', date: '2026-10-20T08:00:00+08:00', reason: 'date_passed', basis: { type: 'package' } });
  assert.match(advice.title, /已过/);
  assert.ok(advice.date);
  assert.equal(advice.warning, true);
});

test('unusable package conditions and invalid dates cannot create a shown expiry', () => {
  const advice = foodAdviceView({ action: 'unknown', reason: 'package_not_applicable', basis: { type: 'none' } });
  assert.match(advice.title, /不适用/);
  assert.equal(advice.date, null);
  assert.equal(foodAdviceView({ action: 'date', date: 'not-a-date', basis: { type: 'package' } }).date, null);
});

test('usage guidance preserves recorded event and storage facts without becoming a date estimate', () => {
  const suggestion = { action: 'unknown', date: null, reason: 'no_published_rule', basis: { type: 'none' },
    usage: { priority: 'use_soon', title: '生鲜优先安排，尽快使用', text: '不据购买日期推定食用安全。',
      observed: { food_class: 'fresh', storage: 'refrigerated', date_type: 'purchase', started_at: '2026-10-25T08:00:00+08:00' } } };
  const usage = foodUsageView(suggestion);
  assert.equal(usage.priority, 'use_soon');
  assert.ok(usage.facts.includes('冷藏'));
  assert.ok(usage.facts.some(text => text.startsWith('购买：') && text.includes('2026')));
  assert.equal(foodAdviceView(suggestion).date, null);
  assert.equal('date' in usage, false);
  assert.equal('percent' in usage, false);
});

test('expired recorded dates, invalid packaging and missing facts override a generic use-soon caption', () => {
  const base = { usage: { priority: 'use_soon', title: '尽快使用', text: '优先安排',
    observed: { storage: 'refrigerated', expires_at: '2026-10-20T08:00:00+08:00', deadline_source: 'user' } } };
  const now = Date.parse('2026-10-27T08:00:00+08:00');
  assert.equal(foodUsageView(base, now).priority, 'check_first');
  assert.match(foodUsageView(base, now).title, /已过/);
  for (const reason of ['package_not_applicable', 'missing_info']) {
    const view = foodUsageView({ ...base, reason, usage: { ...base.usage, observed: { storage: 'frozen' } } }, now);
    assert.equal(view.priority, 'check_first');
    assert.equal(view.title.includes('尽快'), false);
  }
});

test('old responses without usage retain their existing guidance', () => {
  assert.equal(foodUsageView({ reason: 'no_published_rule' }), null);
  assert.match(foodAdviceView({ reason: 'no_published_rule' }).title, /暂无适用/);
});

test('confirmation binds the seen digest and only unsaved items; a later operation gets a new key', () => {
  const draft = { draft_id: 'd', advice_digest: 'seen-v1', items: [{ item_id: 'a', saved_food_id: 'f' }, { item_id: 'b' }] };
  const first = prepareFoodConfirmation(draft, () => 'key-1');
  assert.deepEqual(foodConfirmationBody(first), { item_ids: ['b'], idempotency_key: 'key-1', advice_digest: 'seen-v1' });
  assert.deepEqual(foodConfirmationBody(first), foodConfirmationBody(first), 'transport replay keeps the exact attempt');
  const later = prepareFoodConfirmation({ ...draft, advice_digest: 'seen-v2' }, () => 'key-2');
  assert.equal(later.key, 'key-2');
  assert.equal(later.digest, 'seen-v2');
});

test('missing digest or no remaining items cannot manufacture confirmation protection', () => {
  assert.throws(() => prepareFoodConfirmation({ draft_id: 'd', items: [{ item_id: 'a' }] }, () => 'key'), /核对凭据/);
  assert.throws(() => prepareFoodConfirmation({ draft_id: 'd', advice_digest: 'v', items: [{ item_id: 'a', saved_food_id: 'f' }] }, () => 'key'), /没有待保存/);
});
test('batch confirmation keeps name-only items even when advice needs optional storage facts', () => {
  const draft = { draft_id: 'partial', advice_digest: 'seen-partial', items: [
    { item_id: 'a', name: '豆腐', storage: 'unknown', started_at: null, missing: ['storage', 'started_at'] },
    { item_id: 'b', name: '西兰花', condition: 'unknown', missing: ['condition'] },
  ] };
  assert.deepEqual(foodConfirmationBody(prepareFoodConfirmation(draft, () => 'single-confirm')), {
    item_ids: ['a', 'b'], idempotency_key: 'single-confirm', advice_digest: 'seen-partial',
  });
});

test('automatic assessment copy follows server status and never claims success merely from a saved condition', () => {
  assert.match(autoAssessmentMessage({ auto_reevaluated: false }), /未报告已完成/);
  assert.match(autoAssessmentMessage({ auto_reevaluated: false, reason: 'user_date_preserved' }), /自行设置/);
  assert.match(autoAssessmentMessage({ auto_reevaluated: true, reason: 'no_published_rule', assessment_status: 'needs_review' }), /未找到/);
  assert.match(autoAssessmentMessage({ auto_reevaluated: true, reason: 'ok' }), /已重新核对/);
});

test('planning references are optional display data and do not become a deadline or change usage priority', () => {
  const base = { action: 'unknown', date: null, reason: 'missing_info', basis: { type: 'none' },
    usage: { priority: 'check_first', title: '已记下', text: '按实际储存条件安排使用。', observed: { storage: 'unknown', started_at: null } } };
  const withReference = { ...base, usage: { ...base.usage, reference: {
    kind: 'planning_reference', title: '有条件的安排参考', text: '这是一段参考原文的摘要。',
    source_url: 'https://example.org/official-reference', source_title: '来源资料', conditions: [' 已熟制 ', '持续冷藏', ''],
  } } };
  assert.deepEqual(foodAdviceView(withReference), foodAdviceView(base));
  assert.deepEqual(foodUsageView(withReference), foodUsageView(base));
  const view = foodReferenceView(withReference);
  assert.deepEqual(view.conditions, ['已熟制', '持续冷藏']);
  assert.equal('date' in view, false);
  assert.equal('expires_at' in view, false);
  assert.equal('remind_at' in view, false);
  assert.equal(foodReferenceView(base), null);
  assert.equal(foodReferenceView(null), null);
  assert.equal(foodReferenceView({ usage: { reference: { kind: 'expiry_rule', title: '不支持的类型', text: '不展示' } } }), null);
  assert.equal(foodReferenceView({ usage: { reference: { kind: 'planning_reference', title: ' ', text: '不完整资料' } } }), null);
});

const referenceAt = Date.parse('2026-10-27T12:00:00+08:00');
const timedReference = observed => ({ reason: 'no_published_rule', usage: { observed, reference: {
  kind: 'planning_reference', title: '有条件的安排参考', text: '按资料所述条件对照。',
  source_url: 'https://example.org/reference', source_title: '参考来源', conditions: ['持续冷藏'],
} } });

test('a reference from an earlier response disappears once its observed expiry is reached without changing saved facts', () => {
  const suggestion = timedReference({ started_at: new Date(referenceAt - 86400000).toISOString(), expires_at: new Date(referenceAt + 3600000).toISOString() });
  const snapshot = JSON.stringify(suggestion);
  assert.ok(foodReferenceView(suggestion, referenceAt));
  assert.equal(foodReferenceView(suggestion, referenceAt + 3600000), null);
  assert.equal(foodReferenceView(suggestion, referenceAt + 3600001), null);
  assert.equal(JSON.stringify(suggestion), snapshot);
});

test('expired or inapplicable server reasons hide a reference even if the snapshot dates appear current', () => {
  const suggestion = timedReference({ expires_at: new Date(referenceAt + 86400000).toISOString() });
  for (const reason of ['date_passed', 'package_not_applicable']) {
    assert.equal(foodReferenceView({ ...suggestion, reason }, referenceAt), null);
  }
});

test('invalid observed dates and future starts cannot keep planning references visible', () => {
  for (const observed of [{ expires_at: 'not-a-date' }, { expires_at: ' ' }, { started_at: 'not-a-date' }, { started_at: ' ' },
    { started_at: new Date(referenceAt + 1).toISOString() }]) {
    assert.equal(foodReferenceView(timedReference(observed), referenceAt), null);
  }
  for (const observed of [{}, { started_at: null, expires_at: null }, { started_at: '', expires_at: '' }, { started_at: new Date(referenceAt).toISOString() }]) {
    assert.ok(foodReferenceView(timedReference(observed), referenceAt));
  }
});
