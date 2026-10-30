'use client';

import { useRef, useState } from 'react';
import { ClipboardCheck, LoaderCircle } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { foodEntryDraftBody, foodFieldLabels } from '../lib/food-forms';
import FoodDraft from './FoodDraft';
import styles from './food-entry.module.css';

export default function FoodEntry({ onSaved, onDraftCreated }: {
  onSaved: () => void | Promise<void>; onDraftCreated?: () => void;
}) {
  const [draftId, setDraftId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const creating = useRef(false);

  async function prepare(form: HTMLFormElement) {
    if (creating.current) return;
    creating.current = true; setBusy(true); setError('');
    try {
      const data = new FormData(form);
      const input = Object.fromEntries(Object.keys(foodFieldLabels).map(key => [key, String(data.get(key) || '').trim()]));
      // Unknown selections remain unknown instead of becoming an invented input.
      input.food_class ||= 'unknown'; input.location ||= 'unknown'; input.deadline_source ||= 'unknown';
      const result = await api<{ draft_id: string }>('/foods/drafts', { method: 'POST', body: JSON.stringify(foodEntryDraftBody(input)) });
      if (!result.draft_id) throw new Error('没有收到可读取的草稿编号，请刷新待核对列表查看。');
      setDraftId(result.draft_id); onDraftCreated?.();
    } catch (reason) {
      const uncertain = reason instanceof ApiError && (reason.status === 0 || reason.status >= 500);
      setError(uncertain ? '未收到整理结果，草稿可能已建立。请先查看待核对列表；当前没有确认保存正式食材。' : reason instanceof Error ? reason.message : '暂时无法整理，请重试。');
    } finally { creating.current = false; setBusy(false); }
  }

  return <section className="panel">
    <h3>{draftId ? '核对这份食材' : '记下一份食材'}</h3>
    <p className="small muted">只写名称也能记下，其他信息知道时再补。</p>
    {error && <p role="alert" className="error">{error}</p>}
    {draftId ? <FoodDraft draftId={draftId} onSaved={() => { void Promise.resolve(onSaved()).catch(reason => setError(reason instanceof Error ? reason.message : '食材已保存，库存刷新失败，请手动刷新。')); }} /> : <form onSubmit={event => { event.preventDefault(); void prepare(event.currentTarget); }}>
      <fieldset disabled={busy} className={styles.fields}>
        <label>食材名称<input name="name" required maxLength={100} placeholder="例如：豆腐、燕麦、昨天做的炖菜" /></label>
        <details><summary>补充信息（可选）</summary><div className={`form-grid ${styles.grid}`}>
          <label>存放方式（可选）<select name="storage" defaultValue="unknown"><option value="unknown">稍后补充</option><option value="refrigerated">冷藏</option><option value="frozen">冷冻</option><option value="ambient">常温</option></select></label>
          <label>已知日期（可选）<input name="started_at" type="datetime-local" /></label>
          <label>这是什么日期<select name="date_type" defaultValue="unknown"><option value="unknown">暂不确定</option><option value="purchase">购买日期</option><option value="production">生产日期</option><option value="prepared">制作日期</option><option value="opened">开封日期</option></select></label>
          <label>食物类别<select name="food_class" defaultValue="unknown"><option value="unknown">尚未确定</option><option value="fresh">新鲜食材</option><option value="cooked">自制熟食 / 剩菜</option><option value="packaged">包装成品 / 半成品</option></select></label>
          <label>加工状态<select name="condition" defaultValue="unknown"><option value="unknown">尚未确定</option><option value="raw">未加工 / 生</option><option value="cut">已切配</option><option value="cooked">已熟制</option></select></label>
          <label>是否开封<select name="opened" defaultValue=""><option value="">未知 / 不适用</option><option value="false">未开封</option><option value="true">已开封</option></select></label>
          <label>存放地点<select name="location" defaultValue="unknown"><option value="unknown">尚未确定</option><option value="indoor">室内</option><option value="outdoor">室外</option></select></label>
          <label>温度 ℃<input name="temperature_c" type="number" min={-80} max={70} step="0.1" placeholder="不知道可留空" /></label>
          <label>期限依据<select name="deadline_source" defaultValue="unknown"><option value="unknown">暂不设置日期</option><option value="package">包装明确标示</option><option value="user">我自行设置日期</option></select></label>
          <label>所设日期<input name="expires_at" type="datetime-local" /></label>
          <label>提醒时刻<input name="remind_at" type="datetime-local" /></label>
        </div><label>适用包装说明<input name="package_note" maxLength={1000} placeholder="请核对开封状态与存放条件" /></label></details>
      </fieldset>
      <button className="primary" disabled={busy}>{busy ? <LoaderCircle size={16} className="spin" /> : <ClipboardCheck size={16} />}下一步：确认食材</button>
    </form>}
  </section>;
}
