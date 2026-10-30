import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/recipe-candidates.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { recipeCandidates, newCandidateEdit, candidateRecipeInput, savedCandidateRecipes, candidateExpired, candidateStatusLabel } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
const displaySource = readFileSync(new URL('../lib/recipe-display.ts', import.meta.url), 'utf8');
const displayJs = ts.transpileModule(displaySource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { recipeCategory, recipeCuisineQuery, recipeLines, recipeView, recipeViews, recipeVerdict } = await import('data:text/javascript;base64,' + Buffer.from(displayJs).toString('base64'));
const candidate = { candidate_id: 'c1', title: '鹰嘴豆炖菜', source_url: 'https://example.com/recipe', summary: '搜索摘要，无分量与做法', notice: '' };

test('summary never becomes ingredients or a tutorial, and no initial confirmation is assumed', () => {
  const edit = newCandidateEdit(candidate);
  assert.equal(edit.title, candidate.title);
  assert.equal(edit.ingredients, ''); assert.equal(edit.steps, ''); assert.equal(edit.confirmed, false);
  assert.throws(() => candidateRecipeInput(edit, candidate), /配料/);
});

test('requires both a real tutorial and an explicit confirmation before constructing the save request', () => {
  const edit = { ...newCandidateEdit(candidate), ingredients: '鹰嘴豆200克、番茄1个', steps: '番茄切块，与熟鹰嘴豆炖10分钟。' };
  assert.throws(() => candidateRecipeInput(edit, candidate), /勾选确认/);
  const body = candidateRecipeInput({ ...edit, confirmed: true, dessert: true }, candidate);
  assert.equal(body.confirmed, true); assert.equal(body.source_url, candidate.source_url);
  assert.equal(body.steps, edit.steps); assert.deepEqual(body.tags, ['dessert']);
});

test('rejects whitespace and invalid enum values instead of sending an invalid confirmed save', () => {
  const edit = { ...newCandidateEdit(candidate), ingredients: '豆腐', steps: '煮熟', confirmed: true };
  assert.throws(() => candidateRecipeInput({ ...edit, steps: '  ' }, candidate), /详细步骤/);
  assert.throws(() => candidateRecipeInput({ ...edit, meal: 'main' }, candidate), /具体餐次/);
  assert.throws(() => candidateRecipeInput({ ...edit, cuisine: 'invented' }, candidate), /有效菜系/);
});

test('restores only persisted personal records associated with the exact candidate', () => {
  const restored = savedCandidateRecipes([
    { id: 'r1', candidate_id: 'c1', title: '已保存个人菜谱' },
    { id: 'public1', candidate_id: 'c2', title: '公共来源', scope: 'public' },
    { id: 'r3', title: candidate.title },
    { candidate_id: 'c4', title: '没有持久化ID' }, null,
  ]);
  assert.deepEqual(Object.keys(restored), ['c1']);
  assert.equal(restored.c1.id, 'r1');
  assert.deepEqual(savedCandidateRecipes(null), {});
});

test('ignores malformed or duplicate candidates and never fabricates an id from the title', () => {
  const rows = recipeCandidates([candidate, candidate, { title: '无ID' }, null, { candidate_id: 'c2', title: 42 }]);
  assert.deepEqual(rows.map(row => row.candidate_id), ['c1', 'c2']);
  assert.equal(rows[1].title, '');
});

test('keeps server candidate metadata across HTTP and conversation shapes without inventing an expiry', () => {
  const rows = recipeCandidates([
    { id: 'http-1', title: '来源', status: 'pending_review', tutorial_complete: false, expires_at: '2030-01-01T00:00:00Z' },
    { candidate_id: 'chat-1', title: '对话来源', tutorial_complete: true, status: '已保存到个人菜谱' },
  ]);
  assert.equal(rows[0].candidate_id, 'http-1');
  assert.equal(rows[0].status, 'pending_review');
  assert.equal(rows[0].tutorial_complete, false);
  assert.equal(rows[0].expires_at, '2030-01-01T00:00:00Z');
  assert.equal(rows[1].expires_at, null);
  assert.equal(candidateStatusLabel(rows[1]), '来源标记已保存');
  assert.equal(candidateExpired(rows[0], Date.parse('2030-01-02T00:00:00Z')), true);
  assert.equal(candidateExpired(rows[1], Date.parse('2030-01-02T00:00:00Z')), false);
});

test('browsing keeps Chinese and Western exact; unspecified foreign recipes stay in all recipes', () => {
  assert.equal(recipeCategory('chinese'), 'chinese');
  assert.equal(recipeCategory('western'), 'western');
  for (const value of ['foreign', 'other', '', null, undefined, 'invented']) assert.equal(recipeCategory(value), 'unclassified');
  assert.equal(recipeCuisineQuery('western'), 'western');
  assert.equal(recipeCuisineQuery('foreign'), '');
  assert.equal(recipeCuisineQuery('unclassified'), '');
  const raw = { id: 'old', cuisine: 'other' };
  assert.equal(recipeView(raw).category, 'unclassified');
  assert.equal(raw.cuisine, 'other');
});

test('public arrays and legacy personal text render as separate ingredient and step lines', () => {
  assert.deepEqual(recipeLines(['豆腐 200克', null, '', '番茄 1个']), ['豆腐 200克', '番茄 1个']);
  const publicRecipe = recipeView({ entry_id: 'p1', scope: 'public', ingredients: ['豆腐 200克'], steps: ['切块', '煮熟'], servings: 2, source: 'https://example.org/recipe', nutrition_notes: '来源说明' });
  assert.deepEqual(publicRecipe.steps, ['切块', '煮熟']);
  assert.equal(publicRecipe.sourceUrl, 'https://example.org/recipe');
  assert.equal(publicRecipe.servings, 2);
  assert.equal(publicRecipe.scope, 'public');
  assert.equal(publicRecipe.nutritionLabel, '公共来源的搭配说明');
  const personal = recipeView({ id: 'r1', ingredients: '豆腐\n番茄', steps: '切块\n煮熟', nutrition_notes: '自己的笔记' });
  assert.deepEqual(personal.ingredients, ['豆腐', '番茄']);
  assert.equal(personal.tutorialComplete, false);
  assert.deepEqual(personal.tutorialMissing, ['份量或成品数量', '可追溯来源']);
  assert.match(personal.nutritionLabel, /个人.*未经营养审核/);
});

test('a backend summary marker or missing tutorial fields never becomes a complete tutorial', () => {
  const valid = { id: 'p1', scope: 'public', ingredients: ['豆腐 200克'], steps: ['煮熟'], servings: 2, source: 'https://example.org/recipe' };
  assert.equal(recipeView(valid).tutorialComplete, true);
  assert.equal(recipeView({ ...valid, tutorial_complete: false }).tutorialComplete, false);
  assert.equal(recipeView({ ...valid, tutorial_status: 'source_summary' }).tutorialComplete, false);
  assert.equal(recipeView({ ...valid, steps: [] }).tutorialComplete, false);
  assert.equal(recipeView({ ...valid, source: null }).tutorialComplete, false);
  assert.equal(recipeView({ ...valid, servings: null }).tutorialComplete, false);
  const yieldOnly = recipeView({ ...valid, servings: null, conditions: { yield_text: '2小盅' } });
  assert.equal(yieldOnly.servings, null);
  assert.equal(yieldOnly.tutorialComplete, true);
  assert.equal(recipeView({ ...valid, scope: 'personal', source: null }).tutorialComplete, false);
  assert.equal(recipeView({ ...valid, scope: 'personal', servings: null }).tutorialComplete, false);
  assert.equal(recipeView({ ...valid, source: 'javascript:alert(1)' }).tutorialComplete, false);
  assert.equal(recipeVerdict(recipeView(valid), 'vegan'), '待核实');
  assert.deepEqual(recipeViews([null, { title: '无稳定ID' }]), []);
});
