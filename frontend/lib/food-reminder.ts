import type { Food } from './food-progress';

/** A reminder edit preserves the recorded storage conditions and expiry basis. */
export function reminderUpdate(food: Food, localTime: string, now = Date.now()) {
  const text = localTime.trim();
  if (!text) return { ...food, remind_at: null };
  const due = new Date(text);
  if (Number.isNaN(due.getTime())) throw new Error('请选择有效的提醒时间。');
  if (due.getTime() <= now) throw new Error('请选择之后的提醒时间。');
  const deadline = food.expires_at ? Date.parse(food.expires_at) : NaN;
  if (Number.isFinite(deadline) && due.getTime() > deadline) throw new Error('提醒不能晚于这份食材已设置的日期，请先核对期限。');
  return { ...food, remind_at: due.toISOString() };
}
