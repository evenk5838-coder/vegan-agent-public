import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

// Render actual advice/inventory components without effects or business services.
const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL('../', import.meta.url));
const cache = new Map();
function load(file) {
  const path = resolve(root, file);
  if (cache.has(path)) return cache.get(path).exports;
  const module = { exports: {} }; cache.set(path, module);
  const { outputText } = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  });
  const localRequire = name => {
    if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_target, key) => String(key) }) };
    if (!name.startsWith('.')) return require(name);
    const base = resolve(dirname(path), name);
    const target = [base, base + '.ts', base + '.tsx'].find(candidate => existsSync(candidate));
    assert.ok(target, `Missing test import ${name}`);
    return load(target);
  };
  new Function('require', 'module', 'exports', outputText)(localRequire, module, module.exports);
  return module.exports;
}
const FoodAdvice = load('components/FoodAdvice.tsx').default;
const FridgeInventory = load('components/FridgeInventory.tsx').default;
const FoodEntry = load('components/FoodEntry.tsx').default;
const FoodReminderDialog = load('components/FoodReminderDialog.tsx').default;
const ConsumptionPlan = load('components/ConsumptionPlan.tsx').default;
const suggestion = {
  action: 'unknown', date: null, basis: { type: 'none' }, reason: 'no_published_rule', missing: [],
  usage: { priority: 'use_soon', title: '生鲜优先安排，尽快使用', text: '不据购买或生产日期推定仍能安全食用。',
    observed: { food_class: 'fresh', storage: 'refrigerated', date_type: 'purchase', started_at: '2026-10-25T08:00:00+08:00' } },
};

test('usage guidance and its recorded facts are visible without creating a reference date', () => {
  const html = renderToStaticMarkup(React.createElement(FoodAdvice, { suggestion }));
  assert.ok(html.includes('生鲜优先安排，尽快使用'));
  assert.ok(html.includes('不据购买或生产日期推定仍能安全食用'));
  assert.ok(html.includes('冷藏'));
  assert.ok(html.includes('购买：'));
  assert.equal(html.includes('参考日期：'), false);
  assert.equal(html.includes('<progress'), false);
});

test('the closed inventory card already shows the use plan, without waiting for expansion', () => {
  const row = { id: 'synthetic-food', name: '豆腐', storage: 'refrigerated', condition: 'raw', location: 'unknown',
    food_class: 'fresh', opened: null, started_at: '2026-10-25T08:00:00+08:00', date_type: 'purchase',
    expires_at: null, deadline_source: 'unknown', package_note: '', remind_at: null, temperature_c: null,
    status: 'active', display_status: 'needs_review', assessment_status: 'unknown', storage_history: [], suggestion };
  const html = renderToStaticMarkup(React.createElement(FridgeInventory, { rows: [row], onChanged: async () => {} }));
  assert.ok(html.includes('aria-expanded="false"'));
  assert.ok(html.includes('生鲜优先安排，尽快使用'));
  assert.ok(html.includes('不据购买或生产日期推定仍能安全食用'));
  assert.equal(html.includes('<progress'), false);
  assert.equal(html.includes('预计日期'), false);
});

test('a stale draft suggestion is explicitly identified while preserving the prior record facts', () => {
  const html = renderToStaticMarkup(React.createElement(FoodAdvice, { suggestion, stale: true }));
  assert.ok(html.includes('修改尚未核对，以下仍是上次建议'));
  assert.ok(html.includes('购买：'));
  assert.equal(html.includes('参考日期：'), false);
});

const partial = { id: 'name-only', name: '名称先记的豆腐', storage: 'unknown', condition: 'unknown', location: 'unknown',
  food_class: 'unknown', opened: null, started_at: null, date_type: 'unknown', created: '2026-10-27T12:00:00+08:00',
  expires_at: null, deadline_source: 'unknown', package_note: '', remind_at: null, temperature_c: null,
  status: 'active', display_status: 'needs_review', assessment_status: 'needs_review', storage_history: [] };

test('name-only cards have an unclassified storage badge and use registration date without a fake lifetime', () => {
  const html = renderToStaticMarkup(React.createElement(FridgeInventory, { rows: [partial], onChanged: async () => {} }));
  assert.ok(html.includes('名称先记的豆腐'));
  assert.ok(html.includes('待补存放'));
  assert.ok(html.includes('未分类'));
  assert.ok(html.includes('illustration unclassified'));
  assert.ok(html.includes('登记于 10/27'));
  assert.equal(html.includes('Invalid Date'), false);
  assert.equal(html.includes('<progress'), false);
  assert.equal(html.includes('记录于'), false);
});

