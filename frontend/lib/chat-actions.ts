import { ApiError } from './api';
import { conditionNames, foodClassNames, foodStatusNames, readObject, readText, storageNames, readDate } from './chat-read';

export type PendingAction = {
  record_id: string; action_id: string; kind: string; status: string; target: Record<string, unknown>;
  proposed: Record<string, unknown>; expires_at: string; needs_confirmation: boolean; results: Record<string, unknown>;
  required_ack_fields: string[] | null;
};
export type ActionAttempt = { recordId: string; key: string };
export type ActionClosure = 'stale' | 'expired' | 'closed' | 'missing';

export function pendingAction(value: unknown): PendingAction | null {
  const row = readObject(value);
  const recordId = readText(row.record_id);
  if (!recordId) return null;
  return { record_id: recordId, action_id: readText(row.action_id), kind: readText(row.kind), status: readText(row.status),
    target: readObject(row.target), proposed: readObject(row.proposed), expires_at: readText(row.expires_at),
    needs_confirmation: row.needs_confirmation === true, results: readObject(row.results),
    required_ack_fields: row.required_ack_fields === undefined ? [] : Array.isArray(row.required_ack_fields) && row.required_ack_fields.every(key => typeof key === 'string') ? row.required_ack_fields : null };
}
const contextFields = ['date_type', 'started_at', 'storage_note', 'package_note'] as const;
const dateNames: Record<string, string> = { purchase: '购买日期', production: '生产日期', prepared: '制作 / 切配日期', opened: '开封日期', unknown: '记录日期' };
// The server normalizes date-only input to midnight in Shanghai. Do not invent a purchase time.
export function actionContextDate(value: unknown): string {
  const text = readText(value);
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/);
  if (!match || !Number.isFinite(Date.parse(text))) return '';
  const [, year, month, day, hour, minute, second] = match;
  const days = new Date(Date.UTC(Number(year), Number(month), 0)).getUTCDate();
  if (+month < 1 || +month > 12 || +day < 1 || +day > days || +hour > 23 || +minute > 59 || +(second || 0) > 59) return '';
  const date = new Date(text);
  const time = date.toLocaleTimeString('en-GB', { timeZone: 'Asia/Shanghai', hour12: false });
  return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric',
    ...(time === '00:00:00' ? {} : { hour: '2-digit' as const, minute: '2-digit' as const, hour12: false }) });
}
function contextAcknowledgements(action: PendingAction): string[] | null {
  if (!action.required_ack_fields) return null;
  const p = action.proposed;
  const keys = [...new Set([...action.required_ack_fields, ...contextFields.filter(key => key in p)])];
  if (keys.length && p.action !== 'food_condition') return null;
  for (const key of keys) {
    if (!contextFields.some(field => field === key) || !(key in p)) return null;
    if (key === 'started_at' ? !actionContextDate(p[key]) : key === 'date_type' ? !Object.hasOwn(dateNames, readText(p[key])) : !readText(p[key])) return null;
  }
  return keys;
}
export function actionFromPayload(payload: unknown): PendingAction | null {
  const row = readObject(payload);
  return pendingAction(row.pending_action ?? row);
}
export function actionPath(recordId: string) { return `/actions/${encodeURIComponent(recordId)}`; }
export function requireAction(value: unknown, recordId: string): PendingAction {
  const action = pendingAction(value);
  if (!action || action.record_id !== recordId) throw new ApiError('INVALID_RESPONSE', '读取到的操作与这张确认卡不一致，请重新读取。', 200);
  return action;
}
export function actionExpired(action: PendingAction, now = Date.now()) {
  return action.status === 'expired' || (!!action.expires_at && Number.isFinite(Date.parse(action.expires_at)) && Date.parse(action.expires_at) <= now);
}
export function canConfirmAction(action: PendingAction, now = Date.now()) {
  const proposalValid = action.proposed.action === 'food_status'
    ? ['active', 'used', 'discarded'].includes(readText(action.proposed.status))
    : action.proposed.action === 'food_reminder'
      ? !action.proposed.remind_at || !!readDate(action.proposed.remind_at)
      : action.proposed.action === 'food_condition' && actionProposalLines(action).length > 0;
  return contextAcknowledgements(action) !== null && action.kind === 'food_action' && action.status === 'pending' && action.needs_confirmation && proposalValid &&
    !!readText(action.target.food_id) && ['food_status', 'food_condition', 'food_reminder'].includes(readText(action.proposed.action)) &&
    !!action.expires_at && Number.isFinite(Date.parse(action.expires_at)) && !actionExpired(action, now);
}
export function actionConfirmationBody(action: PendingAction, attempt: ActionAttempt, now = Date.now()) {
  if (!canConfirmAction(action, now) || action.record_id !== attempt.recordId || !attempt.key) throw new Error('这项修改目前无法确认，请重新读取状态。');
  const fields = contextAcknowledgements(action)!;
  return { confirm_key: 'user_confirmed' as const, idempotency_key: attempt.key, ...(fields.length ? { acknowledged_fields: fields } : {}) };
}
export function actionClosure(error: unknown): ActionClosure | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 404) return 'missing';
  if (error.status !== 409) return null;
  return error.code === 'ACTION_STALE' ? 'stale' : error.code === 'ACTION_EXPIRED' ? 'expired' : 'closed';
}
export function actionResultUncertain(error: unknown) {
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500 || error.code === 'INVALID_RESPONSE';
}
export const closureText: Record<ActionClosure, string> = {
  stale: '食材已在其他位置修改，这张确认卡已关闭。请重新发起修改并核对最新内容。',
  expired: '这项修改已过期，请重新发起并确认。',
  closed: '这项修改已结束，不能继续确认。可重新读取最终状态。',
  missing: '这项修改已不存在或无法访问，请重新发起。',
};
export function actionProposalLines(action: PendingAction): { label: string; value: string }[] {
  const p = action.proposed;
  const rows: { label: string; value: string }[] = [];
  if (p.action === 'food_status') rows.push({ label: '修改状态', value: foodStatusNames[readText(p.status)] || readText(p.status) || '未提供' });
  if (p.action === 'food_reminder') rows.push({ label: '提醒时间', value: readDate(p.remind_at) || (!p.remind_at ? '取消提醒' : '日期待核对') });
  // The service also applies supplied condition fields in a reminder action. Show all of them.
  if (p.action === 'food_condition' || p.action === 'food_reminder') {
    if ('started_at' in p) rows.push({ label: dateNames[readText(p.date_type)] || '记录日期', value: actionContextDate(p.started_at) || '日期待核对' });
    else if ('date_type' in p) rows.push({ label: '日期含义', value: dateNames[readText(p.date_type)] || '待核对' });
    for (const [key, label, names] of [
      ['storage', '存放方式', storageNames], ['condition', '加工状态', conditionNames], ['food_class', '食物类别', foodClassNames],
      ['location', '存放地点', { indoor: '室内', outdoor: '室外', unknown: '未知' }],
    ] as const) {
      const value = readText(p[key]);
      if (value) rows.push({ label, value: (names as Record<string, string>)[value] || value });
    }
    if (typeof p.opened === 'boolean') rows.push({ label: '开封状态', value: p.opened ? '已开封' : '未开封' });
    if (typeof p.temperature_c === 'number' && Number.isFinite(p.temperature_c)) rows.push({ label: '温度', value: `${p.temperature_c} ℃` });
    if (p.action === 'food_condition' && readText(p.remind_at)) rows.push({ label: '提醒时间', value: readDate(p.remind_at) || '日期待核对' });
    for (const [key, label] of [['storage_note', '存放情况'], ['package_note', '包装说明']] as const) {
      if (key in p) rows.push({ label, value: readText(p[key]) || '内容待核对' });
    }
  }
  return rows;
}
