export type RecipeCandidate = {
  candidate_id: string;
  title: string;
  source_url: string;
  summary: string;
  notice: string;
  status: string;
  expires_at: string | null;
  tutorial_complete: boolean | null;
  saved_recipe_id?: string | null;
  display_language?: string;
  source_language?: string;
  translation_status?: string;
  quality_flags?: string[];
  translation_quality_flags?: string[];
  source_complete_unreviewed?: boolean;
};

export type CandidateRecipeEdit = {
  title: string;
  ingredients: string;
  steps: string;
  meal: string;
  purpose: string;
  cuisine: string;
  nutrition_notes: string;
  dessert: boolean;
  confirmed: boolean;
  yield_amount?: string;
  yield_unit?: string;
};

export type CandidateRecipeInput = {
  title: string;
  ingredients: string;
  steps: string;
  meal: 'breakfast' | 'lunch' | 'dinner' | 'afternoon_tea';
  purpose: 'everyday' | 'protein' | 'fat';
  cuisine: 'chinese' | 'western' | 'other';
  tags: ['dessert'] | [];
  nutrition_notes: string;
  source_url: string;
  confirmed: true;
  origin: 'external';
  yield_amount: number | null;
  yield_unit: string;
};

export type SavedCandidateRecipe = {
  id: string;
  candidate_id: string;
  title: string;
  ingredients?: string | string[];
  steps?: string | string[];
  source_url?: string;
  servings?: number | null;
  nutrition_notes?: string;
  tutorial_complete?: boolean;
};

export function recipeCandidates(value: unknown): RecipeCandidate[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.flatMap(item => {
    if (!item || typeof item !== 'object') return [];
    const row = item as Record<string, unknown>;
    // HTTP search uses id, while conversation cards use candidate_id. Both are server IDs.
    const id = typeof row.candidate_id === 'string' && row.candidate_id ? row.candidate_id
      : typeof row.id === 'string' ? row.id : '';
    if (!id || seen.has(id)) return [];
    seen.add(id);
    const text = (field: string) => typeof row[field] === 'string' ? row[field] as string : '';
    const meta = row.source_meta && typeof row.source_meta === 'object' ? row.source_meta as Record<string, unknown> : {};
    const translation = row.translation && typeof row.translation === 'object' ? row.translation as Record<string, unknown>
      : meta.translation && typeof meta.translation === 'object' ? meta.translation as Record<string, unknown> : {};
    const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item.trim()) : [];
    return [{ candidate_id: id, title: text('title'), source_url: text('source_url') || text('source'),
      summary: text('summary'), notice: text('notice'), status: text('status'),
      expires_at: text('expires_at') || null,
      saved_recipe_id: text('saved_recipe_id') || null,
      display_language: text('display_language'), source_language: text('source_language') || (typeof meta.language === 'string' ? meta.language : ''),
      translation_status: typeof translation.status === 'string' ? translation.status : '',
      quality_flags: [...new Set([...strings(row.quality_flags), ...strings(meta.quality_flags)])],
      translation_quality_flags: [...new Set([...strings(row.translation_quality_flags), ...strings(meta.translation_quality_flags)])],
      source_complete_unreviewed: row.source_complete_unreviewed === true || meta.manual_reviewed === false,
      tutorial_complete: typeof row.tutorial_complete === 'boolean' ? row.tutorial_complete : null }];
  });
}

/** Stable persisted IDs merge current search results with the recoverable inbox. */
export function recoveredRecipeCandidates(recovered: unknown, current: unknown): RecipeCandidate[] {
  const rows = new Map<string, RecipeCandidate>();
  for (const row of [...recipeCandidates(current), ...recipeCandidates(recovered)]) rows.set(row.candidate_id, row);
  return [...rows.values()].filter(row => !row.saved_recipe_id && row.status !== 'saved' && row.status !== '已保存到个人菜谱');
}

export function candidateExpired(candidate: RecipeCandidate, now = Date.now()): boolean {
  if (candidate.status === 'expired') return true;
  const expiry = candidate.expires_at ? Date.parse(candidate.expires_at) : NaN;
  return Number.isFinite(expiry) && expiry <= now;
}

export function candidateStatusLabel(candidate: RecipeCandidate): string {
  if (candidateExpired(candidate)) return '候选已过期';
  if (candidate.status === 'saved' || candidate.status === '已保存到个人菜谱') return '来源标记已保存';
  const untranslatedTitle = /[a-z]{3,}/i.test(candidate.title) && !/[\u3400-\u9fff]/.test(candidate.title);
  if (untranslatedTitle || candidate.translation_status === 'pending' || candidate.translation_status === 'needs_review'
    || (candidate.source_language && !candidate.source_language.startsWith('zh') && !candidate.display_language?.startsWith('zh'))) return '待核对 · 中文翻译待完成';
  if (candidate.translation_quality_flags?.length) return '待核对 · 机器译文未复核';
  if (candidate.quality_flags?.length) return '待核对 · 来源内容仍需核对';
  if (candidate.source_complete_unreviewed) return '待核对 · 来源结构齐备，内容未复核';
  if (candidate.tutorial_complete === true) return '待核对 · 来源提供教程';
  return '待核对 · 来源摘要';
}

