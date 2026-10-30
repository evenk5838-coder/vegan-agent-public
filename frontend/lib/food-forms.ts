import type { FoodSuggestion } from './food-advice';
import type { ConsumptionPlan } from './consumption-plan';

export type DraftItem = {
  item_id: string; name?: string; storage?: string; condition?: string; date_type?: string;
  food_class?: string; opened?: boolean | null; suggestion?: FoodSuggestion | null;
  consumption_plan?: ConsumptionPlan | null; storage_guidance_summary?: string;
  started_at?: string | null; expires_at?: string | null; deadline_source?: string; package_note?: string;
  remind_at?: string | null; location?: string; temperature_c?: number | null; status: string;
  missing: string[]; saved_food_id?: string | null; field_sources?: Record<string, string>;
  date_source?: string; date_phrase?: string; date_reference_at?: string; date_timezone?: string;
  time_precision?: string; date_warnings?: string[];
};

export type ParsedDate = {
  started_at: string; resolved_date: string; time_precision: string;
  phrase: string; reference_at: string; timezone: string; warnings: string[];
};

export type FoodEdit = Record<string, string>;
export const foodFieldLabels: Record<string, string> = {
  name: '食材名称', storage: '存放方式', condition: '加工状态', date_type: '日期类型',
  food_class: '食物类别', opened: '是否开封',
  started_at: '开始日期', expires_at: '预计到期时间', deadline_source: '期限依据',
  package_note: '包装说明', remind_at: '提醒时间', location: '存放地点', temperature_c: '温度',
};

export function localDate(value?: string | null) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function editValues(item: DraftItem): FoodEdit {
  return {
    name: item.name ?? '', storage: item.storage || 'unknown',
    condition: ['raw', 'cut', 'cooked'].includes(item.condition || '') ? item.condition! : 'unknown',
    food_class: item.food_class ?? 'unknown',
    opened: typeof item.opened === 'boolean' ? String(item.opened) : item.condition === 'opened' ? 'true' : item.condition === 'unopened' ? 'false' : '',
    date_type: item.date_type || 'unknown',
    started_at: localDate(item.started_at), expires_at: localDate(item.expires_at),
    deadline_source: item.deadline_source ?? 'unknown', package_note: item.package_note ?? '',
    remind_at: localDate(item.remind_at), location: item.location ?? 'unknown',
    temperature_c: item.temperature_c == null ? '' : String(item.temperature_c),
  };
}

function isoTime(value: string, label: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error(`请检查${label}。`);
  return date.toISOString();
}

function temperature(value: string) {
  if (!value.trim()) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < -80 || number > 70) throw new Error('温度应在 -80 至 70 ℃ 之间。');
  return number;
}

export function draftPatches(item: DraftItem, values: FoodEdit) {
  const old = editValues(item);
  const changes: Record<string, string | number | boolean | null> = {};
  for (const field of Object.keys(foodFieldLabels)) {
    if (values[field] === old[field]) continue;
    const value = values[field].trim();
    if (!value && field === 'name') {
      throw new Error(`“${item.name || '这条食材'}”的${foodFieldLabels[field]}不能清空。`);
    }
    if (['started_at', 'expires_at', 'remind_at'].includes(field)) {
      changes[field] = value ? isoTime(value, foodFieldLabels[field]) : null;
    } else if (field === 'opened') {
      changes[field] = value === '' ? null : value === 'true';
    } else if (field === 'temperature_c') {
      changes[field] = temperature(value);
    } else if (['storage', 'condition', 'date_type', 'food_class', 'location', 'deadline_source'].includes(field)) {
      changes[field] = value || 'unknown';
    } else changes[field] = value || null;
  }
  return changes;
}

/** Check the server response rather than treating a successful HTTP status as applied fields. */
export function unappliedFoodFields(changes: Record<string, unknown>, item: Record<string, unknown>) {
  return Object.entries(changes).filter(([field, expected]) => {
    const actual = item[field];
    if (expected == null) return actual != null && actual !== '';
    if (['started_at', 'expires_at', 'remind_at'].includes(field)) return Date.parse(String(actual)) !== Date.parse(String(expected));
    return actual !== expected;
  }).map(([field]) => foodFieldLabels[field] || field);
}

