'use client';

import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, ClipboardCheck, LoaderCircle, RefreshCw } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { draftPatches, editValues, foodDraftReady, foodDraftSummary, unappliedFoodFields, foodFieldLabels as labels, type DraftItem, type FoodEdit as Edit } from '../lib/food-forms';
import { foodConfirmationBody, prepareFoodConfirmation, type DraftConfirmationAttempt } from '../lib/food-advice';
import FoodAdvice from './FoodAdvice';
import ConsumptionPlan from './ConsumptionPlan';
import { consumptionPlanView } from '../lib/consumption-plan';
import styles from './food-draft.module.css';
import compactStyles from './chat-results-compact.module.css';

type Draft = { draft_id: string; status: string; expires_at?: string | null; advice_digest?: string | null; items: DraftItem[] };
type Confirmation = {
  status: string; results: { item_id: string; status: string; food_id?: string; error?: { code: string; message: string } | null }[];
};
function friendlyError(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 404) return '这份草稿已不存在或无法访问，请回到食材列表重新整理。';
    if (error.code === 'ITEM_SAVED') return '有食材已在另一处保存，请重新读取草稿；已保存食材请到电子冰箱修改。';
    if (error.code === 'DRAFT_CLOSED') return '这份草稿已结束，请重新读取查看保存结果。';
    if (error.code === 'IDEMPOTENCY_CONFLICT') return '草稿内容已发生变化，请重新读取核对后再确认。';
    if (error.code === 'DRAFT_CHANGED') return '草稿内容或期限依据已变化，请核对最新建议后再次确认。';
  }
  return error instanceof Error ? error.message : '暂时无法完成，请稍后重试。';
}

