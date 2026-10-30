import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

async function load(name) {
  const source = readFileSync(new URL('../lib/' + name + '.ts', import.meta.url), 'utf8');
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  return import('data:text/javascript;base64,' + Buffer.from(output).toString('base64'));
}
const { personalRecipeRecord, loadPersonalRecipeRecords, readPersonalRecipe, recipeEditFromRecord, recipeEditInput, recipeSnapshot, savedRecipeMatches, recipeRecordVersion, recipeUpdateRequest, prepareRecipeCreate, recipeCreateRequest, recipeCreateResult } = await load('recipe-edit');
const { recipeView, recipeVerdict } = await load('recipe-display');
const { recoveredRecipeCandidates, candidateRecipeInput, newCandidateEdit } = await load('recipe-candidates');
const record = { id: 'r1', title: '豆腐汤', ingredients: '豆腐 200克\n水 500毫升', steps: '切块\n煮熟', meal: 'main', purpose: 'everyday', cuisine: 'other', origin: 'user_original', source_url: '', nutrition_notes: '自己的笔记', yield_amount: 2, yield_unit: '人份', tags: [] };

test('only persisted personal records can enter editing; missing provenance never becomes original', () => {
  for (const value of [null, { title: '无ID' }, { ...record, scope: 'public' }, { ...record, origin: 'public_knowledge' }]) {
    assert.equal(personalRecipeRecord(value), null);
    assert.throws(() => recipeEditFromRecord(value), /本人/);
  }
  const edit = recipeEditFromRecord({ ...record, origin: undefined });
  assert.equal(edit.origin, '');
  assert.equal(edit.confirmed, false);
  assert.throws(() => recipeEditInput({ ...edit, confirmed: true }), /明确记录菜谱来源/);
});

test('editing preserves legacy meal and classification, explicit provenance and quantity without recreating the record', () => {
  const edit = recipeEditFromRecord(record);
  assert.equal(edit.meal, 'main'); assert.equal(edit.cuisine, 'other');
  assert.equal(edit.yield_amount, '2'); assert.equal(edit.yield_unit, '人份');
  assert.throws(() => recipeEditInput(edit), /勾选确认/);
  const body = recipeEditInput({ ...edit, confirmed: true });
  assert.equal(body.origin, 'user_original'); assert.equal(body.source_url, '');
  assert.equal(body.meal, 'main'); assert.equal(body.yield_amount, 2);
  assert.equal(savedRecipeMatches({ ...record, ...body }, 'r1', body), true);
  assert.equal(savedRecipeMatches({ ...record, ...body, id: 'r2' }, 'r1', body), false);
});

test('an edited external candidate keeps external attribution and cannot drop its source silently', () => {
  const edit = recipeEditFromRecord({ ...record, origin: 'web_confirmed_by_user', source_url: 'https://example.org/recipe' });
  assert.equal(edit.origin, 'external');
  assert.equal(recipeEditInput({ ...edit, confirmed: true }).origin, 'external');
  assert.throws(() => recipeEditInput({ ...edit, source_url: '', confirmed: true }), /需要填写来源/);
  assert.throws(() => recipeEditInput({ ...edit, source_url: 'javascript:alert(1)', confirmed: true }), /HTTPS/);
});

test('editing unspecified foreign cuisine never silently changes it to Western', () => {
  const edit = recipeEditFromRecord({ ...record, cuisine: 'foreign' });
  assert.equal(edit.cuisine, 'foreign');
  assert.equal(recipeEditInput({ ...edit, confirmed: true }).cuisine, 'foreign');
  assert.notEqual(recipeView({ ...record, cuisine: 'foreign' }).category, 'western');
});

test('editable snapshots detect changed instructions, quantity and provenance while ignoring derived evaluations', () => {
  const snapshot = recipeSnapshot(record);
  for (const change of [{ steps: '改成煎熟' }, { origin: 'external' }, { yield_amount: 3 }, { source_url: 'https://example.org/new' }]) assert.notEqual(recipeSnapshot({ ...record, ...change }), snapshot);
  assert.equal(recipeSnapshot({ ...record, evaluation: { results: {} }, created: 'different' }), snapshot);
  assert.equal(recipeSnapshot({ ...record, ingredients: record.ingredients.split('\n') }), snapshot);
});

