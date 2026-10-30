export type FoodPlanningReference = {
  kind: 'planning_reference'; title: string; text: string;
  source_url: string; source_title: string; conditions: string[];
};

export type FoodSuggestion = {
  action?: string; date?: string | null; reason?: string; missing?: string[];
  basis?: { type?: string; note?: string; rule_id?: string; rule_version?: string; rule_source?: string };
  rules_status?: string; advice_version?: string;
  usage?: {
    priority?: string; title?: string; text?: string;
    reference?: FoodPlanningReference | null;
    observed?: { food_class?: string | null; storage?: string | null; condition?: string | null;
      opened?: boolean | null; date_type?: string | null; started_at?: string | null;
      expires_at?: string | null; deadline_source?: string | null };
  };
};

/** A conditional planning reference is display-only, never an expiry calculation. */
export function foodReferenceView(suggestion?: FoodSuggestion | null, now = Date.now()) {
  const reference = suggestion?.usage?.reference;
  if (reference?.kind !== 'planning_reference' || typeof reference.title !== 'string' || !reference.title.trim()
    || typeof reference.text !== 'string' || !reference.text.trim()) return null;
  if (suggestion?.reason === 'date_passed' || suggestion?.reason === 'package_not_applicable') return null;
  const observed = suggestion?.usage?.observed;
  const expiry = observed?.expires_at;
  const start = observed?.started_at;
  if (expiry && (!Number.isFinite(Date.parse(expiry)) || Date.parse(expiry) <= now)) return null;
  if (start && (!Number.isFinite(Date.parse(start)) || Date.parse(start) > now)) return null;
  return {
    title: reference.title.trim(), text: reference.text.trim(),
    source: typeof reference.source_url === 'string' ? reference.source_url : '',
    sourceTitle: typeof reference.source_title === 'string' ? reference.source_title.trim() : '',
    conditions: Array.isArray(reference.conditions) ? reference.conditions.filter((condition): condition is string => typeof condition === 'string' && !!condition.trim()).map(condition => condition.trim()) : [],
  };
}

/** Usage planning never supplies a deadline or changes the progress contract. */
export function foodUsageView(suggestion?: FoodSuggestion | null, now = Date.now()) {
  const usage = suggestion?.usage;
  if (!usage?.title?.trim() || !usage.text?.trim()) return null;
  const observed = usage.observed || {};
  let priority = usage.priority || 'check_first', title = usage.title, text = usage.text;
  const expiry = observed.expires_at ? Date.parse(observed.expires_at) : NaN;
  if (suggestion?.reason === 'date_passed' || (Number.isFinite(expiry) && expiry <= now)) {
    priority = 'check_first'; title = '先核对已过的记录日期';
    text = '记录日期已过，请先核对包装与实际储存情况；这不判断食物是否仍可食用。';
  } else if (suggestion?.reason === 'package_not_applicable') {
    priority = 'check_first'; title = '先核对当前情况与包装要求';
    text = '原包装说明不适用于当前记录，先核对开封状态和储存条件，不沿用原日期。';
  } else if (suggestion?.reason === 'missing_info' || suggestion?.missing?.length) {
    priority = 'check_first'; title = '先补齐实际储存情况';
    text = '先核对食品类别、存放方式和日期含义，再安排使用；现在不推算食用天数。';
  }
  const storage: Record<string, string> = { refrigerated: '冷藏', frozen: '冷冻', ambient: '常温' };
  const classes: Record<string, string> = { fresh: '新鲜食材', cooked: '熟食', packaged: '包装食品' };
  const events: Record<string, string> = { purchase: '购买', production: '生产', prepared: '制作完成', opened: '开封' };
  const facts: string[] = [];
  if (observed.food_class && classes[observed.food_class]) facts.push(classes[observed.food_class]);
  facts.push(storage[observed.storage || ''] || '存放方式待核对');
  if (typeof observed.opened === 'boolean') facts.push(observed.opened ? '已开封' : '未开封');
  const recordedAt = observed.started_at ? Date.parse(observed.started_at) : NaN;
  if (Number.isFinite(recordedAt)) facts.push(`${events[observed.date_type || ''] || '记录起点'}：${new Date(recordedAt).toLocaleString('zh-CN')}`);
  if (Number.isFinite(expiry)) facts.push(`${observed.deadline_source === 'user' ? '自设' : '已记录'}日期：${new Date(expiry).toLocaleString('zh-CN')}`);
  return { priority, title, text, facts };
}

