/** Display the server's eating plan; never derive a deadline from advice text. */
export type ConsumptionPlan = {
  version: string; scope: string; safety_guarantee: boolean; status: string;
  target_at?: string | null; start_at?: string | null; date_origin?: string;
  source?: { title?: string; url?: string } | null;
  evidence_quote?: string; message?: string;
  basis?: 'web_reference' | 'conservative_schedule'; key_reminders?: string[];
};
export function consumptionPlanView(plan?: ConsumptionPlan | null, now = Date.now()) {
  if (!plan || plan.version !== 'consumption-plan-2026-10-29.1' || plan.scope !== 'eating_plan' || plan.safety_guarantee !== false) return null;
  if (plan.basis !== undefined && !['web_reference', 'conservative_schedule'].includes(plan.basis)) return null;
  const messages: Record<string, string> = {
    needs_context: '食用时间待确认', unavailable: '暂未查到适用的食用时间', outdated: '条件已变化，请重新核对食用时间',
  };
  const ready = ['ready', 'due'].includes(plan.status);
  if (!ready && !messages[plan.status]) return null;
  const end = ready && plan.target_at ? Date.parse(plan.target_at) : NaN;
  const start = ready && plan.start_at ? Date.parse(plan.start_at) : NaN;
  const valid = Number.isFinite(end) && Number.isFinite(start) && end > start;
  const remaining = end - now;
  const due = valid && remaining <= 0;
  const hours = remaining / 3600000;
  const label = !valid ? messages[plan.status] || '食用时间待核对' : due ? '已到建议食用时间'
    : hours < 1 ? '约剩不到 1 小时' : hours < 48 ? `约剩 ${Math.ceil(hours)} 小时` : `约剩 ${Math.ceil(hours / 24)} 天`;
  let source: { title: string; url: string } | null = null;
  if (valid && plan.source?.url) {
    try {
      const url = new URL(plan.source.url);
      const domains = ['gov.cn', 'foodsafety.gov', 'usda.gov', 'fda.gov', 'cfs.gov.hk'];
      if (url.protocol === 'https:' && !url.username && !url.password && domains.some(d => url.hostname === d || url.hostname.endsWith('.' + d))) {
        source = { title: plan.source.title || url.hostname, url: url.href };
      }
    } catch { /* Malformed sources are not links. */ }
  }
  return { label, due, target: valid ? plan.target_at! : null, start: valid ? plan.start_at! : null,
    percent: valid ? Math.max(0, Math.min(100, (now - start) / (end - start) * 100)) : null,
    source, quote: valid ? plan.evidence_quote : null, dateOrigin: plan.date_origin,
    conservative: valid && plan.basis === 'conservative_schedule',
    message: valid && typeof plan.message === 'string' ? plan.message : null,
    reminders: valid && Array.isArray(plan.key_reminders) ? plan.key_reminders.filter(value => typeof value === 'string' && value.trim()) : [] };
}
export type ConsumptionPlanView = NonNullable<ReturnType<typeof consumptionPlanView>>;
export function planDate(value: string, omitMidnight = false) {
  const date = new Date(value);
  const midnight = date.toLocaleTimeString('en-GB', { timeZone: 'Asia/Shanghai', hour12: false }) === '00:00:00';
  return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric',
    ...(omitMidnight && midnight ? {} : { hour: '2-digit' as const, minute: '2-digit' as const, hour12: false }) });
}
