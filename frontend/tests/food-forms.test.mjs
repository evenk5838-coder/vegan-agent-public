import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/food-forms.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { editValues, draftPatches, draftCreateBody, reevaluationBody, foodConditionBody, unappliedFoodFields, foodEntryDraftBody, foodDraftReady, foodDraftSummary } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));

const item = {
  item_id: 'i1', name: '豆腐', storage: 'refrigerated', condition: 'unopened', date_type: 'purchase',
  started_at: '2026-10-24T08:00:00+08:00', expires_at: '2026-10-26T08:00:00+08:00',
  remind_at: '2026-10-25T08:00:00+08:00', temperature_c: 0, package_note: '包装说明',
  deadline_source: 'package', status: 'ready', missing: [],
};

test('unchanged fields are omitted so the server retains them', () => {
  assert.deepEqual(draftPatches(item, editValues(item)), {});
});

test('clearing optional draft values sends explicit null, including zero temperature', () => {
  const values = { ...editValues(item), temperature_c: '', remind_at: '', expires_at: '', package_note: '' };
  assert.deepEqual(draftPatches(item, values), { temperature_c: null, remind_at: null, expires_at: null, package_note: null });
});

test('an unknown temperature can be changed to zero without treating zero as missing', () => {
  const previous = { ...item, temperature_c: undefined };
  assert.deepEqual(draftPatches(previous, { ...editValues(previous), temperature_c: '0' }), { temperature_c: 0 });
});

test('only the food name is mandatory; clearing storage or date keeps unknown facts explicit', () => {
  assert.throws(() => draftPatches(item, { ...editValues(item), name: '' }), /不能清空/);
  assert.deepEqual(draftPatches(item, { ...editValues(item), storage: '', started_at: '', date_type: '' }),
    { storage: 'unknown', started_at: null, date_type: 'unknown' });
});

test('an unresolved relative date meaning stays unselected instead of defaulting to purchase', () => {
  assert.equal(editValues({ ...item, date_source: 'relative', date_type: undefined }).date_type, 'unknown');
});

const preview = {
  phrase: '昨天', started_at: '2026-10-23T00:00:00+08:00', resolved_date: '2026-10-23', time_precision: 'date',
  reference_at: '2026-10-24T23:59:00+08:00', timezone: 'Asia/Shanghai', warnings: ['未提供具体时刻'],
};

test('a batch keeps the preview reference and timezone even if submitted after midnight', () => {
  const body = draftCreateBody([' 豆腐 ', '西兰花', ''], ' 昨天 ', '', preview, '昨天');
  assert.deepEqual(body, {
    items: [{ name: '豆腐', date_phrase: '昨天' }, { name: '西兰花', date_phrase: '昨天' }],
    reference_time: preview.reference_at, timezone: 'Asia/Shanghai',
  });
  assert.equal('started_at' in body.items[0], false);
  assert.equal('date_type' in body.items[0], false);
});

test('an edited date phrase cannot reuse the previous preview', () => {
  assert.throws(() => draftCreateBody(['豆腐'], '今天', 'purchase', preview, '昨天'), /核对日期/);
  assert.throws(() => draftCreateBody(['豆腐'], '今天', 'purchase', null, ''), /核对日期/);
});

test('name-only batches do not invent dates and reject empty or oversized batches', () => {
  assert.deepEqual(draftCreateBody(['豆腐'], '', '', null, ''), { items: [{ name: '豆腐' }] });
  assert.throws(() => draftCreateBody([' '], '', '', null, ''), /至少/);
  assert.throws(() => draftCreateBody(Array(31).fill('豆腐'), '', '', null, ''), /30/);
});

const reviewValues = {
  storage: 'frozen', condition: 'opened', location: 'indoor', temperature: '',
  basis: 'unknown', deadline: '', note: '', reminder: '',
};

test('reevaluation clears the old reminder explicitly instead of sending an ignored null', () => {
  const body = reevaluationBody(item, reviewValues);
  assert.equal(body.temperature_c, null);
  assert.deepEqual(body.clear, ['remind_at', 'package_note']);
  assert.equal('remind_at' in body.evidence, false);
  assert.equal(body.evidence.expires_at, null);
});

test('a replacement reminder is sent with a timezone and is not cleared', () => {
  const body = reevaluationBody(item, { ...reviewValues, temperature: '-18', reminder: '2026-10-25T08:00', note: '新包装说明' });
  assert.equal(body.temperature_c, -18);
  assert.equal(body.clear, undefined);
  assert.match(body.evidence.remind_at, /Z$/);
});

test('invalid dates and nonnumeric temperatures are rejected before submission', () => {
  assert.throws(() => reevaluationBody(item, { ...reviewValues, temperature: '未知' }), /温度/);
  assert.throws(() => reevaluationBody(item, { ...reviewValues, reminder: 'not-a-date' }), /提醒/);
});

test('legacy opening state is separated without inventing processing state or writing a migration', () => {
  const legacy = { ...item, condition: 'opened', date_type: undefined };
  const values = editValues(legacy);
  assert.equal(values.opened, 'true');
  assert.equal(values.condition, 'unknown');
  assert.equal(values.food_class, 'unknown');
  assert.equal(values.date_type, 'unknown');
  assert.deepEqual(draftPatches(legacy, values), {});
});