export function candidateReviewNotice(candidate: RecipeCandidate): string {
  const status = candidateStatusLabel(candidate);
  if (status.includes('中文翻译')) return '当前候选仍需补齐或核对中文翻译。原文保留供查看，未视为已完成的中文教程。';
  if (status.includes('机器译文')) return '机器译文尚未人工复核。请逐项核对原文的配料、用量、份数、步骤与注意事项，再决定是否保存。';
  if (candidate.quality_flags?.length || candidate.source_complete_unreviewed) return '来源仍有解析或内容核对事项，配料与步骤非空不代表教程完整。请核对原文后补齐，未知份数保持未注明。';
  return candidate.tutorial_complete === true ? '来源提供了教程，仍需打开原文核对配料、分量和做法，再确认保存。'
    : '这不是完整教程。请先查看原文，核对配料、分量与做法，再决定是否保存。';
}

export function newCandidateEdit(candidate: RecipeCandidate): CandidateRecipeEdit {
  // A search summary is never used as ingredients or a tutorial.
  return { title: candidate.title, ingredients: '', steps: '', meal: 'lunch', purpose: 'everyday',
    cuisine: 'other', nutrition_notes: '', dessert: false, confirmed: false, yield_amount: '', yield_unit: '' };
}

export function candidateRecipeInput(edit: CandidateRecipeEdit, candidate: RecipeCandidate): CandidateRecipeInput {
  const title = edit.title.trim(), ingredients = edit.ingredients.trim(), steps = edit.steps.trim();
  const nutrition = edit.nutrition_notes.trim();
  if (!title || title.length > 100) throw new Error('请填写100字以内的菜谱名称。');
  if (!ingredients || ingredients.length > 6000) throw new Error('请按原文核对并填写配料，最多6000字。');
  if (!steps || steps.length > 10000) throw new Error('请填写分量与详细步骤，不能仅保存来源摘要。');
  if (nutrition.length > 2000) throw new Error('搭配笔记请控制在2000字以内。');
  if (!['breakfast', 'lunch', 'dinner', 'afternoon_tea'].includes(edit.meal)) throw new Error('请选择具体餐次。');
  if (!['everyday', 'protein', 'fat'].includes(edit.purpose)) throw new Error('请选择有效用途。');
  if (!['chinese', 'western', 'other'].includes(edit.cuisine)) throw new Error('请选择有效菜系。');
  const amountText = (edit.yield_amount || '').trim(), unit = (edit.yield_unit || '').trim();
  const amount = amountText ? Number(amountText) : null;
  if (amount !== null && (!Number.isFinite(amount) || amount < 0 || amount > 100)) throw new Error('份量数量应在0至100之间。');
  if (unit.length > 20 || ((amount !== null) !== !!unit)) throw new Error('请同时填写份量数量与单位，或两项都留空。');
  let source: URL;
  try { source = new URL(candidate.source_url); } catch { throw new Error('来源链接须为公开HTTPS网页，请核对候选来源。'); }
  if (source.protocol !== 'https:' || source.username || source.password) throw new Error('来源链接须为公开HTTPS网页，请核对候选来源。');
  if (!edit.confirmed) throw new Error('请先核对原文与填写内容，再勾选确认。');
  return { title, ingredients, steps, nutrition_notes: nutrition,
    meal: edit.meal as CandidateRecipeInput['meal'], purpose: edit.purpose as CandidateRecipeInput['purpose'],
    cuisine: edit.cuisine as CandidateRecipeInput['cuisine'], tags: edit.dessert ? ['dessert'] : [],
    source_url: candidate.source_url, origin: 'external', yield_amount: amount, yield_unit: unit, confirmed: true };
}

export function savedCandidateRecipes(value: unknown): Record<string, SavedCandidateRecipe> {
  if (!Array.isArray(value)) return {};
  const saved: Record<string, SavedCandidateRecipe> = {};
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const row = item as Record<string, unknown>;
    // Only persisted personal records with a stable association restore saved state.
    if (row.scope === 'public' || typeof row.id !== 'string' || !row.id ||
      typeof row.candidate_id !== 'string' || !row.candidate_id) continue;
    saved[row.candidate_id] = row as SavedCandidateRecipe;
  }
  return saved;
}
