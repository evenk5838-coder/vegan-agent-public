'use client';

import { useEffect, useRef, useState } from 'react';
import { Check, LoaderCircle, RefreshCw } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { personalRecipeRecord, readPersonalRecipe, recipeEditFromRecord, recipeEditInput, recipeRecordVersion, recipeUpdateRequest, savedRecipeMatches, type PersonalRecipeRecord, type RecipeEdit, type RecipeInput } from '../lib/recipe-edit';
import { recipeView } from '../lib/recipe-display';
import RecipeDetail from './RecipeDetail';
import styles from './recipe-candidates.module.css';

export default function PersonalRecipeEditor({ recipeId, onSaved, onClose }: { recipeId: string; onSaved: () => void; onClose: () => void }) {
  const [baseline, setBaseline] = useState<PersonalRecipeRecord | null>(null);
  const [edit, setEdit] = useState<RecipeEdit | null>(null);
  const [conflict, setConflict] = useState<PersonalRecipeRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [requiresRead, setRequiresRead] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const pending = useRef<{ input: RecipeInput; version: string } | null>(null);
  const mounted = useRef(true);
  const busyRef = useRef(false);

  useEffect(() => {
    const controller = new AbortController(); mounted.current = true;
    void readPersonalRecipe(api, recipeId, controller.signal).then(record => {
      if (controller.signal.aborted) return;
      if (!record) { setUnavailable(true); setError('这道个人菜谱已不存在或无法访问。'); return; }
      setBaseline(record); setEdit(recipeEditFromRecord(record));
    }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '暂时无法读取菜谱。'); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { mounted.current = false; controller.abort(); };
  }, [recipeId]);

  function change<K extends keyof RecipeEdit>(field: K, value: RecipeEdit[K]) {
    setEdit(previous => previous ? { ...previous, [field]: value, confirmed: field === 'confirmed' ? value as boolean : false } : previous);
    setError(''); setNotice('');
  }

  function complete(record: PersonalRecipeRecord) {
    pending.current = null; setUncertain(false); setNotice('修改已保存到这道个人菜谱。');
    setBaseline(record); setRequiresRead(false);
    setEdit(previous => previous ? { ...previous, confirmed: false } : previous);
    try { onSaved(); } catch { /* A list refresh failure cannot undo the saved recipe. */ }
  }

  async function refresh() {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try {
      const record = await readPersonalRecipe(api, recipeId);
      if (!mounted.current) return;
      if (!record) { setUnavailable(true); setError('这道个人菜谱已不存在或无法访问，未继续保存。'); return; }
      if (pending.current && recipeRecordVersion(record) && recipeRecordVersion(record) !== pending.current.version
          && savedRecipeMatches(record, recipeId, pending.current.input)) { complete(record); return; }
      if (!edit) { setBaseline(record); setEdit(recipeEditFromRecord(record)); }
      else { setConflict(record); setEdit(previous => previous ? { ...previous, confirmed: false } : previous); setNotice('已读取最新记录，你的输入仍保留。请查看最新内容，再决定是否继续编辑。'); }
      pending.current = null; setUncertain(false); setRequiresRead(false);
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : '读取没有完成，请重试。'); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  }

  async function save() {
    if (!edit || !baseline || busyRef.current || loading || unavailable || uncertain || conflict || requiresRead) return;
    let body: RecipeInput;
    let request: RequestInit;
    const version = recipeRecordVersion(baseline);
    try { body = recipeEditInput(edit); request = recipeUpdateRequest(baseline, body); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '请核对填写内容。'); return; }
    if (!version) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice('');
    let sent = false;
    try {
      pending.current = { input: body, version }; sent = true;
      const result = await api<unknown>('/recipes/' + encodeURIComponent(recipeId), request);
      if (!mounted.current) return;
      const record = personalRecipeRecord(result);
      if (!record || !recipeRecordVersion(record) || recipeRecordVersion(record) === version || !savedRecipeMatches(record, recipeId, body)) throw new ApiError('INVALID_RESPONSE', '尚未核实修改结果，请重新读取。', 200);
      complete(record);
    } catch (reason) {
      if (!mounted.current) return;
      const unknown = sent && (!(reason instanceof ApiError) || reason.status === 0 || reason.status >= 500 || reason.code === 'INVALID_RESPONSE');
      setUncertain(unknown);
      if (!unknown) pending.current = null;
      if (reason instanceof ApiError && reason.status === 404) setUnavailable(true);
      if (reason instanceof ApiError && reason.status === 409) {
        setRequiresRead(true);
        setError('保存遇到状态冲突。输入仍保留，请重新读取最新记录后核对；没有自动重试。');
        setEdit(previous => previous ? { ...previous, confirmed: false } : previous);
        try {
          const latest = await readPersonalRecipe(api, recipeId);
          if (!mounted.current) return;
          if (!latest) { setUnavailable(true); setError('这道个人菜谱已不存在或无法访问，未继续保存。'); }
          else setConflict(latest);
        } catch { if (mounted.current) setError('保存遇到状态冲突，最新记录暂未读到。输入仍保留，请重新读取后核对。'); }
      } else setError(unknown ? '未收到确定的修改结果。输入仍保留，请先重新读取本次保存结果，不会自动再次提交。' : reason instanceof Error ? reason.message : '修改没有完成。');
    } finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  }

  const conflictView = conflict ? recipeView(conflict) : null;
  const locked = loading || busy || unavailable || uncertain;
  const versionMissing = !!baseline && !recipeRecordVersion(baseline);
  return <section className={styles.root} aria-label="编辑个人菜谱">
    <div className={styles.heading}><h3>修改这道个人菜谱</h3><button type="button" className="text-button" disabled={busy || uncertain} onClick={onClose}>放弃本次修改并收起</button></div>
    <p className={styles.note}>仅修改本人的菜谱，保存后仍保留原记录。每次调整内容后都需要重新确认。</p>
    {versionMissing && <p className="warning" role="status">缺少菜谱版本，暂不能保存。请重新读取最新记录后核对。</p>}
    {loading && <p className={styles.loading} role="status"><LoaderCircle className="spin" size={14}/>正在读取个人菜谱…</p>}
    {conflict && <div className="warning"><p>请核对最新记录，你的填写内容没有被清空。</p>{conflictView && <details open><summary>查看最新已保存的内容</summary><h4>{conflictView.title}</h4><RecipeDetail recipe={conflictView}/></details>}<button type="button" className="text-button" disabled={busy || !recipeRecordVersion(conflict)} onClick={() => { setBaseline(conflict); setConflict(null); setRequiresRead(false); setEdit(previous => previous ? { ...previous, confirmed: false } : previous); setNotice('已更新对照记录，继续保留你的输入。请逐项核对并重新勾选确认。'); }}>我已核对，继续保留输入编辑</button></div>}
    {edit && <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset className={styles.fields} disabled={locked}>
        <label>菜谱名称<input required maxLength={100} value={edit.title} onChange={event => change('title', event.target.value)}/></label>
        <div className={styles.grid}>
          <label>餐次<select required value={edit.meal} onChange={event => change('meal', event.target.value)}><option value="">请核对餐次</option>{[['breakfast','早餐'],['lunch','午餐'],['dinner','晚餐'],['afternoon_tea','下午茶'],['main','正餐（旧分类）']].map(([id,label]) => <option key={id} value={id}>{label}</option>)}</select></label>
          <label>用途<select required value={edit.purpose} onChange={event => change('purpose', event.target.value)}><option value="">请核对用途</option><option value="everyday">日常搭配</option><option value="protein">补蛋白（个人标签）</option><option value="fat">补脂肪（个人标签）</option></select></label>
          <label>菜谱分类<select required value={edit.cuisine} onChange={event => change('cuisine', event.target.value)}><option value="">请核对分类</option><option value="other">待补分类</option><option value="chinese">中式</option><option value="western">西式</option>{edit.cuisine === 'foreign' && <option value="foreign">外国菜（旧记录，未细分）</option>}</select></label>
        </div>
        <label className={styles.check}><input type="checkbox" checked={edit.dessert} onChange={event => change('dessert', event.target.checked)}/>同时标为甜点</label>
        <label>食材名称与分量<textarea required rows={3} maxLength={6000} value={edit.ingredients} onChange={event => change('ingredients', event.target.value)}/></label>
        <label>详细做法<textarea required rows={4} maxLength={10000} value={edit.steps} onChange={event => change('steps', event.target.value)}/></label>
        <div className={styles.grid}><label>份量数量（可选）<input type="number" min={0} max={100} step="any" value={edit.yield_amount} onChange={event => change('yield_amount', event.target.value)}/></label><label>份量单位（与数量一起填写）<input maxLength={20} placeholder="例如：人份、碗、个" value={edit.yield_unit} onChange={event => change('yield_unit', event.target.value)}/></label><label>来源类型<select required value={edit.origin} onChange={event => change('origin', event.target.value)}><option value="">请明确来源</option><option value="user_original">我的原创（个人声明）</option><option value="adapted">参考后改编</option><option value="external">外部来源</option></select></label></div>
        <label>来源链接{edit.origin === 'user_original' ? '（原创可不填）' : ''}<input type="url" maxLength={2000} value={edit.source_url} onChange={event => change('source_url', event.target.value)} placeholder="https://…"/></label>
        <label>个人搭配笔记（未经营养审核）<textarea rows={2} maxLength={2000} value={edit.nutrition_notes} onChange={event => change('nutrition_notes', event.target.value)}/></label>
        <label className={styles.check}><input type="checkbox" required checked={edit.confirmed} onChange={event => change('confirmed', event.target.checked)}/>我已核对修改内容，确认保存到这道个人菜谱</label>
      </fieldset>
      <div className={styles.actions}><button className="primary" disabled={locked || !!conflict || requiresRead || versionMissing || !edit.confirmed}>{busy ? <LoaderCircle className="spin" size={14}/> : <Check size={14}/>}确认保存修改</button></div>
    </form>}
    {error && <p role="alert" className={styles.error}>{error}</p>}{notice && <p role="status" className={styles.note}>{notice}</p>}
    <button type="button" className="text-button" disabled={loading || busy || unavailable} onClick={() => void refresh()}><RefreshCw size={13}/>{uncertain ? '重新读取本次保存结果' : '重新读取最新记录'}</button>
  </section>;
}
