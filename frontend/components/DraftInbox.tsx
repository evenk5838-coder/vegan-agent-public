'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronRight, ClipboardList, LoaderCircle, Plus } from 'lucide-react';
import { api } from '../lib/api';
import { draftCreateBody, type ParsedDate } from '../lib/food-forms';
import FoodDraft from './FoodDraft';

type Draft = { draft_id: string; item_count: number; status: string; expires_at: string };
export default function DraftInbox({ onSaved }: { onSaved: () => void | Promise<void> }) {
  const [rows, setRows] = useState<Draft[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [phrase, setPhrase] = useState('');
  const [dateType, setDateType] = useState('');
  const [preview, setPreview] = useState<ParsedDate | null>(null);
  const [previewInput, setPreviewInput] = useState('');
  const [parsing, setParsing] = useState(false);
  const parseRequest = useRef<AbortController | null>(null);
  const creating = useRef(false);
  const refresh = useCallback(async () => {
    const data = await api<{ items: Draft[] }>('/foods/drafts');
    setRows(data.items);
  }, []);
  useEffect(() => { let alive = true; void api<{ items: Draft[] }>('/foods/drafts').then(data => {if(alive)setRows(data.items);}).catch(e => {if(alive)setError(e.message);}).finally(()=>{if(alive)setLoading(false);});return()=>{alive=false;}; }, []);
  useEffect(() => () => parseRequest.current?.abort(), []);

  function changePhrase(value: string) {
    parseRequest.current?.abort();
    parseRequest.current = null;
    setPhrase(value); setPreview(null); setPreviewInput(''); setParsing(false); setError('');
  }

  async function parseDate() {
    parseRequest.current?.abort();
    const controller = new AbortController();
    parseRequest.current = controller;
    const text = phrase.trim();
    if (!text) return;
    setParsing(true); setError(''); setPreview(null); setPreviewInput('');
    try {
      const value = await api<ParsedDate>('/dates/parse', {
        method: 'POST', signal: controller.signal,
        body: JSON.stringify({ phrase: text, reference_time: new Date().toISOString(), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }),
      });
      if (controller.signal.aborted) return;
      setPreview(value); setPreviewInput(text);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '日期未能解析，请改用明确日期。');
    } finally {
      if (parseRequest.current === controller) { setParsing(false); parseRequest.current = null; }
    }
  }

  async function createDraft(form: HTMLFormElement) {
    if (creating.current) return;
    creating.current = true; setBusy(true); setError('');
    try {
      const names = String(new FormData(form).get('names') || '').split('\n');
      const body = draftCreateBody(names, phrase, dateType, preview, previewInput);
      const result = await api<{ draft_id: string }>('/foods/drafts', { method: 'POST', body: JSON.stringify(body) });
      setSelected(result.draft_id); setAdding(false);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '草稿未能整理，请重试。');
    } finally { creating.current = false; setBusy(false); }
  }

  return <section className="panel draft-inbox">
    <div className="row-between"><h3><ClipboardList size={18}/> 待核对的食材</h3><button className="text-button" disabled={busy} onClick={() => setAdding(!adding)}><Plus size={15}/> 批量登记</button></div>
    <p className="small muted">先整理成草稿，补齐信息并确认后，再放入你的食材记录。</p>
    {adding && <form onSubmit={e => {e.preventDefault();void createDraft(e.currentTarget);}}>
      <label>食材名称，每行一项<textarea name="names" rows={3} required maxLength={3000} disabled={busy} placeholder={'豆腐\n西兰花\n燕麦'}/></label>
      <div className="form-grid">
        <label>这批食材的日期描述（可选）<input value={phrase} maxLength={40} disabled={busy} placeholder="例如：今天、昨天下午3点" onChange={e => changePhrase(e.target.value)}/></label>
        <label>这一天是<select value={dateType} disabled={busy} onChange={e => setDateType(e.target.value)}><option value="">稍后逐项确认</option><option value="purchase">购买日期</option><option value="production">生产日期</option><option value="prepared">制作日期</option><option value="opened">开封日期</option></select></label>
      </div>
      {phrase.trim() && <button type="button" className="text-button" disabled={busy || parsing} onClick={() => void parseDate()}>{parsing ? <LoaderCircle className="spin" size={15}/> : null}核对日期</button>}
      {preview && <div className="warning" role="status"><p>“{previewInput}”对应 {preview.resolved_date}{preview.time_precision === 'date' ? '（仅日期）' : preview.time_precision === 'period' ? '（时段起点）' : ''} · {preview.timezone}</p><p className="small">{preview.time_precision === 'date' ? '没有提供具体时刻，不把记录用的 00:00 当作实际购买时间。' : `解析时刻：${new Date(preview.started_at).toLocaleString('zh-CN', { timeZone: preview.timezone })}`}</p>{preview.warnings.map((warning, index) => <p className="small" key={index}>{warning}</p>)}</div>}
      <button className="secondary" disabled={busy || parsing || (!!phrase.trim() && !preview)}>{busy?<LoaderCircle className="spin" size={15}/>:null}整理核对卡片</button>
      <p className="small muted">每批最多 30 项。日期描述应用到这批食材，可在下一步逐项修改；没有提供的信息会继续留空等待核对。</p>
    </form>}
    {error && <p role="alert" className="error">{error}</p>}
    {loading ? <p role="status" className="small muted">正在读取草稿…</p> : rows.length > 0 && <details><summary>查看草稿（{rows.length}）<ChevronRight size={15}/></summary>{rows.map(row=><button key={row.draft_id} className="draft-open" onClick={()=>setSelected(row.draft_id)}><span>{row.item_count} 项食材 · {row.status==='confirmed'?'已保存':row.status==='partially_saved'?'部分已保存':'待核对'}</span><ChevronRight size={15}/></button>)}</details>}
    {selected && <div className="space-top"><button className="text-button" onClick={()=>setSelected(null)}>收起核对卡片</button><FoodDraft key={selected} draftId={selected} onSaved={()=>{void refresh().catch(e=>setError(e.message));void Promise.resolve(onSaved()).catch(e=>setError(e.message));}}/></div>}
  </section>;
}