test('unknown registration dates render a safe optional-date hint, never the current date or epoch', () => {
  for (const created of [null, undefined, 'not-a-date']) {
    const html = renderToStaticMarkup(React.createElement(FridgeInventory, { rows: [{ ...partial, created }], onChanged: async () => {} }));
    assert.ok(html.includes('日期可后补'));
    assert.equal(html.includes('Invalid Date'), false);
    assert.equal(html.includes('登记于'), false);
    assert.equal(html.includes('<progress'), false);
  }
});

test('single-entry form requires only the name and collapses optional processing and expiry details', () => {
  const html = renderToStaticMarkup(React.createElement(FoodEntry, { onSaved: async () => {} }));
  assert.equal((html.match(/required=""/g) || []).length, 1);
  assert.match(html, /<input(?=[^>]*name="name")(?=[^>]*required="")[^>]*>/);
  assert.ok(html.includes('存放方式（可选）'));
  assert.ok(html.includes('已知日期（可选）'));
  assert.ok(html.includes('下一步：确认食材'));
  assert.match(html, /<details><summary>补充信息（可选）<\/summary>[\s\S]*name="storage"[\s\S]*name="started_at"[\s\S]*name="condition"[\s\S]*name="expires_at"[\s\S]*<\/details>/);
  assert.ok(html.includes('<option value="unknown" selected="">稍后补充'));
  assert.equal(html.includes('name="started_at" type="datetime-local" required'), false);
});

test('missing facts do not become a required checklist or an invented food deadline', () => {
  const html = renderToStaticMarkup(React.createElement(FoodAdvice, { suggestion: {
    reason: 'missing_info', missing: ['food_class', 'opened', 'temperature_c'], basis: { type: 'none' },
    usage: { title: '补充信息', text: '需要填写所有条件', observed: {} },
  } }));
  assert.match(html, /暂未确定食用日期/);
  assert.doesNotMatch(html, /待补：|温度|需要填写所有条件|参考日期：|<progress/);
});

test('reminder form distinguishes saving a time from actual device notification delivery', () => {
  const html = renderToStaticMarkup(React.createElement(FoodReminderDialog, {
    open: true, onClose: () => {}, foods: [partial], onChanged: async () => {}, initialFoodId: partial.id,
  }));
  assert.ok(html.includes('主动推送需先开启设备通知'));
  assert.ok(html.includes('保存提醒时间不代表消息已送达'));
});

const reference = { kind: 'planning_reference', title: '有条件的食用安排参考', text: '按资料所述情况安排使用。',
  source_url: 'https://example.org/official-reference', source_title: '官方参考资料', conditions: ['仅适用于已熟制食材', '持续冷藏，按实际情况对照'] };
const referenced = changes => ({ ...suggestion, usage: { ...suggestion.usage, reference: { ...reference, ...changes } } });

test('conditional planning references expose a safe source and collapsed conditions without creating dates or progress', () => {
  for (const source_url of ['https://example.org/official-reference', 'http://example.org/reference']) {
    const html = renderToStaticMarkup(React.createElement(FoodAdvice, { suggestion: referenced({ source_url }) }));
    assert.ok(html.includes(reference.title));
    assert.ok(html.includes(reference.text));
    assert.ok(html.includes('按所列条件对照，不是安全期限'));
    assert.ok(html.includes(`href="${source_url}" target="_blank" rel="noreferrer"`));
    assert.ok(html.includes('来源：官方参考资料'));
    assert.match(html, /<details><summary>适用条件<\/summary><ul><li>仅适用于已熟制食材<\/li>/);
    assert.equal(html.includes('<time'), false);
    assert.equal(html.includes('<progress'), false);
    assert.equal(html.includes('参考日期：'), false);
    assert.equal(html.includes('已所设日期'), false);
  }
});

test('unsafe reference URLs cannot produce clickable links and reference text is escaped', () => {
  for (const source_url of ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', 'file:///tmp/reference', '//example.org/reference']) {
    const html = renderToStaticMarkup(React.createElement(FoodAdvice, { suggestion: referenced({ source_url, text: '<script>unsafe text</script>' }) }));
    assert.equal(html.includes('href='), false);
    assert.equal(html.includes('<script>'), false);
    assert.ok(html.includes('&lt;script&gt;unsafe text&lt;/script&gt;'));
    assert.ok(html.includes('不是安全期限'));
  }
});

