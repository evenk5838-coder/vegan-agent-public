'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ClipboardCheck, ExternalLink, LoaderCircle, RefreshCw } from 'lucide-react';
import { api, ApiError, safeExternalUrl } from '../lib/api';
import { candidateExpired, candidateStatusLabel, candidateReviewNotice, candidateRecipeInput, newCandidateEdit, recipeCandidates, savedCandidateRecipes,
  type CandidateRecipeEdit, type CandidateRecipeInput, type RecipeCandidate, type SavedCandidateRecipe } from '../lib/recipe-candidates';
import { recipeView } from '../lib/recipe-display';
import { loadPersonalRecipeRecords } from '../lib/recipe-edit';
import RecipeDetail from './RecipeDetail';
import styles from './recipe-candidates.module.css';

type SaveLookup = 'loading' | 'ready' | 'failed';

function CandidateItem({ candidate, saved, disabled, lookup, refresh, onSaved }: {
  candidate: RecipeCandidate; saved?: SavedCandidateRecipe; disabled: boolean; lookup: SaveLookup;
  refresh: () => Promise<void>; onSaved: (record: SavedCandidateRecipe) => void;
}) {
  const [edit, setEdit] = useState(() => newCandidateEdit(candidate));
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [unavailable, setUnavailable] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const pending = useRef<CandidateRecipeInput | null>(null);
  const busyRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  function change<K extends keyof CandidateRecipeEdit>(field: K, value: CandidateRecipeEdit[K]) {
    setEdit(previous => ({ ...previous, [field]: value, confirmed: field === 'confirmed' ? value as boolean : false }));
    setError('');
  }

  async function save() {
    if (disabled || saved || unavailable || lookup !== 'ready' || busyRef.current) return;
    if (candidateExpired(candidate)) { setUnavailable(true); setError('这条候选已过期，请重新搜索；已保存的个人菜谱不受候选到期影响。'); return; }
    setError('');
    try {
      pending.current ??= candidateRecipeInput(edit, candidate);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '请核对输入。');
      return;
    }
    busyRef.current = true; setBusy(true);
    try {
      // The backend binds one saved record to this candidate; an uncertain response reuses this body.
      const record = await api<SavedCandidateRecipe>(`/recipes/candidates/${encodeURIComponent(candidate.candidate_id)}/save`, {
        method: 'POST', body: JSON.stringify(pending.current),
      });
      if (!mounted.current) return;
      if (!record.id || record.candidate_id !== candidate.candidate_id) throw new ApiError('INVALID_RESPONSE', '保存结果暂时无法确认，请重新读取状态。', 200);
      pending.current = null; setUncertain(false); onSaved(record);
    } catch (reason) {
      if (!mounted.current) return;
      const unknown = !(reason instanceof ApiError) || reason.status === 0 || reason.status >= 500 || reason.code === 'INVALID_RESPONSE';
      setUncertain(unknown);
      if (!unknown) pending.current = null;
      if (reason instanceof ApiError && reason.status === 404) {
        setUnavailable(true);
        setError('这条候选已过期或无法访问，请重新搜索。若此前保存过，重新读取状态可找到个人菜谱。');
        void refresh();
      } else {
        setError(unknown ? '未收到确定的保存结果，填写内容仍保留。可先重新读取状态，或安全重试本次保存，不会重复添加。'
          : reason instanceof Error ? reason.message : '保存未完成，请稍后重试。');
      }
    } finally {
      busyRef.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  const source = safeExternalUrl(candidate.source_url);
  const expired = candidateExpired(candidate);
  const markedSaved = candidate.status === 'saved' || candidate.status === '已保存到个人菜谱';
  const locked = disabled || busy || unavailable || expired || markedSaved || uncertain || lookup !== 'ready';
  const savedView = saved ? recipeView(saved) : null;
  const expiry = candidate.expires_at && Number.isFinite(Date.parse(candidate.expires_at)) ? new Date(candidate.expires_at).toLocaleString('zh-CN') : null;
  return <article className={styles.item}>
    <div className={styles.heading}><h4>{saved?.title || candidate.title || '菜谱候选来源'}</h4><span className={saved ? styles.savedBadge : styles.badge}>{saved ? <><Check size={12}/>已保存到个人菜谱</> : candidateStatusLabel(candidate)}</span></div>
    {source && <a className={styles.source} href={source} target="_blank" rel="noreferrer">打开来源原文<ExternalLink size={12}/></a>}
    <details><summary className={styles.source}>查看来源摘要</summary><p className={styles.summary}>{candidate.summary || '此来源暂未提供摘要，请查看原文。'}</p></details>
    {saved ? <details className={styles.details}><summary>查看已保存的内容</summary>{savedView && <RecipeDetail recipe={savedView}/>}<p className={styles.note}>内容已保存并可跨设备读取，后续管理请前往“选菜谱”。</p></details> : <>
      <p className={styles.note}>{candidateReviewNotice(candidate)}</p>
      <details><summary className={styles.source}>来源状态</summary>{candidate.notice && <p className={styles.note}>{candidate.notice}</p>}<p className={styles.note}>{expiry ? `候选保留至：${expiry}。` : '服务端暂未提供候选到期时刻。'}候选保存后，个人菜谱按自己的保留规则管理。</p></details>
      {markedSaved && <p className={styles.note}>来源已标记保存，但目前未读取到对应个人菜谱。请重新读取保存状态，不会自动创建副本。</p>}
      {expired && <p className={styles.note}>这条候选已过期，请重新搜索后再核对保存。</p>}
      {!open ? <button type="button" className="secondary" disabled={locked} onClick={() => setOpen(true)}>核对并补全教程</button> : <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset disabled={locked} className={styles.fields}>
          <label>菜谱名称<input required maxLength={100} value={edit.title} onChange={event => change('title', event.target.value)}/></label>
          <details><summary className={styles.source}>分类与个人标签（可选）</summary><div className={styles.grid}>
            <label>餐次<select value={edit.meal} onChange={event => change('meal', event.target.value)}><option value="breakfast">早餐</option><option value="lunch">午餐</option><option value="dinner">晚餐</option><option value="afternoon_tea">下午茶</option></select></label>
            <label>用途<select value={edit.purpose} onChange={event => change('purpose', event.target.value)}><option value="everyday">日常搭配</option><option value="protein">补蛋白（个人标签）</option><option value="fat">补脂肪（个人标签）</option></select></label>
            <label>菜谱分类<select value={edit.cuisine} onChange={event => change('cuisine', event.target.value)}><option value="other">待补分类</option><option value="chinese">中国菜</option><option value="western">外国菜</option></select></label>
          </div>
          <label className={styles.check}><input type="checkbox" checked={edit.dessert} onChange={event => change('dessert', event.target.checked)}/>同时标为甜点</label></details>
          <label>原材料与用量<textarea required rows={3} maxLength={6000} value={edit.ingredients} placeholder="逐项列出原料和用量" onChange={event => change('ingredients', event.target.value)}/></label>
          <label>制作步骤<textarea required rows={4} maxLength={10000} value={edit.steps} placeholder="按顺序列出操作，保留必要的时间和温度" onChange={event => change('steps', event.target.value)}/></label>
          <div className={styles.grid}><label>原文份量数量（可选）<input type="number" min={0} max={100} step="any" value={edit.yield_amount || ''} onChange={event => change('yield_amount', event.target.value)}/></label><label>原文份量单位<input maxLength={20} value={edit.yield_unit || ''} placeholder="例如：人份、碗、个" onChange={event => change('yield_unit', event.target.value)}/></label></div>
          <p className={styles.note}>未提供份量时保持未知，保存后仍会标明教程信息未齐全。</p>
          <label>个人搭配笔记（可选，未经营养审核）<textarea rows={2} maxLength={2000} value={edit.nutrition_notes} onChange={event => change('nutrition_notes', event.target.value)}/></label>
          <label className={styles.check}><input type="checkbox" required checked={edit.confirmed} onChange={event => change('confirmed', event.target.checked)}/>我已查看原文、核对填写内容，确认保存到个人菜谱</label>
        </fieldset>
        <p className={styles.note}>保存仅供你个人使用，不会自动公开；之后可在菜谱页面删除。</p>
        <div className={styles.actions}>
          <button type="submit" className="primary" disabled={disabled || busy || unavailable || expired || markedSaved || lookup !== 'ready' || (!uncertain && !edit.confirmed)}>{busy ? <LoaderCircle size={15} className="spin"/> : <Check size={15}/>}{busy ? '正在保存…' : uncertain ? '安全重试本次保存' : '确认保存到个人菜谱'}</button>
          {!uncertain && <button type="button" className="text-button" disabled={busy} onClick={() => setOpen(false)}>暂不保存</button>}
        </div>
      </form>}
      {error && <p className={styles.error} role="alert">{error}</p>}
    </>}
  </article>;
}

export default function RecipeCandidates({ payload, disabled = false, onSaved }: { payload: Record<string, unknown>; disabled?: boolean; onSaved?: (record: SavedCandidateRecipe) => void | Promise<void> }) {
  const candidates = useMemo(() => recipeCandidates(payload.items), [payload.items]);
  const candidateKey = candidates.map(candidate => candidate.candidate_id).join('|');
  const [saved, setSaved] = useState<Record<string, SavedCandidateRecipe>>({});
  const [lookup, setLookup] = useState<SaveLookup>('loading');
  const [lookupError, setLookupError] = useState('');
  const mounted = useRef(true);
  const refreshController = useRef<AbortController | null>(null);

  const loadSaved = useCallback(() => {
    refreshController.current?.abort();
    const controller = new AbortController(); refreshController.current = controller;
    return loadPersonalRecipeRecords(api, controller.signal).then(items => {
      if (!mounted.current || controller.signal.aborted) return;
      // Existing records are the source of truth after refresh and on other devices.
      setSaved(savedCandidateRecipes(items)); setLookup('ready'); setLookupError('');
    }).catch(reason => {
      if (!mounted.current || controller.signal.aborted) return;
      setLookup('failed'); setLookupError(reason instanceof Error ? reason.message : '暂时无法读取保存状态。');
    });
  }, []);
  const refresh = useCallback(async () => {
    setLookup('loading'); setLookupError('');
    await loadSaved();
  }, [loadSaved]);

  useEffect(() => {
    mounted.current = true;
    void loadSaved();
    const onFocus = () => { void refresh(); };
    window.addEventListener('focus', onFocus);
    return () => { mounted.current = false; refreshController.current?.abort(); window.removeEventListener('focus', onFocus); };
  }, [refresh, loadSaved, candidateKey]);

  return <section className={styles.root} aria-label="待核对的菜谱来源">
    <div className={styles.heading}><h3><ClipboardCheck size={17}/>待核对的菜谱来源</h3><button type="button" className={styles.refresh} disabled={lookup === 'loading'} onClick={() => { void refresh(); }}><RefreshCw size={13}/>重新读取保存状态</button></div>
    <p className={styles.note}>{typeof payload.notice === 'string' ? payload.notice : '来源摘要不是完整教程，确认后才会保存到个人菜谱，不会自动公开。'}</p>
    {lookup === 'loading' && <p className={styles.loading} role="status"><LoaderCircle size={14} className="spin"/>正在核对个人菜谱的保存状态…</p>}
    {lookupError && <p className={styles.error} role="alert">{lookupError} 请重新读取后再保存。</p>}
    {disabled && <p className={styles.note}>本轮对话未成功结束，候选可查看，暂不能保存。</p>}
    {!candidates.length && <p className={styles.note}>没有可核对的候选，请重新说明想找的菜谱。</p>}
    {candidates.map(candidate => <CandidateItem key={candidate.candidate_id} candidate={candidate} saved={saved[candidate.candidate_id]} disabled={disabled} lookup={lookup} refresh={refresh} onSaved={record => {
      refreshController.current?.abort();
      setLookup('ready'); setLookupError('');
      setSaved(previous => ({ ...previous, [candidate.candidate_id]: record }));
      try { void Promise.resolve(onSaved?.(record)).catch(() => {}); } catch { /* Save state has already been received. */ }
    }}/>) }
  </section>;
}