test('unsupported labels, partial quantities and invalid values block a full-replacement update', () => {
  const edit = { ...recipeEditFromRecord(record), confirmed: true };
  assert.throws(() => recipeEditInput({ ...edit, unsupportedTags: ['unknown'] }), /未支持的标签/);
  assert.throws(() => recipeEditInput({ ...edit, yield_unit: '' }), /同时填写/);
  assert.throws(() => recipeEditInput({ ...edit, yield_amount: 'NaN' }), /0至100/);
  assert.throws(() => recipeEditInput({ ...edit, cuisine: 'invented' }), /分类/);
  assert.equal(recipeEditInput({ ...edit, yield_amount: '0' }).yield_amount, 0);
});

test('saved-candidate recovery reads all personal pages and stops before the public library', async () => {
  const calls = [];
  const personal = Array.from({ length: 51 }, (_, index) => ({ ...record, id: 'r' + index, candidate_id: 'c' + index }));
  const request = async (path, init) => {
    calls.push(path); assert.equal(init.method, undefined, 'status recovery is GET only');
    return calls.length === 1 ? { items: personal.slice(0, 50), personal_count: 51, has_more: true, next_page: 2 }
      : { items: [personal[50], { id: 'public1', scope: 'public' }], personal_count: 51, has_more: true, next_page: 3 };
  };
  const rows = await loadPersonalRecipeRecords(request);
  assert.equal(rows.length, 51); assert.equal(rows[50].candidate_id, 'c50');
  assert.deepEqual(calls, ['/recipes?page=1&page_size=50', '/recipes?page=2&page_size=50']);
});

test('personal detail uses one owner-only GET, preserves abort and never scans or writes', async () => {
  const calls = [];
  const controller = new AbortController();
  const row = await readPersonalRecipe(async (path, init) => {
    calls.push(path); assert.equal(init.method, undefined); assert.equal(init.signal, controller.signal);
    return { ...record, id: 'r/1', version: 'server-version' };
  }, 'r/1', controller.signal);
  assert.equal(row.id, 'r/1');
  assert.deepEqual(calls, ['/recipes/r%2F1']);
});

test('personal detail distinguishes deleted, forbidden, failed and mismatched responses', async () => {
  assert.equal(await readPersonalRecipe(async () => { throw { status: 404 }; }, 'r1'), null);
  for (const status of [401, 403, 500]) await assert.rejects(readPersonalRecipe(async () => { throw { status }; }, 'r1'), reason => reason.status === status);
  for (const data of [{ ...record, id: 'other' }, { ...record, scope: 'public' }, { items: [record] }, null]) {
    await assert.rejects(readPersonalRecipe(async () => data, 'r1'), /详情不一致/);
  }
});

test('updates require the displayed opaque version and send it without replacing the content', () => {
  const body = recipeEditInput({ ...recipeEditFromRecord(record), confirmed: true });
  for (const value of [record, { ...record, version: '' }, { ...record, version: '*'}, { ...record, version: 'W/"old"' }]) {
    assert.equal(recipeRecordVersion(value), null);
    assert.throws(() => recipeUpdateRequest(value, body), /版本/);
  }
  const baseline = { ...record, version: 'actual-v12' };
  const request = recipeUpdateRequest(baseline, body);
  assert.equal(request.method, 'PUT'); assert.deepEqual(request.headers, { 'If-Match': 'actual-v12' });
  assert.deepEqual(JSON.parse(request.body), body);
  assert.equal(recipeRecordVersion({ ...baseline, scope: 'public' }), null);
});

test('unknown create results reuse the same body key, while changed input gets a different key', () => {
  const attempts = [];
  let number = 0;
  const makeKey = () => 'logical-save-' + ++number;
  const body = recipeEditInput({ ...recipeEditFromRecord(record), confirmed: true });
  const first = prepareRecipeCreate(attempts, body, makeKey);
  first.outcome = 'uncertain';
  const again = prepareRecipeCreate(attempts, { ...body, tags: [...body.tags] }, makeKey);
  assert.equal(again, first);
  assert.deepEqual(recipeCreateRequest(first), recipeCreateRequest(again));
  assert.equal(recipeCreateRequest(first).headers['Idempotency-Key'], 'logical-save-1');
  const changed = prepareRecipeCreate(attempts, { ...body, title: '另一道菜' }, makeKey);
  assert.notEqual(changed.key, first.key);
  assert.equal(prepareRecipeCreate(attempts, body, makeKey).key, first.key, 'returning to an earlier uncertain body retains its key');
  assert.equal(number, 2);
});

