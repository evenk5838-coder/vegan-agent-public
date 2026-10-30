export type PersonalRecipeRecord = Record<string, unknown> & { id: string };
export type RecipeRequester = <T>(path: string, init?: RequestInit) => Promise<T>;
export type RecipeEdit = {
  title: string; ingredients: string; steps: string; meal: string; purpose: string; cuisine: string;
  nutrition_notes: string; source_url: string; origin: string; yield_amount: string; yield_unit: string;
  dessert: boolean; confirmed: boolean; unsupportedTags: string[];
};
export type RecipeInput = {
  title: string; ingredients: string; steps: string; meal: string; purpose: string; cuisine: string;
  nutrition_notes: string; source_url: string; origin: 'user_original' | 'adapted' | 'external';
  yield_amount: number | null; yield_unit: string; tags: string[]; confirmed: true;
};
const text = (value: unknown) => typeof value === 'string' ? value : '';
const lines = (value: unknown) => Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string').join('\n') : text(value);
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
export function personalRecipeRecord(value: unknown): PersonalRecipeRecord | null {
  const row = object(value);
  return typeof row.id === 'string' && row.id && row.scope !== 'public' && row.origin !== 'public_knowledge' ? row as PersonalRecipeRecord : null;
}

/** Candidate recovery still needs the complete personal prefix, rather than the first 20 records. */
export async function loadPersonalRecipeRecords(request: RecipeRequester, signal?: AbortSignal): Promise<PersonalRecipeRecord[]> {
  const rows = new Map<string, PersonalRecipeRecord>();
  let page = 1;
  for (;;) {
    const data = await request<Record<string, unknown>>(`/recipes?page=${page}&page_size=50`, { signal });
    if (!Array.isArray(data.items)) throw new Error('个人菜谱列表暂时无法读取，请重新读取状态。');
    for (const item of data.items) {
      const record = personalRecipeRecord(item);
      if (record) rows.set(record.id, record);
    }
    const count = typeof data.personal_count === 'number' && Number.isSafeInteger(data.personal_count) && data.personal_count >= 0 ? data.personal_count : null;
    if (count !== null && rows.size >= count) return [...rows.values()];
    if (data.has_more !== true) {
      if (count !== null && rows.size < count) throw new Error('个人菜谱列表尚未完整读取，请重新读取状态。');
      return [...rows.values()];
    }
    const next = data.next_page;
    if (typeof next !== 'number' || !Number.isSafeInteger(next) || next <= page || !data.items.length) throw new Error('菜谱分页状态不一致，请重新读取状态。');
    page = next;
  }
}

export async function readPersonalRecipe(request: RecipeRequester, id: string, signal?: AbortSignal): Promise<PersonalRecipeRecord | null> {
  let result: unknown;
  try { result = await request<unknown>('/recipes/' + encodeURIComponent(id), { signal }); }
  catch (reason) { if (object(reason).status === 404) return null; throw reason; }
  const row = personalRecipeRecord(result);
  if (!row || row.id !== id) throw new Error('个人菜谱详情不一致，请重新读取状态。');
  return row;
}

export function recipeRecordVersion(value: unknown): string | null {
  const row = personalRecipeRecord(value);
  const version = row?.version;
  return typeof version === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(version) ? version : null;
}

/** PUT must bind to the version actually displayed for confirmation. */
export function recipeUpdateRequest(baseline: unknown, input: RecipeInput): RequestInit {
  const version = recipeRecordVersion(baseline);
  if (!version) throw new Error('缺少菜谱版本，暂不能保存。请重新读取最新记录后核对。');
  return { method: 'PUT', headers: { 'If-Match': version }, body: JSON.stringify(input) };
}

export type RecipeCreateAttempt = {
  key: string; body: RecipeInput; snapshot: string; outcome: 'pending' | 'uncertain' | 'saved' | 'deleted';
};

/** Keep a key for each exact confirmed body, including a body revisited after editing. */
export function prepareRecipeCreate(attempts: RecipeCreateAttempt[], body: RecipeInput,
                                    makeKey: () => string = () => crypto.randomUUID()): RecipeCreateAttempt {
  const snapshot = recipeSnapshot(body);
  const existing = attempts.find(attempt => attempt.snapshot === snapshot);
  if (existing?.outcome === 'deleted') throw new Error('这次保存的菜谱已删除，不能用原保存操作重新创建。请核对列表。');
  if (existing) return existing;
  const key = makeKey();
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(key)) throw new Error('无法生成保存标识，请保留输入并重新打开页面。');
  const attempt: RecipeCreateAttempt = { key, snapshot, body: { ...body, tags: [...body.tags] }, outcome: 'pending' };
  attempts.push(attempt);
  return attempt;
}

export function recipeCreateRequest(attempt: RecipeCreateAttempt): RequestInit {
  if (attempt.outcome === 'deleted') throw new Error('这次保存的菜谱已删除，不会自动重新创建。');
  return { method: 'POST', headers: { 'Idempotency-Key': attempt.key }, body: JSON.stringify(attempt.body) };
}