export function foodAdviceView(suggestion?: FoodSuggestion | null, now = Date.now()) {
  if (!suggestion) return { title: '暂未取得食用建议', text: '可先记录实际情况，取得依据后再核对。', date: null, source: '', warning: false };
  const basis = suggestion.basis?.type;
  const trustedBasis = basis === 'package' || (basis === 'rule' && suggestion.rules_status === 'published');
  const validDate = suggestion.date && Number.isFinite(Date.parse(suggestion.date)) ? suggestion.date : null;
  const passed = suggestion.reason === 'date_passed' || (!!validDate && Date.parse(validDate) <= now);
  if (basis === 'user') return { title: '这是你自行设置的日期', text: '不属于系统食用建议。', date: null, source: '', warning: false };
  if (basis === 'rule' && suggestion.rules_status !== 'published') return { title: '储存规则尚待复核', text: '未发布规则不作为食用期限依据。', date: null, source: '', warning: true };
  if (trustedBasis && validDate) return {
    title: passed ? basis === 'package' ? '包装标示日期已过' : '规则参考日期已过' : basis === 'package' ? '按适用包装说明核对' : '按已发布规则估算',
    text: passed ? '不要把已过的参考日期当作仍可食用的建议。' : '这是依据当前记录条件得到的参考日期，不代表实际新鲜度或食用安全。',
    date: validDate, source: basis === 'rule' ? suggestion.basis?.rule_source || '' : '', warning: passed,
  };
  const reasons: Record<string, [string, string]> = {
    missing_info: ['还需要补充信息', '补齐实际情况后再核对；现在不推算食用天数。'],
    no_published_rule: ['暂无适用的已发布规则', '目前信息已记录，不需要反复补填；可以核对包装说明或另设自己的提醒日期。'],
    package_not_applicable: ['包装说明不适用于当前情况', '请重新核对实际状态与包装要求，暂不沿用包装日期。'],
  };
  const [title, text] = reasons[suggestion.reason || ''] || ['暂无法估算食用日期', '没有适用依据时，不生成天数或“几天内安全”的结论。'];
  return { title, text, date: null, source: '', warning: suggestion.reason === 'package_not_applicable' };
}

export type DraftConfirmationSource = {
  draft_id: string; advice_digest?: string | null;
  items: { item_id: string; saved_food_id?: string | null }[];
};
export type DraftConfirmationAttempt = { key: string; ids: string[]; digest: string };

export function prepareFoodConfirmation(draft: DraftConfirmationSource, makeKey: () => string): DraftConfirmationAttempt {
  const digest = draft.advice_digest?.trim();
  if (!digest) throw new Error('未取得本次建议的核对凭据，请重新读取草稿后再确认。');
  const ids = draft.items.filter(item => !item.saved_food_id).map(item => item.item_id);
  if (!ids.length) throw new Error('这份草稿没有待保存的食材。');
  return { key: makeKey(), ids, digest };
}

export function foodConfirmationBody(operation: DraftConfirmationAttempt) {
  return { item_ids: operation.ids, idempotency_key: operation.key, advice_digest: operation.digest };
}

export function autoAssessmentMessage(result: { auto_reevaluated?: boolean; reason?: string; assessment_status?: string }) {
  if (result.reason === 'user_date_preserved') return '当前条件已保存；你自行设置的日期已保留，它不属于系统食用建议。';
  if (result.auto_reevaluated && (result.assessment_status === 'needs_review' || result.reason !== 'ok')) return '当前条件已保存，未找到适用期限依据；请核对下方建议与提醒状态。';
  if (result.auto_reevaluated) return '当前条件已保存，服务器已重新核对期限；结果与依据见下方。';
  return '当前条件已保存；服务器未报告已完成自动重评，请核对下方期限状态。';
}
