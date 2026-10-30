'use client';

import { useEffect, useRef, useState } from 'react';
import { Check, ClipboardCheck, LoaderCircle, RefreshCw } from 'lucide-react';
import { api } from '../lib/api';
import { actionClosure, actionConfirmationBody, actionExpired, actionFromPayload, actionPath, actionProposalLines, actionResultUncertain, canConfirmAction, closureText, requireAction, type ActionAttempt, type ActionClosure, type PendingAction } from '../lib/chat-actions';
import { readDate, readText } from '../lib/chat-read';
import { consumptionPlanView, planDate, type ConsumptionPlan as Plan } from '../lib/consumption-plan';
import ConsumptionPlan from './ConsumptionPlan';
import styles from './chat-results.module.css';
import compactStyles from './chat-results-compact.module.css';

export type ChatActionProps = { payload: unknown; disabled?: boolean; onUpdated?: (action: PendingAction) => void | Promise<void>; compact?: boolean; onDetails?: () => void };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '操作没有完成，请重新读取状态。';

export default function ChatAction({ payload, disabled = false, onUpdated, compact = false, onDetails }: ChatActionProps) {
  const snapshot = actionFromPayload(payload);
  if (!snapshot) return <section className={styles.card}><h3><ClipboardCheck size={17}/>修改确认</h3><p className={styles.notice}>这张卡片缺少可恢复的操作记录，请重新发起修改。</p></section>;
  // A new record gets isolated request state even if React reuses the outer card.
  return <PersistedAction key={snapshot.record_id} recordId={snapshot.record_id} disabled={disabled} onUpdated={onUpdated} compact={compact} onDetails={onDetails}/>;
}

