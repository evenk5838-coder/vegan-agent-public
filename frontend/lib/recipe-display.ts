export type RecipeCategory = 'chinese' | 'western' | 'unclassified';
export type RecipeSourceContent = {
  title: string; ingredients: string[]; steps: string[]; body: string;
  servingsText: string; notes: string[]; reviewSections: { heading: string; text: string }[];
};

export type RecipeView = {
  id: string;
  title: string;
  scope: 'public' | 'personal';
  origin: string;
  category: RecipeCategory;
  meal: string;
  tags: string[];
  ingredients: string[];
  steps: string[];
  servings: number | null;
  yieldText: string;
  servingsText: string;
  sourceUrl: string;
  sourceAttribution?: string;
  licenseName?: string;
  licenseUrl?: string;
  body: string;
  tutorialComplete: boolean;
  tutorialMissing: string[];
  tutorialLabel: string;
  notes: string[];
  reviewSections: { heading: string; text: string }[];
  qualityFlags: string[];
  qualityMessages: string[];
  sourceCompleteUnreviewed: boolean;
  languageState: 'chinese' | 'pending' | 'unknown';
  languageNotice: string;
  machineTranslationUnreviewed: boolean;
  originalContent: RecipeSourceContent | null;
  nutritionNotes: string;
  nutritionLabel: string;
  notice: string;
  evaluation: Record<string, unknown> | null;
};

const text = (value: unknown) => typeof value === 'string' ? value.trim() : '';
const scalarText = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? String(value) : text(value);
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : {};
const unique = (values: string[]) => [...new Set(values.filter(Boolean))];

export function recipeLines(value: unknown): string[] {
  return (Array.isArray(value) ? value : typeof value === 'string' ? value.split(/\r?\n/) : [])
    .filter((item): item is string => typeof item === 'string')
    .map(item => item.trim()).filter(Boolean);
}

function reviewSections(value: unknown): { heading: string; text: string }[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    const row = object(item), body = text(row.text);
    return body ? [{ heading: text(row.heading), text: body }] : [];
  });
}

function readableChinese(value: string): boolean {
  if (!value) return true;
  // This is a conservative display check, never a translation or certification.
  // Unit letters and the established Vegan name can appear beside Chinese.
  const remainder = value.replace(/(?<![a-z])(?:vegan|kg|mg|g|ml|cl|dl|l|cm|mm|kcal|kj|°?c|°?f)(?![a-z])/gi, '');
  if (!remainder.replace(/[\d\s\p{P}\p{S}]+/gu, '')) return true;
  return /[\u3400-\u9fff]/.test(value) && !/[a-z]{3,}/i.test(remainder);
}

const qualityNames: Record<string, string> = {
  missing_ingredients: '来源缺少独立的食材与用量清单。',
  missing_steps: '来源缺少做法步骤。', missing_servings: '来源未注明具体份量。',
  source_incomplete: '来源内容不完整。', unparsed_table: '来源表格尚未完整解析。',
  recipe_anchor_not_found: '来源的菜谱定位尚未核对。',
  no_separately_marked_ingredient_list_in_source: '来源没有单独列出食材，不能把正文当作已核对的配料表。',
  source_paragraphs_not_verified_as_step_sequence: '正文段落尚未核实为正确的操作顺序。',
  ingredient_inventory_and_step_crosscheck_not_done: '食材清单与做法步骤尚未逐项交叉核对。',
  compound_ingredients_and_cross_references_not_expanded: '复合配料与交叉引用尚未补全。',
  source_contains_internal_cross_references: '来源引用了其他段落，仍需结合原文核对。',
  short_source_entry_requires_context_review: '来源条目较短，仍需核对上下文。',
  conversion_not_calculated: '来源中的数量计算尚未核对，未自动换算或补齐用量。',
  historical_units_not_converted: '历史用量单位按来源保留，未自动换算。',
  historical_source_requires_modern_recipe_review: '历史来源仍需核对现代烹饪条件。',
  modern_food_safety_not_reviewed: '来源尚未经过现代食品安全复核。',
  dietary_suitability_not_verified: '来源的素食声明不能代替本应用的饮食规则判断。',
  source_paragraphs_include_recipe_notes: '来源正文同时包含做法与注意事项，请完整查看。',
  source_uses_real_chapter_anchor_not_recipe_specific_anchor: '来源链接定位到章节，未精确定位到单道菜。',
  machine_translation_unreviewed: '机器翻译尚未经过人工复核，请核对原文。',
  source_parser_artifact_unreviewed: '来源混入了目录或互链等解析杂项，仍待核对。',
  source_nonvegetarian_pairing_unreviewed: '来源含有非素食搭配建议，仍待核对。',
};
const informationalQuality = new Set(['html_whitespace_normalized_and_printed_page_markers_removed',
  'dietary_suitability_not_verified', 'modern_food_safety_not_reviewed',
  'historical_source_requires_modern_recipe_review', 'source_paragraphs_include_recipe_notes',
  'machine_translation_unreviewed', 'original_english_not_translated']);