/** A replay may return the same saved record after a later edit; never overwrite it to recover. */
export function recipeCreateResult(value: unknown, input: RecipeInput): { record: PersonalRecipeRecord; changedSinceSave: boolean } | null {
  const record = personalRecipeRecord(value);
  if (!record || !recipeRecordVersion(record)) return null;
  const matches = savedRecipeMatches(record, record.id, input);
  if (!matches && record.replayed !== true) return null;
  return { record, changedSinceSave: !matches };
}

export function recipeEditFromRecord(value: unknown): RecipeEdit {
  const row = personalRecipeRecord(value);
  if (!row) throw new Error('只能编辑本人的个人菜谱；公共菜谱保持只读。');
  const tags = Array.isArray(row.tags) ? row.tags.filter((tag): tag is string => typeof tag === 'string') : [];
  return {
    title: text(row.title), ingredients: lines(row.ingredients), steps: lines(row.steps), meal: text(row.meal),
    purpose: text(row.purpose), cuisine: text(row.cuisine),
    nutrition_notes: text(row.nutrition_notes), source_url: text(row.source_url),
    origin: row.origin === 'web_confirmed_by_user' ? 'external' : text(row.origin),
    yield_amount: typeof row.yield_amount === 'number' && Number.isFinite(row.yield_amount) ? String(row.yield_amount) : '',
    yield_unit: text(row.yield_unit), dessert: tags.includes('dessert'), confirmed: false,
    unsupportedTags: tags.filter(tag => tag !== 'dessert'),
  };
}

export function recipeEditInput(edit: RecipeEdit): RecipeInput {
  const title = edit.title.trim(), ingredients = edit.ingredients.trim(), steps = edit.steps.trim();
  if (!title || title.length > 100) throw new Error('请填写100字以内的菜谱名称。');
  if (!ingredients || ingredients.length > 6000) throw new Error('请核对食材与用量，最多6000字。');
  if (!steps || steps.length > 10000) throw new Error('请核对详细做法，最多10000字。');
  if (!['breakfast', 'lunch', 'dinner', 'afternoon_tea', 'main'].includes(edit.meal)) throw new Error('请核对餐次。');
  if (!['everyday', 'protein', 'fat'].includes(edit.purpose)) throw new Error('请核对用途标签。');
  if (!['chinese', 'western', 'other', 'foreign'].includes(edit.cuisine)) throw new Error('请核对菜谱分类。');
  if (!['user_original', 'adapted', 'external'].includes(edit.origin)) throw new Error('请明确记录菜谱来源。');
  if (edit.unsupportedTags.length) throw new Error('原记录包含当前表单未支持的标签，暂不能安全修改。');
  const source = edit.source_url.trim();
  if (source.length > 2000) throw new Error('来源链接最多2000字。');
  if (source) {
    let url: URL;
    try { url = new URL(source); } catch { throw new Error('来源链接须为公开HTTPS网页。'); }
    if (url.protocol !== 'https:' || url.username || url.password) throw new Error('来源链接须为公开HTTPS网页。');
  }
  if (edit.origin !== 'user_original' && !source) throw new Error('转载或改编的菜谱需要填写来源链接。');
  const amountText = edit.yield_amount.trim();
  const amount = amountText ? Number(amountText) : null;
  if (amount !== null && (!Number.isFinite(amount) || amount < 0 || amount > 100)) throw new Error('份量数量应在0至100之间。');
  const unit = edit.yield_unit.trim();
  if (unit.length > 20) throw new Error('份量单位最多20字。');
  if ((amount !== null) !== !!unit) throw new Error('请同时填写份量数量与单位，或两项都留空。');
  const nutrition = edit.nutrition_notes.trim();
  if (nutrition.length > 2000) throw new Error('搭配笔记最多2000字。');
  if (!edit.confirmed) throw new Error('请重新核对并勾选确认后保存。');
  return { title, ingredients, steps, meal: edit.meal, purpose: edit.purpose, cuisine: edit.cuisine,
    source_url: source, origin: edit.origin as RecipeInput['origin'], yield_amount: amount, yield_unit: unit,
    nutrition_notes: nutrition, tags: edit.dessert ? ['dessert'] : [], confirmed: true };
}

/** Compare only editable fields, while keeping source attribution and quantity in the comparison. */
export function recipeSnapshot(value: unknown): string {
  const row = object(value);
  return JSON.stringify({
    title: text(row.title), ingredients: lines(row.ingredients), steps: lines(row.steps), meal: text(row.meal),
    purpose: text(row.purpose), cuisine: text(row.cuisine), origin: text(row.origin),
    source_url: text(row.source_url), nutrition_notes: text(row.nutrition_notes),
    yield_amount: typeof row.yield_amount === 'number' && Number.isFinite(row.yield_amount) ? row.yield_amount : null,
    yield_unit: text(row.yield_unit), tags: Array.isArray(row.tags) ? [...row.tags].sort() : [],
  });
}

export function savedRecipeMatches(record: unknown, id: string, input: RecipeInput): boolean {
  const row = personalRecipeRecord(record);
  return !!row && row.id === id && recipeSnapshot(row) === recipeSnapshot(input);
}