export function foodConditionBody(previous: Record<string, unknown>, values: FoodEdit) {
  return {
    name: previous.name, started_at: previous.started_at ?? null, date_type: values.date_type || 'unknown',
    status: previous.status, storage: values.storage || 'unknown',
    condition: ['raw', 'cut', 'cooked'].includes(values.condition) ? values.condition : 'unknown',
    food_class: values.food_class || 'unknown', opened: values.opened === '' ? null : values.opened === 'true',
    location: values.location || 'unknown', temperature_c: temperature(values.temperature),
    // Preserve the recorded basis for the server to re-evaluate; no client-calculated dates.
    deadline_source: previous.deadline_source || 'unknown', expires_at: previous.expires_at ?? null,
    package_note: previous.package_note || '', remind_at: previous.remind_at ?? null,
    storage_note: previous.storage_note ?? '',
  };
}

export function draftCreateBody(names: string[], phrase: string, dateType: string,
  preview: ParsedDate | null, previewInput: string) {
  const cleaned = names.map(name => name.trim()).filter(Boolean);
  if (!cleaned.length) throw new Error('请至少填写一项食材名称。');
  if (cleaned.length > 30) throw new Error('每批最多 30 项，请分批登记。');
  const text = phrase.trim();
  if (text && (!preview || previewInput !== text)) throw new Error('请先点击“核对日期”，确认日期解析结果。');
  return {
    items: cleaned.map(name => ({ name, ...(text ? { date_phrase: text } : {}), ...(dateType ? { date_type: dateType } : {}) })),
    ...(text && preview ? { reference_time: preview.reference_at, timezone: preview.timezone } : {}),
  };
}

export function foodEntryDraftBody(input: FoodEdit) {
  const empty: DraftItem = { item_id: 'new', status: 'needs_info', missing: [] };
  const values = { ...editValues(empty), ...input };
  values.name = values.name.trim();
  if (!values.name) throw new Error('请填写食材名称。');
  if (values.expires_at && !['package', 'user'].includes(values.deadline_source)) throw new Error('填写日期时，请选择包装依据或用户自行设置。');
  // Create only a review draft. No default dates, storage, processing state or food class.
  return { items: [draftPatches(empty, values)] };
}

/** Other missing facts affect advice, not whether the user can keep a food record. */
export function foodDraftReady(item: Pick<DraftItem, 'name'>) {
  return !!item.name?.trim();
}

export function foodDraftSummary(item: Pick<DraftItem, 'storage' | 'started_at' | 'date_type'>) {
  const storage = ({ ambient: '常温', refrigerated: '冷藏', frozen: '冷冻' } as Record<string, string>)[item.storage || ''] || '存放方式待补';
  const parsed = item.started_at ? new Date(item.started_at) : null;
  const meaning = ({ purchase: '购买', production: '生产', prepared: '制作', opened: '开封' } as Record<string, string>)[item.date_type || ''] || '已知日期';
  return `${storage} · ${parsed && !Number.isNaN(parsed.getTime()) ? meaning + ' ' + parsed.toLocaleDateString('zh-CN') : '日期可后补'}`;
}

export function reevaluationBody(previous: { temperature_c: number | null; remind_at: string | null; package_note: string }, values: FoodEdit) {
  const remindAt = values.reminder.trim();
  const clear: string[] = [];
  // The evidence object's null reminder is not a clear instruction on the server.
  if (!remindAt && previous.remind_at) clear.push('remind_at');
  if (!values.note.trim() && previous.package_note) clear.push('package_note');
  return {
    ...(['ambient', 'refrigerated', 'frozen'].includes(values.storage) ? { storage: values.storage } : {}),
    ...(['raw', 'cut', 'cooked', 'opened', 'unopened'].includes(values.condition) ? { condition: values.condition } : {}),
    location: values.location || 'unknown',
    temperature_c: temperature(values.temperature),
    ...(clear.length ? { clear } : {}),
    evidence: {
      deadline_source: values.basis,
      expires_at: values.deadline ? isoTime(values.deadline, '所设日期') : null,
      package_note: values.note.trim(),
      ...(remindAt ? { remind_at: isoTime(remindAt, '提醒时间') } : {}),
    },
  };
}
