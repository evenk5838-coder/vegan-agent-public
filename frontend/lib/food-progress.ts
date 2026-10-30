import type { FoodSuggestion } from './food-advice';
import type { ConsumptionPlan } from './consumption-plan';

export type Food = {
  id: string; name: string; storage: string; condition: string; location: string;
  food_class?: string; opened?: boolean | null; suggestion?: FoodSuggestion | null;
  consumption_plan?: ConsumptionPlan | null; storage_note?: string;
  temperature_c: number | null; started_at: string | null; date_type: string; created?: string | null;
  expires_at: string | null; deadline_source: string; package_note: string;
  remind_at: string | null; status: string; display_status: string; storage_history: unknown[];
  valid_started_at?: string | null; valid_deadline_at?: string | null;
  stored_for_days?: number | null; estimated_remaining_days?: number | null;
  date_origin?: string; elapsed_time_label?: string;
  assessment_status?: string;
  deadline_basis?: { type: string; rules_status?: string | null; rule_source?: string | null; note?: string };
};

export function foodProgress(food: Food, now = Date.now()) {
  const pending = food.assessment_status === 'needs_review' || food.display_status === 'needs_review';
  const draftRule = food.deadline_basis?.type === 'rule' && food.deadline_basis.rules_status !== 'published';
  const eligible = food.assessment_status === 'estimated' || food.assessment_status === 'user_set';
  const deadline = pending || draftRule || !eligible ? null : food.valid_deadline_at;
  const end = deadline ? Date.parse(deadline) : NaN;
  const start = food.valid_started_at ? Date.parse(food.valid_started_at) : NaN;
  let label = pending ? food.storage === 'unknown' || !food.started_at ? '信息待补充，期限待核对' : '条件已变化，期限待核对' : draftRule ? '储存参考条目待复核' : '尚无适用期限';
  if (Number.isFinite(end)) {
    const hours = (end - now) / 3600000;
    label = hours <= 0 ? '已到所设日期' : hours < 1 ? '距所设日期不足 1 小时' : hours < 48 ? `预计剩余 ${Math.ceil(hours)} 小时` : `预计剩余 ${Math.ceil(hours / 24)} 天`;
    if (food.assessment_status === 'user_set') label = label.replace('预计剩余', '距自设日期');
  }
  // A user-set date has no system-estimated total lifetime. Never invent a start or freshness score.
  const percent = food.status === 'active' && food.assessment_status === 'estimated' && !draftRule && !pending && Number.isFinite(end) && Number.isFinite(start) && end > start
    ? Math.max(0, Math.min(100, (now - start) / (end - start) * 100)) : null;
  return { label, percent, deadline: Number.isFinite(end) ? deadline : null, pending, draftRule };
}