test('confirmed create attempts retain an immutable submitted body and a deleted attempt cannot resurrect', () => {
  const body = recipeEditInput({ ...recipeEditFromRecord(record), dessert: true, confirmed: true });
  const attempts = [];
  const first = prepareRecipeCreate(attempts, body, () => 'fixed');
  body.title = '后来改的内容'; body.tags.push('other');
  const request = recipeCreateRequest(first);
  assert.equal(JSON.parse(request.body).title, record.title);
  assert.deepEqual(JSON.parse(request.body).tags, ['dessert']);
  first.outcome = 'deleted';
  assert.throws(() => recipeCreateRequest(first), /已删除/);
  assert.throws(() => prepareRecipeCreate(attempts, first.body, () => 'must-not-be-used'), /已删除/);
  assert.equal(attempts.length, 1);
});

test('a create replay acknowledges a later edit without recreating or overwriting it', () => {
  const input = recipeEditInput({ ...recipeEditFromRecord(record), confirmed: true });
  const saved = { ...record, ...input, version: 'created-v1' };
  assert.equal(recipeCreateResult(saved, input).changedSinceSave, false);
  assert.equal(recipeCreateResult({ ...saved, title: '其他位置更新' }, input), null);
  assert.equal(recipeCreateResult({ ...saved, title: '其他位置更新', replayed: true }, input).changedSinceSave, true);
  assert.equal(recipeCreateResult({ ...saved, version: undefined }, input), null);
  assert.equal(recipeCreateResult({ ...saved, scope: 'public', replayed: true }, input), null);
});

test('malformed or incomplete paging cannot claim that unseen candidates were not saved', async () => {
  await assert.rejects(loadPersonalRecipeRecords(async () => ({ items: [], personal_count: 2, has_more: false })), /未完整/);
  await assert.rejects(loadPersonalRecipeRecords(async () => ({ items: [record], personal_count: 2, has_more: true, next_page: 1 })), /分页状态不一致/);
});

test('persisted candidate identity wins over title matches and saved records leave the unconfirmed inbox', () => {
  const current = [{ candidate_id: 'c1', title: '同名', source_url: 'https://example.org' }, { candidate_id: 'c2', title: '同名' }];
  const rows = recoveredRecipeCandidates([{ candidate_id: 'c1', title: '服务端最新标题', saved_recipe_id: 'r1', status: '已保存到个人菜谱' }, { candidate_id: 'c3', title: '跨设备候选' }], current);
  assert.deepEqual(rows.map(row => row.candidate_id), ['c2', 'c3']);
});

test('explicit original provenance and real quantities affect tutorial structure, never dietary verdicts or review status', () => {
  const original = recipeView(record);
  assert.equal(original.tutorialComplete, true); assert.equal(original.yieldText, '2 人份');
  assert.equal(recipeVerdict(original, 'vegan'), '待核实');
  assert.equal(recipeView({ ...record, origin: undefined }).tutorialComplete, false);
  assert.equal(recipeView({ ...record, scope: 'public', source: '', source_url: '' }).tutorialComplete, false);
  assert.equal(recipeView({ ...record, yield_amount: null }).tutorialComplete, false);
  assert.equal(recipeView({ ...record, tutorial_complete: false }).tutorialComplete, false);
});

test('candidate quantity remains absent until entered and the exact source is retained', () => {
  const candidate = { candidate_id: 'c', title: '原文', source_url: 'https://example.org/original' };
  const edit = { ...newCandidateEdit(candidate), ingredients: '豆腐200克', steps: '煮熟', confirmed: true };
  const body = candidateRecipeInput(edit, candidate);
  assert.equal(body.yield_amount, null); assert.equal(body.yield_unit, ''); assert.equal(body.origin, 'external');
  assert.equal(candidateRecipeInput({ ...edit, yield_amount: '3', yield_unit: '碗' }, candidate).yield_amount, 3);
  assert.throws(() => candidateRecipeInput({ ...edit, yield_amount: '3' }, candidate), /同时填写/);
});
