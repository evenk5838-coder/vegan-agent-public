'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpRight, BookOpen, Leaf, LoaderCircle, RefreshCw, Search } from 'lucide-react';
import { api } from '../lib/api';
import { guidanceExpired, guidanceFailure, storageGuidanceView, type StorageGuidance as Guidance } from '../lib/storage-guidance';
import styles from './storage-guidance.module.css';
import ConsumptionPlan from './ConsumptionPlan';
import type { ConsumptionPlan as Plan } from '../lib/consumption-plan';

export function StorageGuidanceContent({ value }: { value: Guidance }) {
  return <div className={styles.result}>
    <p className={styles.summary}>{value.summary}</p>
    {value.status === 'ready' && <>
      {value.conditions.length > 0 && <details className={styles.conditions}><summary>建议的适用条件</summary><ul>{value.conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul></details>}
      <details className={styles.sources} aria-label="建议来源"><summary><BookOpen size={13}/>参考资料</summary>{value.sources.map(source => <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer">{source.title}<ArrowUpRight size={14}/></a>)}</details>
      <p className={styles.meta}>资料参考，请结合包装和实际储存情况判断。</p>
      <time className={styles.meta} dateTime={value.generatedAt}>{new Date(value.generatedAt).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}{value.cached ? ' · 已保存的建议' : ' · 本次查询'}</time>
    </>}
  </div>;
}

/** Remount on a changed food/context; old requests cannot repaint the new record. */
export default function StorageGuidance({ foodId, contextKey = '', onUpdated }: { foodId: string; contextKey?: string; onUpdated?: () => Promise<void> }) {
  return <GuidanceSession key={foodId + ':' + contextKey} foodId={foodId} onUpdated={onUpdated}/>;
}
function GuidanceSession({ foodId, onUpdated }: { foodId: string; onUpdated?: () => Promise<void> }) {
  const [phase, setPhase] = useState<'reading' | 'idle' | 'searching' | 'error'>('reading');
  const [value, setValue] = useState<Guidance | null>(null);
  const [failure, setFailure] = useState<{ message: string; terminal: boolean } | null>(null);
  const [expired, setExpired] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [syncError, setSyncError] = useState('');
  const onUpdatedRef = useRef(onUpdated);
  useEffect(() => { onUpdatedRef.current = onUpdated; }, [onUpdated]);
  const requestId = useRef(0);
  const running = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const request = useCallback((search: boolean) => {
    if (running.current || !foodId) return;
    running.current = true;
    const id = ++requestId.current;
    const abort = new AbortController(); controller.current = abort;
    void api<unknown>('/foods/' + encodeURIComponent(foodId) + '/storage-guidance', {
      method: search ? 'POST' : 'GET', signal: AbortSignal.any([abort.signal, AbortSignal.timeout(search ? 55000 : 20000)]),
    }).then(async result => {
      if (id !== requestId.current) return;
      const current = storageGuidanceView(result, foodId);
      // Only the outer plan reflects current food conditions. The nested one is historical.
      setPlan((result as { consumption_plan?: Plan | null }).consumption_plan ?? null);
      setValue(current); setExpired(current ? guidanceExpired(current) : false); setPhase('idle');
      setSyncError('');
      try { await onUpdatedRef.current?.(); }
      catch { if (id === requestId.current) setSyncError('建议已读取，库存刷新失败。请关闭后刷新库存。'); }
    }).catch(error => {
      if (id !== requestId.current) return;
      setFailure(guidanceFailure(error, search)); setPhase('error');
    }).finally(() => {
      if (id === requestId.current) running.current = false;
    });
  }, [foodId]);
  const start = useCallback((search: boolean) => {
    if (running.current) return;
    setPhase(search ? 'searching' : 'reading'); setFailure(null); setValue(null); setPlan(null); setExpired(false); setSyncError('');
    void request(search);
  }, [request]);
  useEffect(() => {
    const sequence = requestId;
    void request(false); // Cache only. Paid search is exclusively an explicit click.
    return () => { ++sequence.current; controller.current?.abort(); running.current = false; };
  }, [request]);
  useEffect(() => {
    if (!value) return;
    const check = () => setExpired(guidanceExpired(value));
    const timer = window.setInterval(check, 30000);
    return () => window.clearInterval(timer);
  }, [value]);
  // Returning to an old chat/tab validates the current food conditions without paying.
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') start(false); };
    window.addEventListener('focus', refresh); document.addEventListener('visibilitychange', refresh);
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); };
  }, [start]);
  const busy = phase === 'reading' || phase === 'searching';
  return <section className={styles.root} aria-label="储存建议" aria-busy={busy}>
    <div className={styles.heading}><span className={styles.icon}><Leaf size={18}/></span><h3>{value?.name ? `${value.name} · 储存建议` : '储存建议'}</h3>
      {!busy && !failure?.terminal && <button type="button" className={styles.refresh} aria-label="读取最新建议" onClick={() => start(false)}><RefreshCw size={15}/></button>}</div>
    {busy && <p className={styles.loading} role="status"><LoaderCircle size={17} className="spin"/>{phase === 'searching' ? '正在查找资料并整理建议…' : '正在读取已保存的建议…'}</p>}
    {failure && <div role="alert" className={styles.error}><p>{failure.message}</p>{!failure.terminal && <button type="button" className="text-button" onClick={() => start(false)}>读取查询结果<RefreshCw size={14}/></button>}</div>}
    {!busy && !failure && <>
      <ConsumptionPlan plan={plan} name={value?.name}/>
      {syncError&&<p role="status" className={styles.meta}>{syncError}</p>}
      {value && !expired ? <StorageGuidanceContent value={value}/> : <p className={styles.intro}>{expired ? '上次建议已过期，可按当前记录重新查询。' : '查一查怎么存放、哪些条件需要留意。'}</p>}
      {(!value || value.status === 'no_reliable_sources' || expired) && <button type="button" className={`primary ${styles.search}`} onClick={() => start(true)}><Search size={16}/>{value ? '重新查询' : '联网查建议'}</button>}
    </>}
  </section>;
}