test('processed and opened are independent and false remains a boolean', () => {
  const old = { ...item, condition: 'cooked', opened: true, food_class: 'cooked' };
  const values = { ...editValues(old), opened: 'false', food_class: 'packaged' };
  assert.deepEqual(draftPatches(old, values), { food_class: 'packaged', opened: false });
  assert.deepEqual(draftPatches(old, { ...editValues(old), opened: '' }), { opened: null });
});

test('a successful status with ignored new fields is not treated as an applied change', () => {
  assert.deepEqual(unappliedFoodFields({ opened: false, food_class: 'packaged' }, { opened: null }), ['是否开封', '食物类别']);
  assert.deepEqual(unappliedFoodFields({ temperature_c: null, opened: false }, { temperature_c: null, opened: false }), []);
  assert.deepEqual(unappliedFoodFields({ started_at: '2026-10-26T00:00:00Z' }, { started_at: '2026-10-26T08:00:00+08:00' }), []);
});

test('condition update preserves source dates and sends new conditions without inventing a deadline', () => {
  const old = { ...item, status: 'active', opened: false, food_class: 'packaged' };
  const values = { ...editValues(old), condition: 'cooked', opened: 'true', temperature: '0' };
  const body = foodConditionBody(old, values);
  assert.equal(body.opened, true);
  assert.equal(body.food_class, 'packaged');
  assert.equal(body.condition, 'cooked');
  assert.equal(body.temperature_c, 0);
  assert.equal(body.started_at, old.started_at);
  assert.equal(body.expires_at, old.expires_at);
  assert.equal(body.deadline_source, old.deadline_source);
  assert.equal(foodConditionBody(old, { ...values, condition: '' }).condition, 'unknown');
  assert.equal(foodConditionBody(old, { ...values, date_type: '' }).date_type, 'unknown');
});

test('single entry creates a name-only draft with no invented state or dates', () => {
  assert.deepEqual(foodEntryDraftBody({ name: ' 豆腐 ' }), { items: [{ name: '豆腐' }] });
  assert.throws(() => foodEntryDraftBody({ name: ' ' }), /名称/);
});

test('single-entry draft preserves independent food class, processing and unopened false', () => {
  const body = foodEntryDraftBody({ name: '豆奶', food_class: 'packaged', opened: 'false', condition: 'cooked', storage: 'refrigerated', temperature_c: '0' });
  assert.deepEqual(body, { items: [{ name: '豆奶', storage: 'refrigerated', condition: 'cooked', food_class: 'packaged', opened: false, temperature_c: 0 }] });
  assert.equal('started_at' in body.items[0], false);
  assert.equal('expires_at' in body.items[0], false);
});

test('a known date can be kept without inventing its meaning and uses the device timezone', () => {
  const unknownMeaning = foodEntryDraftBody({ name: '豆腐', started_at: '2026-10-26T08:00' });
  assert.equal(unknownMeaning.items[0].started_at, new Date('2026-10-26T08:00').toISOString());
  assert.equal('date_type' in unknownMeaning.items[0], false);
  const body = foodEntryDraftBody({ name: '豆腐', started_at: '2026-10-26T08:00', date_type: 'purchase' });
  assert.equal(body.items[0].started_at, new Date('2026-10-26T08:00').toISOString());
  assert.equal(body.items[0].date_type, 'purchase');
});

test('name-only review is ready to save while optional unknowns stay unknown', () => {
  const partial = { item_id: 'partial', name: '豆腐', storage: 'unknown', condition: 'unknown', date_type: 'unknown', started_at: null, missing: ['storage', 'started_at'] };
  assert.equal(foodDraftReady(partial), true);
  assert.equal(foodDraftReady({ name: ' ' }), false);
  const values = editValues(partial);
  assert.equal(values.storage, 'unknown');
  assert.equal(values.condition, 'unknown');
  assert.equal(values.date_type, 'unknown');
  assert.equal(values.started_at, '');
  assert.deepEqual(draftPatches(partial, values), {});
  assert.equal(foodDraftSummary(partial), '存放方式待补 · 日期可后补');
  assert.equal(foodDraftSummary({ ...partial, started_at: 'not-a-date' }), '存放方式待补 · 日期可后补');
});

test('editing or reevaluating a partial food does not supply a purchase date, refrigerator or raw state', () => {
  const partial = { name: '豆腐', started_at: null, status: 'active', expires_at: null, remind_at: null, package_note: '' };
  const updated = foodConditionBody(partial, { storage: '', condition: '', date_type: '', food_class: 'unknown', opened: '', location: 'unknown', temperature: '' });
  assert.equal(updated.started_at, null);
  assert.equal(updated.storage, 'unknown');
  assert.equal(updated.condition, 'unknown');
  assert.equal(updated.date_type, 'unknown');
  assert.equal(updated.expires_at, null);
  const rechecked = reevaluationBody({ ...partial, temperature_c: null }, { ...reviewValues, storage: 'unknown', condition: 'unknown' });
  assert.equal('storage' in rechecked, false);
  assert.equal('condition' in rechecked, false);
});

test('single-entry expiry cannot silently become a system estimate', () => {
  assert.throws(() => foodEntryDraftBody({ name: '豆腐', expires_at: '2026-10-28T08:00' }), /选择包装依据/);
  const body = foodEntryDraftBody({ name: '豆腐', expires_at: '2026-10-28T08:00', deadline_source: 'user' });
  assert.equal(body.items[0].deadline_source, 'user');
  assert.equal('suggestion' in body.items[0], false);
  assert.equal('advice_digest' in body.items[0], false);
});
