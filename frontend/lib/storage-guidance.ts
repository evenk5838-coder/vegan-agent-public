/** Display-only storage reference. Deliberately contains no deadline/reminder fields. */
export type StorageGuidance = {
  status: 'ready' | 'no_reliable_sources'; summary: string; name: string;
  conditions: string[]; sources: { id: number; title: string; url: string }[];
  generatedAt: string; cached: boolean;
};
const fields = ['name', 'food_class', 'storage', 'condition', 'opened', 'temperature_c', 'date_type', 'started_at', 'status', 'storage_note', 'expires_at', 'deadline_source', 'package_note'] as const;
export function guidanceContext(food: object) {
  const row = food as Record<string, unknown>;
  return JSON.stringify(fields.map(field => row[field] ?? null));
}
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function sourceUrl(value: unknown): string | null {
  try {
    const url = new URL(typeof value === 'string' ? value : '');
    const domains = ['gov.cn', 'foodsafety.gov', 'usda.gov', 'fda.gov', 'cfs.gov.hk'];
    return url.protocol === 'https:' && !url.username && !url.password && domains.some(domain => url.hostname === domain || url.hostname.endsWith('.' + domain)) ? url.href : null;
  } catch { return null; }
}
export function storageGuidanceView(value: unknown, foodId: string): StorageGuidance | null {
  const envelope = object(value);
  if (envelope.food_id !== foodId) throw new Error('食材结果不匹配，请重新读取。');
  if (envelope.status === 'not_requested' && envelope.guidance === null) return null;
  const data = object(envelope.guidance), observed = object(data.observed);
  if (envelope.status !== 'available' || data.scope !== 'storage_reference_only'
      || !['ready', 'no_reliable_sources'].includes(String(data.status))
      || typeof data.summary !== 'string' || !data.summary.trim()
      || typeof data.generated_at !== 'string' || !Number.isFinite(Date.parse(data.generated_at))) {
    throw new Error('暂时无法读取储存建议，请重新读取。');
  }
  const seen = new Set<string>();
  const sources = (Array.isArray(data.sources) ? data.sources : []).flatMap(value => {
    const source = object(value), url = sourceUrl(source.url);
    if (!url || !Number.isInteger(source.id) || seen.has(url)) return [];
    seen.add(url);
    return [{ id: source.id as number, title: typeof source.title === 'string' && source.title.trim() ? source.title : new URL(url).hostname, url }];
  });
  if (data.status === 'ready' && !sources.length) throw new Error('这份建议缺少可核对的来源，请重新读取。');
  return { status: data.status as StorageGuidance['status'], summary: data.summary,
    name: typeof observed.name === 'string' ? observed.name : '',
    conditions: (Array.isArray(data.conditions_to_check) ? data.conditions_to_check : []).filter((s): s is string => typeof s === 'string' && !!s.trim()),
    sources, generatedAt: data.generated_at, cached: data.cached === true };
}
export function guidanceExpired(value: StorageGuidance, now = Date.now()) {
  return now >= Date.parse(value.generatedAt) + 24 * 60 * 60 * 1000;
}
export function guidanceFailure(error: unknown, searching: boolean) {
  const value = object(error), code = String(value.code || '');
  if (['NOT_FOUND', 'FOOD_INACTIVE', 'AUTH_REQUIRED'].includes(code)) {
    return { terminal: true, message: code === 'AUTH_REQUIRED' ? '请重新登录后查看储存建议。' : '这份食材已删除或结束管理。' };
  }
  if (code === 'STORAGE_CONTEXT_CHANGED') return { terminal: false, message: '储存条件已变化，请先读取当前记录的建议。' };
  if (searching && (code === 'CONNECTION_LOST' || value.name === 'TimeoutError' || value.name === 'AbortError')) {
    return { terminal: false, message: '等待结果超时或连接中断。请先读取查询结果，避免重复查询。' };
  }
  return { terminal: false, message: error instanceof Error ? error.message : '暂时无法读取建议，请稍后重试。' };
}