function PersistedAction({ recordId, disabled, onUpdated, compact, onDetails }: { recordId: string; disabled: boolean; onUpdated?: ChatActionProps['onUpdated']; compact?: boolean; onDetails?: () => void }) {
  const [action, setAction] = useState<PendingAction | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [closure, setClosure] = useState<ActionClosure | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [clock, setClock] = useState(() => Date.now());
  const mounted = useRef(true);
  const busyRef = useRef(false);
  const attempt = useRef<ActionAttempt | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    mounted.current = true;
    // Mount, refresh, and history restoration only read; they can never confirm an operation.
    void api<unknown>(actionPath(recordId), { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) setAction(requireAction(data, recordId));
    }).catch(reason => {
      if (!controller.signal.aborted) { setError(errorMessage(reason)); setClosure(actionClosure(reason)); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { mounted.current = false; controller.abort(); };
  }, [recordId]);

  useEffect(() => {
    if (!action || action.status !== 'pending') return;
    const end = Date.parse(action.expires_at);
    if (!Number.isFinite(end)) return;
    const timer = setTimeout(() => setClock(Date.now()), Math.max(0, Math.min(end - Date.now() + 20, 2147483647)));
    return () => clearTimeout(timer);
  }, [action]);

  function received(value: unknown) {
    const latest = requireAction(value, recordId);
    setAction(latest); setClock(Date.now());
    if (latest.status !== 'pending') {
      attempt.current = null; setUncertain(false);
      // An optional page refresh cannot change the confirmed result into an API failure.
      try { void Promise.resolve(onUpdated?.(latest)).catch(() => {}); } catch { /* The persistent action remains the source of truth. */ }
    }
    return latest;
  }

  async function refresh() {
    if (busyRef.current) return;
    busyRef.current = true; setLoading(true); setError(''); setNotice('');
    try {
      const data = await api<unknown>(actionPath(recordId));
      if (!mounted.current) return;
      const latest = received(data);
      setUncertain(false);
      if (latest.status === 'pending' && !closure) setNotice('已读取最新修改内容。请核对后，再点击确认。');
    } catch (reason) {
      if (!mounted.current) return;
      setError(errorMessage(reason));
      const closed = actionClosure(reason);
      if (closed) setClosure(closed);
    } finally { busyRef.current = false; if (mounted.current) setLoading(false); }
  }

  async function mutate(operation: 'confirm' | 'cancel') {
    if (!action || disabled || busyRef.current || loading || closure || uncertain || action.status !== 'pending' || actionExpired(action)) return;
    if (operation === 'confirm' && !canConfirmAction(action)) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice('');
    try {
      let data: unknown;
      if (operation === 'confirm') {
        // This is the only confirmation call site, invoked by the visible button below.
        attempt.current ??= { recordId, key: crypto.randomUUID() };
        const body = actionConfirmationBody(action, attempt.current);
        data = await api<unknown>(actionPath(recordId) + '/confirm', { method: 'POST', body: JSON.stringify(body) });
      } else data = await api<unknown>(actionPath(recordId), { method: 'DELETE' });
      if (!mounted.current) return;
      const latest = received(data);
      if (latest.status === 'pending') { setUncertain(true); setError('服务端未返回结束状态，请重新读取后核对。'); }
    } catch (reason) {
      if (!mounted.current) return;
      const closed = actionClosure(reason);
      if (closed) {
        setClosure(closed); setUncertain(false); attempt.current = null; setError(closureText[closed]);
      } else {
        const unknown = actionResultUncertain(reason);
        setUncertain(unknown);
        if (!unknown) attempt.current = null;
        setError(unknown ? '尚未收到确定结果，修改可能已完成。请先重新读取状态，再决定是否继续。' : errorMessage(reason));
      }
    } finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  }

  const expired = action ? actionExpired(action, clock) : false;
  const closed = action?.status === 'pending' ? closure : null;
  const confirmed = action?.status === 'confirmed';
  const cancelled = action?.status === 'cancelled';
  const enabled = action && canConfirmAction(action, clock) && !disabled && !busy && !loading && !closed && !uncertain;
  const canCancel = action?.status === 'pending' && !expired && !disabled && !busy && !loading && !closed && !uncertain;
  const lines = action ? actionProposalLines(action) : [];
  const pendingPlan = action?.status === 'pending' && !expired && !closed ? action.proposed._consumption_plan as Plan | undefined : undefined;
  const plan = consumptionPlanView(pendingPlan, clock);
  const title = action?.proposed.action === 'food_status' ? '确认修改食材状态' : action?.proposed.action === 'food_condition' ? '确认更新食材' : action?.proposed.action === 'food_reminder' ? '确认修改食材提醒' : '食材修改确认';
  if (compact) return <section className={compactStyles.confirmation} aria-label="食材修改确认">
    <div className={compactStyles.heading}><strong>{title}</strong>{onDetails&&<button type="button" className="text-button" onClick={onDetails}>查看详情</button>}</div>
    {loading&&<p role="status">正在读取最新状态…</p>}
    {action&&<p>{readText(action.target.name)}{lines.length>0?' · '+lines.map(line=>`${line.label}：${line.value}`).join('；'):''}</p>}
    {plan?.target&&<p className={compactStyles.meta}>{plan.conservative?'保守安排':'建议食用'}：{planDate(plan.target)}{plan.conservative?'，优先安排下一餐，非保质期。':'前。'}{plan.due?'已到目标时间，请先核对当前情况。':''}</p>}
    {confirmed&&<p role="status">修改已保存。</p>}{cancelled&&<p role="status">修改已取消。</p>}
    {closed&&<p>{closureText[closed]}</p>}{expired&&!closed&&<p>确认已过期，请继续对话重新发起。</p>}
    {disabled&&<p>这次对话未完成，请重新发起修改。</p>}
    {action?.status==='pending'&&!expired&&!closed&&!canConfirmAction(action,clock)&&<p>修改内容暂无法完整显示，请重新发起后核对。</p>}
    {error&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status">{notice}</p>}
    <div className={compactStyles.actions}>
      {action?.status==='pending'&&!expired&&!closed&&<><button type="button" className="primary" disabled={!enabled} onClick={()=>void mutate('confirm')}>{busy?'正在保存…':'确认修改'}</button><button type="button" className="text-button" disabled={!canCancel} onClick={()=>void mutate('cancel')}>取消</button></>}
      {(error||uncertain)&&<button type="button" className="text-button" disabled={busy||loading||closure==='missing'} onClick={()=>void refresh()}>刷新状态</button>}
    </div>
  </section>;
  return <section className={styles.card} aria-label="持久化修改确认">
    <div className={styles.heading}><h3><ClipboardCheck size={17}/>{title}</h3>{action && <span className={styles.badge}>{confirmed ? '已确认执行' : cancelled ? '已取消' : closed ? '原操作已关闭' : expired ? '已过期' : action.status === 'pending' ? '待你确认' : '操作已结束'}</span>}</div>
    {loading && <p className={styles.meta} role="status"><LoaderCircle className="spin" size={14}/>正在读取最新状态…</p>}
    {action && <>
      <h4>{readText(action.target.name) || '食材名称未提供'}</h4>
      {lines.length > 0 && <dl className={styles.facts}>{lines.map(line => <div key={line.label}><dt>{line.label}</dt><dd>{line.value}</dd></div>)}</dl>}
      {pendingPlan&&<ConsumptionPlan plan={pendingPlan} name={readText(action.target.name)}/>}
      {confirmed ? <p className={styles.closed} role="status"><Check size={15}/>服务端已确认执行这项修改。</p>
        : cancelled ? <p className={styles.closed} role="status">这项修改已取消。</p>
        : closed ? <p className={styles.notice}>{closureText[closed]}</p>
        : expired ? <p className={styles.notice}>{closureText.expired}</p>
        : <><p className={styles.note}>核对以上内容后点击确认，才会修改正式食材记录。对话回复和重新读取都不会替你确认。</p>{readDate(action.expires_at) && <p className={styles.meta}>确认有效至 {readDate(action.expires_at)}</p>}</>}
      {action.status === 'pending' && !expired && !closed && !canConfirmAction(action, clock) && <p className={styles.notice}>这项修改的信息不完整，暂不能确认，请重新发起。</p>}
    </>}
    {disabled && <p className={styles.notice}>本轮对话未完整结束。请重新发起修改并核对后再确认。</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {notice && <p className={styles.note} role="status">{notice}</p>}
    <div className={styles.actions}>
      {action?.status === 'pending' && !expired && !closed && <><button type="button" className="primary" disabled={!enabled} onClick={() => void mutate('confirm')}>{busy ? <LoaderCircle className="spin" size={14}/> : <Check size={14}/>}确认修改</button><button type="button" className="secondary" disabled={!canCancel} onClick={() => void mutate('cancel')}>取消这次修改</button></>}
      <button type="button" className="text-button" disabled={busy || loading || closure === 'missing'} onClick={() => void refresh()}><RefreshCw size={13}/>重新读取状态</button>
    </div>
  </section>;
}