test('references do not replace an existing package date or add new card dates', () => {
  const withReference = { ...referenced({}), action: 'date', date: '2099-10-28T08:00:00+08:00', basis: { type: 'package' } };
  const advice = renderToStaticMarkup(React.createElement(FoodAdvice, { suggestion: withReference }));
  assert.equal((advice.match(/<time /g) || []).length, 1);
  assert.ok(advice.includes('dateTime="2099-10-28T08:00:00+08:00"'));
  assert.equal(advice.includes('<progress'), false);
  const card = renderToStaticMarkup(React.createElement(FridgeInventory, { rows: [{ ...partial, suggestion: referenced({}) }], onChanged: async () => {} }));
  assert.ok(card.includes('登记于 10/27'));
  assert.equal(card.includes(reference.title), false, 'expanded reference stays out of the closed inventory card');
  assert.equal(card.includes('<progress'), false);
});

test('old usage without a reference has no empty reference section or conditions control', () => {
  const html = renderToStaticMarkup(React.createElement(FoodAdvice, { suggestion }));
  assert.ok(html.includes(suggestion.usage.title));
  assert.equal(html.includes('食用安排参考'), false);
  assert.equal(html.includes('适用条件'), false);
  assert.equal(html.includes('不是安全期限'), false);
});

const storageWeb = load('lib/storage-guidance.ts');
const StorageGuidance = load('components/StorageGuidance.tsx').default;
const StorageGuidanceContent = load('components/StorageGuidance.tsx').StorageGuidanceContent;
function webResult(overrides = {}) {
  return { food_id: 'f1', status: 'available', guidance: { status: 'ready', scope: 'storage_reference_only',
    summary: '对照包装与冷藏条件，优先安排使用。', observed: { name: '豆腐' },
    conditions_to_check: ['确认是否开封'], generated_at: '2026-10-28T12:00:00Z', cached: true,
    sources: [{ id: 1, title: '官方储存参考', url: 'https://www.foodsafety.gov/food-safety-charts' }], ...overrides } };
}

test('web advice validates the food identity, result status and reference scope', () => {
  assert.equal(storageWeb.storageGuidanceView({ food_id: 'f1', status: 'not_requested', guidance: null }, 'f1'), null);
  assert.throws(() => storageWeb.storageGuidanceView(webResult(), 'other'), /不匹配/);
  assert.throws(() => storageWeb.storageGuidanceView(webResult({ scope: 'expiry_rule' }), 'f1'));
  assert.throws(() => storageWeb.storageGuidanceView(webResult({ generated_at: 'bad' }), 'f1'));
  assert.throws(() => storageWeb.storageGuidanceView(webResult({ status: 'failed' }), 'f1'));
  assert.equal(storageWeb.storageGuidanceView(webResult({ status: 'no_reliable_sources', sources: [] }), 'f1').status, 'no_reliable_sources');
});

test('reference links reject scripts, HTTP, lookalike hosts and embedded credentials', () => {
  for (const url of ['javascript:alert(1)', 'http://foodsafety.gov/x', 'https://foodsafety.gov.evil.test/x', 'https://user:password@foodsafety.gov/x', 'https://localhost/x']) {
    assert.throws(() => storageWeb.storageGuidanceView(webResult({ sources: [{ id: 1, title: 'bad', url }] }), 'f1'), /来源/);
  }
  const result = webResult(); result.guidance.sources.push({ ...result.guidance.sources[0], id: 2 });
  assert.equal(storageWeb.storageGuidanceView(result, 'f1').sources.length, 1);
});

test('web reference projection cannot create deadline/progress/reminder fields or mutate its source', () => {
  const result = webResult({ expires_at: '2026-10-30T00:00:00Z', remind_at: '2026-10-29T00:00:00Z', percent: 20 });
  const before = JSON.stringify(result), view = storageWeb.storageGuidanceView(result, 'f1');
  for (const field of ['expires_at', 'remind_at', 'date', 'percent']) assert.equal(field in view, false);
  assert.equal(JSON.stringify(result), before);
  assert.equal(storageWeb.guidanceExpired(view, Date.parse(view.generatedAt) + 86400000 - 1), false);
  assert.equal(storageWeb.guidanceExpired(view, Date.parse(view.generatedAt) + 86400000), true);
});

test('changing any stored food condition invalidates the view key, while unrelated UI state does not', () => {
  const row = { ...partial, opened: false, temperature_c: 0 };
  for (const [key, value] of Object.entries({ name: 'other', food_class: 'fresh', storage: 'frozen', condition: 'cooked', opened: true, temperature_c: 4, date_type: 'purchase', started_at: '2026-10-28T12:00:00Z', status: 'used' })) {
    assert.notEqual(storageWeb.guidanceContext(row), storageWeb.guidanceContext({ ...row, [key]: value }));
  }
  assert.equal(storageWeb.guidanceContext(row), storageWeb.guidanceContext({ ...row, reminderDialogOpen: true }));
});