export default function FoodDraft({ draftId, onSaved, compact = false, onDetails }: { draftId: string; onSaved?: () => void; compact?: boolean; onDetails?: () => void }) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [itemErrors, setItemErrors] = useState<Record<string, string>>({});
  const [uncertain, setUncertain] = useState(false);
  const request = useRef<DraftConfirmationAttempt | null>(null);
  const busyRef = useRef(false);
  const currentId = useRef(draftId);
  currentId.current = draftId;

  function receive(value: Draft) {
    setDraft(value);
    setEdits(Object.fromEntries(value.items.map(item => [item.item_id, editValues(item)])));
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setNotice(''); setItemErrors({}); setUncertain(false);
    setDraft(null); request.current = null;
    api<Draft>(`/foods/drafts/${encodeURIComponent(draftId)}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) receive(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(friendlyError(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [draftId]);

  async function refresh() {
    if (busyRef.current) return;
    const targetId = draftId;
    setLoading(true); setError('');
    try {
      const value = await api<Draft>(`/foods/drafts/${encodeURIComponent(targetId)}`);
      if (currentId.current === targetId) receive(value);
    }
    catch (reason) { if (currentId.current === targetId) setError(friendlyError(reason)); }
    finally { if (currentId.current === targetId) setLoading(false); }
  }

  const dirty = draft?.items.some(item => !item.saved_food_id && JSON.stringify(edits[item.item_id] ?? editValues(item)) !== JSON.stringify(editValues(item))) ?? false;

  async function updatePreview() {
    if (!draft || busyRef.current || uncertain) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice('');
    const targetId = draftId;
    try {
      const updates = draft.items.filter(item => !item.saved_food_id).map(item => ({
        item, changes: draftPatches(item, edits[item.item_id] ?? editValues(item)),
      }));
      for (const { item, changes } of updates) {
        if (currentId.current !== targetId) return;
        if (Object.keys(changes).length) await api(`/foods/drafts/${encodeURIComponent(targetId)}/items/${encodeURIComponent(item.item_id)}`, {
          method: 'PUT', body: JSON.stringify(changes),
        });
      }
      const latest = await api<Draft>(`/foods/drafts/${encodeURIComponent(targetId)}`);
      if (currentId.current !== targetId) return;
      const unapplied = updates.flatMap(({ item, changes }) => {
        const received = latest.items.find(row => row.item_id === item.item_id);
        return received ? unappliedFoodFields(changes, received as unknown as Record<string, unknown>) : ['草稿项'];
      });
      setDraft(latest); request.current = null;
      if (unapplied.length) {
        setError(`服务器尚未应用这些修改：${[...new Set(unapplied)].join('、')}。你的输入仍保留，暂不能按这些新条件确认。`);
      } else {
        receive(latest);
        setNotice('修改已更新，可以确认存入冰箱。');
      }
    } catch (reason) {
      if (currentId.current === targetId) setError(friendlyError(reason));
    } finally { busyRef.current = false; setBusy(false); }
  }

  async function confirm() {
    if (!draft || busyRef.current) return;
    if (dirty && !request.current) { setError('请先更新修改，再确认保存。'); return; }
    if (draft.items.some(item => !item.saved_food_id && !foodDraftReady(item))) { setError('请补上食材名称，其他信息可以之后再填。'); return; }
    busyRef.current = true; setBusy(true); setError(''); setNotice('');
    const targetId = draftId;
    let sentConfirmation = false;
    try {
      const latest = draft;
      if (!request.current) {
        request.current = prepareFoodConfirmation(latest, () => crypto.randomUUID());
      }
      // A response lost in transit must be retried with exactly the same key and items.
      const operation = request.current;
      sentConfirmation = true;
      const result = await api<Confirmation>(`/foods/drafts/${encodeURIComponent(targetId)}/confirm`, {
        method: 'POST', body: JSON.stringify(foodConfirmationBody(operation)),
      });
      if (currentId.current !== targetId) return;
      request.current = null; setUncertain(false);
      const saved = result.results.filter(item => item.status === 'saved');
      const failures = result.results.filter(item => item.status !== 'saved');
      const savedById = new Map(saved.map(item => [item.item_id, item.food_id]));
      const next = { ...latest, advice_digest: null, status: result.status, items: latest.items.map(item => savedById.has(item.item_id) ? { ...item, saved_food_id: savedById.get(item.item_id) } : item) };
      receive(next);
      setItemErrors(Object.fromEntries(failures.map(item => [item.item_id, item.error?.message || '暂未保存，请核对后再试。'])));
      setNotice(result.status === 'confirmed' ? '整批食材已存入电子冰箱。' : `${saved.length} 条已保存，剩余 ${next.items.filter(item => !item.saved_food_id).length} 条请补全或重试。`);
      if (saved.length) onSaved?.();
      // The digest for a partially saved batch must come from the server's latest view.
      try {
        const refreshed = await api<Draft>(`/foods/drafts/${encodeURIComponent(targetId)}`);
        if (currentId.current === targetId) receive(refreshed);
      } catch {
        if (currentId.current === targetId) setError('保存结果已收到，但最新建议读取失败。请重新读取后再操作剩余食材。');
      }
    } catch (reason) {
      if (currentId.current !== targetId) return;
      if (reason instanceof ApiError && reason.code === 'DRAFT_CHANGED') {
        request.current = null; setUncertain(false);
        setDraft(previous => previous ? { ...previous, advice_digest: null } : previous);
        setError(friendlyError(reason));
        try {
          const refreshed = await api<Draft>(`/foods/drafts/${encodeURIComponent(targetId)}`);
          if (currentId.current === targetId) { receive(refreshed); setNotice('已读取最新草稿；本次没有继续保存，请重新核对后确认。'); }
        } catch { if (currentId.current === targetId) setError('建议已变化，最新草稿读取失败。请重新读取后核对；没有自动保存。'); }
        return;
      }
      const isUnknown = sentConfirmation && (!(reason instanceof ApiError) || reason.status === 0 || reason.status >= 500 || reason.code === 'INVALID_RESPONSE');
      if (!isUnknown) request.current = null;
      setUncertain(isUnknown);
      setError(isUnknown ? '未收到保存结果，食材可能已保存。请点击“重新获取本次保存结果”，会安全重试，不会重复添加。' : friendlyError(reason));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function change(id: string, field: string, value: string) {
    setEdits(previous => ({ ...previous, [id]: { ...previous[id], [field]: value } }));
    request.current = null;
    setItemErrors(previous => { const next = { ...previous }; delete next[id]; return next; });
    setNotice('');
  }

  const remaining = draft?.items.filter(item => !item.saved_food_id).length ?? 0;
  const missingName = draft?.items.some(item => !item.saved_food_id && !foodDraftReady(item)) ?? false;
  const closed = draft && !['pending_confirmation', 'partially_saved'].includes(draft.status);

  if (compact) return <section className={compactStyles.confirmation} aria-label="食材保存确认">
    <div className={compactStyles.heading}><strong>{closed ? '食材记录' : '确认存入冰箱？'}</strong>{onDetails&&<button type="button" className="text-button" onClick={onDetails}>查看详情</button>}</div>
    {loading ? <p role="status">正在读取食材…</p> : draft&&<p>{draft.items.map(item=>item.name||'待补名称').join('、')}</p>}
    {error&&<p role="alert" className="error">{error}</p>}
    {notice&&<p role="status">{notice}</p>}
    {Object.entries(itemErrors).map(([id,message])=><p key={id} role="alert" className="error">{message}</p>)}
    {closed&&!notice&&<p className={compactStyles.meta}>{draft.status==='confirmed'?'已存入冰箱。':'这份草稿已结束，可继续对话重新整理。'}</p>}
    {!loading&&remaining>0&&!closed&&<div className={compactStyles.actions}>
      <button type="button" className="primary" disabled={busy||(!uncertain&&(dirty||missingName||!draft?.advice_digest))} onClick={confirm}>{busy?'正在保存…':uncertain?'核对本次保存结果':'确认保存'}</button>
      <button type="button" className="text-button" disabled={busy||uncertain} onClick={refresh}>刷新状态</button>
    </div>}
  </section>;

  return <section className={styles.draft} aria-label="核对食材草稿">
    <div className={styles.heading}>
      <div><span className={styles.eyebrow}><ClipboardCheck size={15} />食材核对</span><h3>记下这些食材？</h3></div>
      <button className={styles.refresh} type="button" disabled={busy || loading || uncertain} onClick={refresh} title="重新读取会放弃尚未提交的修改"><RefreshCw size={15} />重新读取</button>
    </div>
    <p className={styles.explanation}>看一眼名称，确认即可。存放方式和日期可以之后补。</p>
    {loading && <p className={styles.loading} role="status"><LoaderCircle size={16} className="spin" />正在读取草稿…</p>}
    {error && <p className={styles.error} role="alert">{error}</p>}
    {notice && <p className={styles.notice} role="status">{notice}</p>}
    {!loading && draft && <>
      <div className={styles.items}>{draft.items.map((item, index) => {
        const values = edits[item.item_id] ?? editValues(item);
        const saved = !!item.saved_food_id;
        const hasEatingPlan = !!consumptionPlanView(item.consumption_plan)?.target;
        const disabled = busy || uncertain || saved || !!closed;
        const input = (field: string, type = 'text', extra: Record<string, unknown> = {}) => <label>{labels[field]}{field === 'name' && <span className={styles.required}> *</span>}<input type={type} value={values[field]} disabled={disabled} onChange={event => change(item.item_id, field, event.target.value)} {...extra} /></label>;
        const select = (field: string, choices: [string, string][]) => <label>{labels[field]}<select value={values[field]} disabled={disabled} onChange={event => change(item.item_id, field, event.target.value)}>{choices.map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>;
        return <article key={item.item_id} className={`${styles.item} ${saved ? styles.saved : ''}`}>
          <div className={styles.itemHeading}><strong>{index + 1}. {values.name || '待补名称'}</strong><span className={saved ? styles.savedBadge : styles.badge}>{saved ? <><Check size={13} />已保存</> : foodDraftReady(item) ? '待确认' : '请补名称'}</span></div>
          <p className={styles.itemSummary}>{foodDraftSummary(item)}</p>
          {!saved&&<ConsumptionPlan plan={item.consumption_plan} name={item.name} stale={JSON.stringify(values)!==JSON.stringify(editValues(item))}/>}
          {saved ? <>
            <p className={styles.explanation}>已存入电子冰箱，后续可在那里补充。</p>
          </> : <>
            {itemErrors[item.item_id] && <p className={styles.error} role="alert">{itemErrors[item.item_id]}</p>}
            <details className={styles.details} open={!values.name.trim() || undefined}><summary>补充或修改信息（可选）<ChevronDown size={14} /></summary>
            <div className={styles.grid}>
              {input('name', 'text', { maxLength: 100, placeholder: '如：豆腐' })}
              {select('storage', [['unknown', '稍后补充'], ['ambient', '常温'], ['refrigerated', '冷藏'], ['frozen', '冷冻']])}
              {select('food_class', [['unknown', '尚未确定'], ['fresh', '新鲜食材'], ['cooked', '自制熟食 / 剩菜'], ['packaged', '包装成品 / 半成品']])}
              {select('condition', [['unknown', '尚未确定'], ['raw', '未加工 / 生'], ['cut', '已切配'], ['cooked', '已熟制']])}
              {select('opened', [['', '未知 / 不适用'], ['false', '未开封'], ['true', '已开封']])}
              {select('date_type', [['unknown', '暂不确定'], ['purchase', '购买日期'], ['production', '生产日期'], ['prepared', '制作日期'], ['opened', '开封日期']])}
              {input('started_at', 'datetime-local')}
              {select('location', [['unknown', '尚未确定'], ['indoor', '室内'], ['outdoor', '室外']])}
            </div>
            {['opened', 'unopened'].includes(item.condition || '') && <p className={styles.source}>旧记录只记了开封状态，加工状态需要另行核对；不会自动当作生鲜。</p>}
            {item.date_source === 'relative' && <div className={styles.dateContext}>
              <p>原日期描述：{item.date_phrase} · 时区 {item.date_timezone || '待核对'}</p>
              <p>{item.time_precision === 'date' ? '原描述仅确定日期，未提供具体时刻。' : item.time_precision === 'period' ? '原描述只提供时段，解析值为时段起点。' : '原描述已提供时刻。'}当前记录起点以表单为准，原描述保留供核对。</p>
              {values.started_at !== editValues(item).started_at ? <p>当前起点已手动修改，请再次核对。</p> : (item.date_warnings || []).map((warning, index) => <p key={index}>{warning}</p>)}
            </div>}
            <div className={styles.grid}>
              {input('expires_at', 'datetime-local')}
              {select('deadline_source', [['unknown', '暂不设置期限'], ['package', '依据适用包装说明'], ['user', '我自己设置']])}
              {input('remind_at', 'datetime-local')}
              {input('temperature_c', 'number', { min: -80, max: 70, step: 'any', placeholder: '已知时填写，℃' })}
              <label className={styles.wide}>适用包装说明<textarea value={values.package_note} maxLength={1000} disabled={disabled} placeholder="如包装对冷藏、开封后的说明" onChange={event => change(item.item_id, 'package_note', event.target.value)} /></label>
            </div>
            <p className={styles.source}>缺少储存条件或依据时，先保存记录，不推算期限。</p>
            </details>
          </>}
          {!hasEatingPlan&&<details className={styles.details}><summary>储存建议与依据<ChevronDown size={14} /></summary><FoodAdvice suggestion={item.suggestion} stale={!saved && JSON.stringify(values) !== JSON.stringify(editValues(item))} /></details>}
        </article>;
      })}</div>
      <p className={styles.timeNote}>日期按此设备的时区保存；预计期限不代表实际新鲜度。</p>
      {remaining > 0 && !closed && <>
        {dirty && !uncertain && <button type="button" className={styles.confirm} disabled={busy || loading} onClick={updatePreview}>{busy ? '正在更新…' : '更新修改'}</button>}
        {!dirty && !draft.advice_digest && !uncertain && <p className={styles.error}>尚未取得本次建议的核对凭据，暂不能确认。请重新读取草稿。</p>}
        <button type="button" className={styles.confirm} disabled={busy || loading || (!uncertain && (dirty || missingName || !draft.advice_digest))} onClick={confirm}>
          {busy ? <><LoaderCircle size={16} className="spin" />处理中…</> : uncertain ? '重新获取本次保存结果' : draft.status === 'partially_saved' ? `重试剩余 ${remaining} 条` : `确认存入冰箱 · ${remaining} 条`}
        </button>
      </>}
      {closed && remaining > 0 && <p className={styles.explanation}>这份草稿已结束，请重新整理未保存的食材。</p>}
    </>}
  </section>;
}
