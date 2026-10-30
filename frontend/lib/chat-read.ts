import { safeExternalUrl } from './api';

export function readObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export const readText = (value: unknown) => typeof value === 'string' ? value.trim() : '';
export function readRows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(row => row && typeof row === 'object' && !Array.isArray(row)) : [];
}
export function readDate(value: unknown): string {
  const text = readText(value);
  return text && Number.isFinite(Date.parse(text)) ? new Date(text).toLocaleString('zh-CN', { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '';
}

export const storageNames: Record<string, string> = { refrigerated: '冷藏', frozen: '冷冻', ambient: '常温' };
export const conditionNames: Record<string, string> = { raw: '未加工 / 生', cut: '已切配', cooked: '已熟制', opened: '已开封（旧记录）', unopened: '未开封（旧记录）' };
export const foodStatusNames: Record<string, string> = { active: '库存中', used: '已用完', discarded: '已丢弃' };
export const foodClassNames: Record<string, string> = { fresh: '新鲜食材', cooked: '自制熟食 / 剩菜', packaged: '包装成品 / 半成品', unknown: '类别待核对' };
export const reminderStatusNames: Record<string, string> = {
  pending: '等待提醒', queued: '等待处理', running: '正在处理', waiting_configuration: '通知服务未配置',
  waiting_subscription: '设备尚未订阅通知', accepted: '推送服务已接受', succeeded: '任务已完成', failed: '提醒未完成', cancelled: '已取消',
};
export const knowledgeTypeNames: Record<string, string> = { ingredient_rule: '配料规则', storage: '储存参考', recipe: '菜谱资料', nutrition: '营养资料' };

const conditionLabels: Record<string, string> = {
  storage: '存放方式', condition: '加工状态', food_class: '食物类别', opened: '开封状态', location: '存放地点',
  temperature_c: '温度', temperature_min_c: '最低温度', temperature_max_c: '最高温度',
  contains_pungents: '含五辛', yield_text: '成品数量', applicable_foods: '适用食材',
};
/** Keep the server's actual conditions visible, including unfamiliar field names. */
export function conditionLines(value: unknown): string[] {
  const scalar = (key: string, v: unknown): string => {
    if (typeof v === 'boolean') return v ? '是' : '否';
    if (typeof v === 'number' && Number.isFinite(v)) return String(v) + (key.includes('temperature') ? ' ℃' : '');
    if (typeof v !== 'string') return '';
    const names = key === 'storage' ? storageNames : key === 'condition' ? conditionNames : key === 'food_class' ? foodClassNames : {};
    return names[v] || ({ indoor: '室内', outdoor: '室外', unknown: '未知' } as Record<string, string>)[v] || v;
  };
  return Object.entries(readObject(value)).flatMap(([key, v]) => {
    const display = Array.isArray(v) ? v.map(item => scalar(key, item)).filter(Boolean).join('、') : scalar(key, v);
    return display ? [`${conditionLabels[key] || key}：${display}`] : [];
  });
}

export type CitationView = { entryId: string; title: string; source: string; href?: string };
/** URLs remain text unless they are absolute HTTP(S); never turn source prose into a link. */
export function citationViews(value: unknown): CitationView[] {
  const seen = new Set<string>();
  return readRows(value).flatMap(row => {
    const entryId = readText(row.entry_id);
    if (!entryId || seen.has(entryId)) return [];
    seen.add(entryId);
    const source = readText(row.source);
    return [{ entryId, title: readText(row.title) || '知识来源', source, href: safeExternalUrl(source) }];
  });
}

export const chatReadTypes = new Set(['ingredient_history', 'food_inventory', 'food_detail', 'reminder_status', 'recipe_detail', 'food_draft_list', 'knowledge_result']);
