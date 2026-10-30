'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ClipboardList, LoaderCircle, RefreshCw } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { recoveredRecipeCandidates } from '../lib/recipe-candidates';
import RecipeCandidates from './RecipeCandidates';
import styles from './recipe-candidates.module.css';

/** Recover saved search candidates, without repeating a search or creating a recipe. */
export default function RecipeCandidateInbox({ current, disabled = false, onSaved }: { current?: Record<string, unknown> | null; disabled?: boolean; onSaved: () => void }) {
  const [recovered, setRecovered] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setLoading(true); setError('');
    try {
      const data = await api<{ items: unknown[] }>('/recipes/candidates', { signal: request.signal });
      if (request.signal.aborted || !mounted.current) return;
      if (!Array.isArray(data.items)) throw new ApiError('INVALID_RESPONSE', '候选列表暂时无法读取。', 200);
      setRecovered(data.items);
    } catch (reason) { if (!request.signal.aborted && mounted.current) setError(reason instanceof Error ? reason.message : '候选读取失败。'); }
    finally { if (!request.signal.aborted && mounted.current) setLoading(false); }
  }, []);
  useEffect(() => {
    let alive = true; mounted.current = true;
    void Promise.resolve().then(() => { if (alive) void refresh(); });
    const onFocus = () => { void refresh(); };
    window.addEventListener('focus', onFocus);
    return () => { alive = false; mounted.current = false; controller.current?.abort(); window.removeEventListener('focus', onFocus); };
  }, [refresh]);
  const candidates = useMemo(() => recoveredRecipeCandidates(recovered, current?.items), [recovered, current?.items]);
  return <section className={styles.root} aria-label="可恢复的菜谱候选">
    <div className={styles.heading}><h3><ClipboardList size={17}/>待核对的菜谱候选</h3><button type="button" className={styles.refresh} disabled={loading || disabled} onClick={() => void refresh()}><RefreshCw size={13}/>刷新未保存候选</button></div>
    <p className={styles.note}>读取本账号仍在保留期内的候选，换设备后可继续核对。这里不会重新联网搜索，也不会自动保存。</p>
    {loading && <p role="status" className={styles.loading}><LoaderCircle size={14} className="spin"/>正在读取候选…</p>}
    {error && <p role="alert" className={styles.error}>{error} 已显示的候选仍保留，可稍后刷新。</p>}
    {!loading && !candidates.length && <p className={styles.note}>目前没有未保存的候选。搜索库内没有的菜谱时，联网来源会整理在这里，等待你核对。</p>}
    {!!candidates.length && <RecipeCandidates payload={{ items: candidates, notice: typeof current?.notice === 'string' ? current.notice : '这些是来源摘要，配料、分量和做法仍需按原文核对；确认后保存到个人菜谱。' }} disabled={disabled} onSaved={() => { onSaved(); }} />}
  </section>;
}
