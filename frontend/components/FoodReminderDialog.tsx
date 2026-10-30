'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { Bell, Check, LoaderCircle, X } from 'lucide-react';
import { ApiError, api } from '../lib/api';
import { localDate } from '../lib/food-forms';
import { reminderUpdate } from '../lib/food-reminder';
import type { Food } from '../lib/food-progress';
import NotificationSettings from './NotificationSettings';
import styles from './fridge-inventory.module.css';

type Props = {
  open: boolean;
  onClose: () => void;
  foods: Food[];
  onChanged: () => Promise<void>;
  initialFoodId?: string;
};

function ReminderForm({ foods, onChanged, initialFoodId }: Omit<Props, 'open' | 'onClose'>) {
  const [selected, setSelected] = useState(initialFoodId || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const sending = useRef(false);
  const food = foods.find(item => item.id === selected && item.status === 'active');

  async function save(form: HTMLFormElement) {
    if (!food || sending.current) return;
    const value = String(new FormData(form).get('reminder') || '');
    if (!value && !food.remind_at) { setError('请先选择提醒时间。'); return; }
    sending.current = true; setBusy(true); setError(''); setNotice('');
    let saved = false;
    try {
      const body = reminderUpdate(food, value);
      await api('/foods/' + encodeURIComponent(food.id), { method: 'PUT', body: JSON.stringify(body) });
      saved = true;
      setNotice(body.remind_at ? '提醒时间已保存。' : '这份食材的提醒已取消。');
      await onChanged();
    } catch (reason) {
      if (saved) setError('提醒已保存，库存刷新未完成。请刷新库存后核对。');
      else if (reason instanceof ApiError && (reason.status === 0 || reason.status >= 500)) setError('未收到保存结果，请先刷新库存核对提醒时间，避免反复提交。');
      else setError(reason instanceof Error ? reason.message : '提醒暂时没有保存，请重试。');
    } finally { sending.current = false; setBusy(false); }
  }

  return <>
    {foods.some(item => item.status === 'active') ? <form onSubmit={event => { event.preventDefault(); void save(event.currentTarget); }}>
      <fieldset className={styles.fieldset} disabled={busy}>
        <label>提醒哪份食材<select value={selected} onChange={event => { setSelected(event.target.value); setError(''); setNotice(''); }} required>
          <option value="">请选择食材</option>
          {foods.filter(item => item.status === 'active').map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        {food && <label key={food.id}>什么时候提醒<input name="reminder" type="datetime-local" defaultValue={localDate(food.remind_at)} required={!food.remind_at} /></label>}
      </fieldset>
      {food?.remind_at && <p className="small muted">清空时间后确认，可取消这份食材的提醒。</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="small" role="status">{notice}</p>}
      <p className="small muted">主动推送需先开启设备通知；保存提醒时间不代表消息已送达。</p>
      <button className="primary" type="submit" disabled={busy || !food}>
        {busy ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}确认提醒时间
      </button>
    </form> : <p className="small muted">先在冰箱里添加一份食材，再为它设置提醒。</p>}
    <details className={styles.notificationDetails}>
      <summary>设备通知与送达状态</summary>
      <NotificationSettings />
    </details>
  </>;
}

export default function FoodReminderDialog({ open, onClose, foods, onChanged, initialFoodId }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);

  return <dialog ref={dialog} className={styles.reminderDialog} aria-labelledby={titleId} onClose={onClose}
    onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}>
    <div className={styles.dialogContent}>
      <div className={styles.dialogHeading}>
        <div><span className="eyebrow"><Bell size={14} />记得这一份</span><h2 id={titleId}>食材提醒</h2></div>
        <button type="button" className="icon-button" aria-label="关闭食材提醒" onClick={() => dialog.current?.close()}><X size={19} /></button>
      </div>
      {open && <ReminderForm foods={foods} onChanged={onChanged} initialFoodId={initialFoodId} />}
    </div>
  </dialog>;
}