function qualityMessage(flag: string): string {
  if (qualityNames[flag]) return qualityNames[flag];
  if (flag.startsWith('unparsed_template:')) return '来源模板尚未完整解析，部分内容需要核对原文。';
  if (/[\u3400-\u9fff]/.test(flag)) return flag;
  return '来源仍有待核对的解析记录。';
}

/** Read-only compatibility: old records retain their original stored cuisine. */
export function recipeCategory(cuisine: unknown): RecipeCategory {
  if (cuisine === 'chinese') return 'chinese';
  if (cuisine === 'western') return 'western';
  return 'unclassified';
}

export const recipeCategoryNames: Record<RecipeCategory, string> = {
  chinese: '中式', western: '西式', unclassified: '待补分类',
};

export function recipeCuisineQuery(category: string): string {
  // Only source-backed classifications become a filter; foreign is not a synonym for western.
  return category === 'chinese' || category === 'western' ? category : '';
}

export function recipeView(value: unknown): RecipeView | null {
  if (!value || typeof value !== 'object') return null;
  const row = value as Record<string, unknown>;
  const id = text(row.id) || text(row.entry_id);
  if (!id) return null;
  const scope = row.scope === 'public' || row.origin === 'public_knowledge' ? 'public' : 'personal';
  const conditions = row.conditions && typeof row.conditions === 'object' ? row.conditions as Record<string, unknown> : {};
  const sourceMeta = object(row.source_meta), translation = object(row.translation || sourceMeta.translation);
  const license = object(row.license || sourceMeta.license);
  const ingredients = recipeLines(row.ingredients), steps = recipeLines(row.steps);
  const notes = recipeLines(row.notes), sections = reviewSections(row.review_sections);
  const sourceUrl = text(row.source_url) || text(row.source);
  const servings = typeof row.servings === 'number' && Number.isInteger(row.servings) && row.servings > 0 ? row.servings : null;
  const yieldAmount = typeof row.yield_amount === 'number' && Number.isFinite(row.yield_amount) && row.yield_amount > 0 ? row.yield_amount : null;
  const yieldUnit = text(row.yield_unit);
  const yieldText = text(conditions.yield_text) || (yieldAmount !== null && yieldUnit ? `${yieldAmount} ${yieldUnit}` : '');
  const servingsText = text(row.servings_text) || text(conditions.servings_text);
  const qualityFlags = unique([...recipeLines(row.quality_flags), ...recipeLines(sourceMeta.quality_flags),
    ...recipeLines(row.translation_quality_flags), ...recipeLines(sourceMeta.translation_quality_flags), ...recipeLines(translation.flags)]);
  const translated = text(translation.status) === 'complete';
  const displayLanguage = text(row.display_language), sourceLanguage = text(row.source_language) || text(sourceMeta.language);
  const humanText = [text(row.title), ...ingredients, ...steps, text(row.body), servingsText, ...notes,
    text(row.nutrition_notes), ...sections.flatMap(section => [section.heading, section.text])].filter(Boolean);
  const textIsChinese = humanText.length > 0 && humanText.every(readableChinese);
  const pendingTranslation = text(translation.status) === 'needs_review' || text(translation.status) === 'pending'
    || humanText.some(line => !readableChinese(line))
    || (!translated && !displayLanguage.startsWith('zh') && sourceLanguage && !sourceLanguage.startsWith('zh'));
  const languageState = pendingTranslation ? 'pending' : textIsChinese ? 'chinese' : 'unknown';
  const contentReviewed = row.manual_reviewed === true || sourceMeta.manual_reviewed === true
    || object(translation.validation).semantic_quality_manually_reviewed === true;
  const machineTranslationUnreviewed = qualityFlags.includes('machine_translation_unreviewed') || (translated && !contentReviewed);
  const languageNotice = languageState === 'pending' ? '中文翻译待完成或待核对；当前内容可能仍含原文。'
    : languageState === 'unknown' ? '内容语言与翻译状态尚未确认。'
      : machineTranslationUnreviewed ? '当前为中文机器译文，尚未经过人工复核。' : '';
  const sourceCompleteUnreviewed = row.source_complete_unreviewed === true || sourceMeta.manual_reviewed === false;
  const meaningfulFlags = qualityFlags.filter(flag => flag !== 'html_whitespace_normalized_and_printed_page_markers_removed'
    && !(flag === 'original_english_not_translated' && translated && languageState === 'chinese'));
  const qualityMessages = unique(meaningfulFlags.map(flag => flag === 'original_english_not_translated'
    ? '来源原文为英文，中文翻译尚未完成。' : qualityMessage(flag)));
  const parsingNeedsReview = meaningfulFlags.some(flag => !informationalQuality.has(flag));
  const original = object(sourceMeta.original_content);
  const originalIngredients = recipeLines(original.ingredients).length ? recipeLines(original.ingredients) : recipeLines(row.original_ingredients);
  const originalContent = Object.keys(original).length || originalIngredients.length ? {
    title: text(original.title), ingredients: originalIngredients, steps: recipeLines(original.steps),
    body: text(original.body), servingsText: scalarText(original.servings_text) || scalarText(original.servings),
    notes: recipeLines(original.notes), reviewSections: reviewSections(original.review_sections),
  } : null;
  let traceableSource = false;
  try { traceableSource = ['https:', 'http:'].includes(new URL(sourceUrl).protocol); } catch { /* Missing or invalid source stays unverified. */ }
  // Original attribution is an explicit persisted user declaration, never inferred from ownership.
  const userOriginal = scope === 'personal' && row.origin === 'user_original';
  const tutorialMissing = [
    !ingredients.length ? '食材与用量' : '', !steps.length ? '步骤' : '',
    !servings && !yieldText && !servingsText ? '份量或成品数量' : '', !traceableSource && !userOriginal ? '可追溯来源' : '',
  ].filter(Boolean);
  const completeFields = tutorialMissing.length === 0;
  const tutorialComplete = row.tutorial_complete !== false && row.tutorial_status !== 'source_summary'
    && completeFields && !parsingNeedsReview && languageState === 'chinese';
  const tutorialLabel = languageState !== 'chinese' ? '中文内容待补齐'
    : tutorialComplete ? sourceCompleteUnreviewed || machineTranslationUnreviewed ? '内容齐备 · 待复核' : '配料与做法'
      : ingredients.length || steps.length ? '现有内容 · 尚需补全核对' : '来源摘要 · 尚非完整教程';
  return {
    id, title: text(row.title) || '未命名菜谱', scope, origin: text(row.origin), category: recipeCategory(row.cuisine),
    meal: text(row.meal), tags: recipeLines(row.tags), ingredients, steps,
    servings, yieldText, servingsText, sourceUrl,
    sourceAttribution: text(license.attribution), licenseName: text(license.id), licenseUrl: text(license.url),
    body: text(row.body),
    tutorialComplete, tutorialMissing, tutorialLabel, notes, reviewSections: sections,
    qualityFlags, qualityMessages, sourceCompleteUnreviewed, languageState, languageNotice,
    machineTranslationUnreviewed, originalContent,
    nutritionNotes: text(row.nutrition_notes),
    nutritionLabel: scope === 'public' ? '公共来源的搭配说明' : '个人搭配笔记 · 未经营养审核',
    notice: text(row.notice),
    evaluation: row.evaluation && typeof row.evaluation === 'object' ? row.evaluation as Record<string, unknown> : null,
  };
}

export function recipeViews(value: unknown): RecipeView[] {
  if (!Array.isArray(value)) return [];
  return value.map(recipeView).filter((row): row is RecipeView => row !== null);
}

export function recipeVerdict(recipe: RecipeView, diet: string): string {
  const results = recipe.evaluation?.results;
  if (!results || typeof results !== 'object') return '待核实';
  const result = (results as Record<string, unknown>)[diet];
  const status = result && typeof result === 'object' ? (result as Record<string, unknown>).status : null;
  return status === 'suitable' ? '按所示配料符合' : status === 'not_suitable' ? '不符合' : '待核实';
}