test('network uncertainty offers read recovery while deleted/inactive/auth failures stop queries', () => {
  const lost = new Error('lost'); lost.code = 'CONNECTION_LOST';
  assert.match(storageWeb.guidanceFailure(lost, true).message, /读取查询结果/);
  for (const code of ['NOT_FOUND', 'FOOD_INACTIVE', 'AUTH_REQUIRED']) {
    const error = new Error(code); error.code = code;
    assert.equal(storageWeb.guidanceFailure(error, false).terminal, true);
  }
  const changed = new Error('changed'); changed.code = 'STORAGE_CONTEXT_CHANGED';
  assert.match(storageWeb.guidanceFailure(changed, true).message, /条件已变化/);
});

test('web reference renders readable source links and conditions with safe escaping and no countdown', () => {
  const value = storageWeb.storageGuidanceView(webResult({ summary: '<script>alert(1)</script>参考说明' }), 'f1');
  const html = renderToStaticMarkup(React.createElement(StorageGuidanceContent, { value }));
  assert.match(html, /参考资料/); assert.match(html, /确认是否开封/);
  assert.match(html, /noopener noreferrer/); assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script|<progress|预计剩余|设置提醒/);
});

test('new chat card initially reads current advice instead of displaying a historical payload', () => {
  const html = renderToStaticMarkup(React.createElement(StorageGuidance, { foodId: 'f1' }));
  assert.match(html, /正在读取已保存的建议/);
  assert.doesNotMatch(html, /联网查建议|对照包装与冷藏条件/);
});

test('every active inventory tile has a storage advice entry without expanding the full editor', () => {
  const html = renderToStaticMarkup(React.createElement(FridgeInventory, { rows: [partial], onChanged: async () => {} }));
  assert.match(html, /名称先记的豆腐的储存建议/);
  assert.doesNotMatch(html, /记录现在的实际情况/);
});

const eatingPlan = { version: 'consumption-plan-2026-10-29.1', scope: 'eating_plan', safety_guarantee: false, status: 'ready',
  start_at: new Date(Date.now() - 3600000).toISOString(), target_at: new Date(Date.now() + 12 * 3600000).toISOString(),
  source: { title: '食用参考', url: 'https://www.hami.gov.cn/example' }, evidence_quote: '<script>测试来源</script>' };
test('inventory shows eating time and progress even when the formal expiry is unknown', () => {
  const html = renderToStaticMarkup(React.createElement(FridgeInventory, { rows: [{ ...partial, consumption_plan: eatingPlan }], onChanged: async () => {} }));
  assert.match(html, /约剩 12 小时/); assert.match(html, /前食用/); assert.match(html, /建议食用时间已过比例/);
  assert.doesNotMatch(html, /期限待核对|暂无适用期限|新鲜度百分比/);
  assert.doesNotMatch(html, /食用参考|<blockquote/); // Main tile stays compact.
});
test('plan evidence is collapsed and escaped; unsaved edits hide old countdowns', () => {
  const html = renderToStaticMarkup(React.createElement(ConsumptionPlan, { plan: eatingPlan }));
  assert.match(html, /<details[^>]*><summary>食用时间依据/); assert.doesNotMatch(html, /<details[^>]*open|<script/);
  assert.match(html, /&lt;script&gt;/); assert.match(html, /noopener noreferrer/);
  const stale = renderToStaticMarkup(React.createElement(ConsumptionPlan, { plan: eatingPlan, stale: true }));
  assert.match(stale, /条件已变化/); assert.doesNotMatch(stale, /约剩|<progress|食用参考/);
});

test('conservative inventory is visibly a schedule, with key reminders and sources only in details', () => {
  const plan = { ...eatingPlan, basis: 'conservative_schedule', date_origin: 'purchase', start_at: '2026-10-29T00:00:00+08:00',
    message: '资料没有提供保存天数', key_reminders: ['发生冷藏中断时先核对'] };
  const tile = renderToStaticMarkup(React.createElement(FridgeInventory, { rows: [{ ...partial, consumption_plan: plan }], onChanged: async () => {} }));
  assert.match(tile, /保守安排/); assert.match(tile, /安排目标/); assert.match(tile, /非保质期/);
  assert.doesNotMatch(tile, /发生冷藏中断|食用参考|<blockquote/);
  const detail = renderToStaticMarkup(React.createElement(ConsumptionPlan, { plan }));
  assert.match(detail, /关键提醒/); assert.match(detail, /发生冷藏中断/); assert.match(detail, /资料没有提供保存天数/);
  assert.doesNotMatch(detail, /购买日期[^<]*00:00/);
});
