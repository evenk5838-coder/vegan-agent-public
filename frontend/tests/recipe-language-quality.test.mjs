import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL('../', import.meta.url));
const cache = new Map();
function load(file) {
  const path = resolve(root, file);
  if (cache.has(path)) return cache.get(path).exports;
  const module = { exports: {} };
  cache.set(path, module);
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
const { recipeView, recipeVerdict } = load('lib/recipe-display.ts');
const { recipeCandidates, candidateStatusLabel, candidateReviewNotice } = load('lib/recipe-candidates.ts');
const RecipeDetail = load('components/RecipeDetail.tsx').default;
const RecipeDietTags = load('components/RecipeDetail.tsx').RecipeDietTags;
const base = { entry_id: 'public-1', scope: 'public', origin: 'public_knowledge', title: '燕麦粥',
  ingredients: ['燕麦 100克', '水 500毫升'], steps: ['煮10分钟。'], source: 'https://example.org/recipe',
  servings_text: '约2人份', display_language: 'zh-CN', notes: ['不要省略浸泡步骤。'] };
const render = row => renderToStaticMarkup(React.createElement(RecipeDetail, { recipe: recipeView(row), diet: 'vegan' }));

test('three dietary tags retain independent overlapping results rather than one exclusive category', () => {
  const recipe = recipeView({ ...base, evaluation: { results: {
    pure: { status: 'not_suitable', denied: ['洋葱'] },
    vegan: { status: 'suitable', denied: [] }, lacto_ovo: { status: 'suitable', denied: [] },
  } } });
  const html = renderToStaticMarkup(React.createElement(RecipeDietTags, { recipe }));
  assert.match(html, /纯净素：不符合/);
  assert.match(html, /Vegan：按所示配料符合/);
  assert.match(html, /蛋奶素：按所示配料符合/);
  assert.equal((html.match(/<span /g) || []).length, 3);
});

test('ingredients, quantities and steps precede supplementary review detail; source status remains available', () => {
  const html = render({ ...base, quality_flags: ['machine_translation_unreviewed'],
    translation: { status: 'complete' }, source_meta: { manual_reviewed: false } });
  assert.ok(html.indexOf('原材料与用量') < html.indexOf('制作步骤'));
  assert.ok(html.indexOf('制作步骤') < html.indexOf('补充信息与来源核对'));
  assert.ok(html.indexOf('制作步骤') < html.indexOf('机器译文'));
  assert.match(html, /尚未经过人工复核/);
});

test('Chinese recipe quantities, servings and complete notes remain visible without invented numeric servings', () => {
  const recipe = recipeView(base);
  assert.equal(recipe.servings, null);
  assert.equal(recipe.servingsText, '约2人份');
  assert.equal(recipe.tutorialComplete, true);
  const html = render(base);
  assert.match(html, /约2人份/);
  assert.match(html, /燕麦 100克/);
  assert.match(html, /注意事项/);
  assert.match(html, /不要省略浸泡步骤/);
});

test('a declared completed Chinese translation still displays machine-review status and preserves the original', () => {
  const row = { ...base, source_language: 'en', source_complete_unreviewed: true,
    body: '来源完整正文中的额外说明。', review_sections: [{ heading: '替代方案', kind: 'stop', text: '可用水替代，份量仍按原文。' }],
    source_meta: { translation_quality_flags: ['machine_translation_unreviewed'], original_content: {
      title: 'Oatmeal', ingredients: ['100g oats', '500ml water'], steps: ['Cook for 10 minutes.'],
      servings_text: 'about 2 servings', body: 'The complete original body.', notes: ['Soak first.'],
      review_sections: [{ heading: 'Variations', kind: 'stop', text: 'Use water.' }] } },
    translation: { status: 'complete', validation: { semantic_quality_manually_reviewed: false } } };
  const recipe = recipeView(row);
  assert.equal(recipe.languageState, 'chinese');
  assert.equal(recipe.tutorialComplete, true); // Structure and review status are separate.
  assert.equal(recipe.tutorialLabel, '内容齐备 · 待复核');
  assert.equal(recipe.machineTranslationUnreviewed, true);
  const html = render(row);
  assert.match(html, /中文机器译文/);
  assert.match(html, /未经过人工复核/);
  assert.match(html, /来源完整正文中的额外说明/);
  assert.match(html, /替代方案/);
  assert.match(html, /查看来源原文，核对译文/);
  assert.match(html, /100g oats/);
  assert.match(html, /about 2 servings/);
  assert.match(html, /The complete original body/);
});

test('English legacy content cannot be called fully Chinese based only on language or complete flags', () => {
  const row = { ...base, title: 'Oatmeal', ingredients: ['100g oats'], steps: ['Cook for 10 minutes.'],
    servings_text: '2 servings', tutorial_complete: true, translation: { status: 'complete' } };
  const recipe = recipeView(row);
  assert.equal(recipe.languageState, 'pending');
  assert.equal(recipe.tutorialComplete, false);
  assert.equal(recipe.tutorialLabel, '中文内容待补齐');
  const html = render(row);
  assert.match(html, /中文翻译待完成或待核对/);
  assert.match(html, /100g oats/); // Existing content is retained for checking.
  assert.match(html, /Cook for 10 minutes/);
});

test('unknown language, missing servings and missing source remain explicit without fabricated defaults', () => {
  const unknown = recipeView({ id: 'empty' });
  assert.equal(unknown.languageState, 'unknown');
  assert.equal(unknown.tutorialComplete, false);
  const recipe = recipeView({ ...base, servings_text: '', servings: null });
  assert.equal(recipe.servings, null);
  assert.equal(recipe.servingsText, '');
  assert.equal(recipe.yieldText, '');
  assert.equal(recipe.tutorialComplete, false);
  assert.ok(recipe.tutorialMissing.includes('份量或成品数量'));
  assert.match(render({ ...base, servings_text: '' }), /未注明，未自动估算/);
});

test('nonempty ingredients and steps do not overcome parser gaps or unreconciled source notes', () => {
  for (const flag of ['unparsed_table', 'ingredient_inventory_and_step_crosscheck_not_done',
    'source_paragraphs_not_verified_as_step_sequence', 'unparsed_template:nutritionsummary', 'unknown_parser_issue']) {
    const row = { ...base, quality_flags: [flag], tutorial_complete: true };
    assert.equal(recipeView(row).tutorialComplete, false, flag);
    assert.ok(recipeView(row).qualityMessages.length > 0);
    assert.match(render(row), /尚未完成/);
  }
});

test('informational source normalization does not erase the real tutorial or independent dietary verdict', () => {
  const row = { ...base, quality_flags: ['html_whitespace_normalized_and_printed_page_markers_removed'],
    evaluation: { results: { vegan: { status: 'not_suitable', denied: ['牛奶'], unknown: ['复合配料'] } } } };
  const recipe = recipeView(row);
  assert.equal(recipe.tutorialComplete, true);
  assert.equal(recipeVerdict(recipe, 'vegan'), '不符合');
  const html = render(row);
  assert.match(html, /Vegan：不符合/);
  assert.match(html, /命中的不适配配料：牛奶/);
  assert.match(html, /仍需核对的配料：复合配料/);
  assert.match(html, /译文或教程齐备不代表饮食适配/);
});

test('parser or translation problems never suppress a real animal verdict', () => {
  const row = { ...base, ingredients: ['beef and potato'], quality_flags: ['unparsed_table'],
    evaluation: { results: { vegan: { status: 'not_suitable', denied: ['牛肉'], unknown: [] } } } };
  assert.equal(recipeView(row).tutorialComplete, false);
  assert.match(render(row), /命中的不适配配料：牛肉/);
});

test('only explicit review evidence clears a completed machine-translation notice', () => {
  const row = { ...base, translation: { status: 'complete' }, source_meta: { manual_reviewed: true } };
  assert.equal(recipeView(row).machineTranslationUnreviewed, false);
  const stillFlagged = { ...row, source_meta: { manual_reviewed: true, translation_quality_flags: ['machine_translation_unreviewed'] } };
  assert.equal(recipeView(stillFlagged).machineTranslationUnreviewed, true);
});

test('translated output does not inherit a stale original-English flag as a new untranslated claim', () => {
  const row = { ...base, source_language: 'en', quality_flags: ['original_english_not_translated'],
    translation: { status: 'complete', validation: { semantic_quality_manually_reviewed: true } } };
  assert.equal(recipeView(row).languageState, 'chinese');
  assert.doesNotMatch(render(row), /中文翻译尚未完成/);
});

test('source body, notes and original audit content render as text with safe links only', () => {
  const row = { ...base, body: '<script>alert("body")</script>', notes: ['<img src=x onerror=alert(1)>'],
    source: 'javascript:alert(1)', source_meta: { original_content: { title: '<svg onload=alert(1)>',
      ingredients: ['<script>alert(2)</script>'] } } };
  const html = render(row);
  assert.doesNotMatch(html, /<script|<img|<svg|href="javascript:/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;svg/);
  assert.match(html, /未提供可打开的来源链接/);
});

test('candidate source quality and translation flags survive HTTP normalization and change review wording', () => {
  const [candidate] = recipeCandidates([{ id: 'c1', title: '燕麦粥', tutorial_complete: true, display_language: 'zh-CN',
    source_meta: { language: 'en', quality_flags: ['unparsed_table'], translation_quality_flags: ['machine_translation_unreviewed'],
      translation: { status: 'complete' } } }]);
  assert.deepEqual(candidate.quality_flags, ['unparsed_table']);
  assert.deepEqual(candidate.translation_quality_flags, ['machine_translation_unreviewed']);
  assert.match(candidateStatusLabel(candidate), /机器译文未复核/);
  assert.match(candidateReviewNotice(candidate), /份数、步骤与注意事项/);
  const [english] = recipeCandidates([{ id: 'c2', title: 'Oatmeal', tutorial_complete: true }]);
  assert.match(candidateStatusLabel(english), /中文翻译待完成/);
});
